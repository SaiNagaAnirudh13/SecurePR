import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import DataError
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.api.webhooks import _handle_check_event
from app.core.db import Base
from app.integrations import github_client as github_module
from app.integrations import groq_client as groq_module
from app.integrations.ai_provider import (
    AIAnalysisResult,
    AIInvalidResponse,
    AIUnavailable,
)
from app.models import models as m
from app.services import pipeline

UTC_TZ = timezone(timedelta(0))


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'review_ci.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()


@pytest.fixture()
def repo_and_pr(db_session):
    repo = m.Repository(
        github_repo_id="1",
        owner_login="octo",
        name="widgets",
        full_name="octo/widgets",
        installation_id="999",
    )
    db_session.add(repo)
    db_session.flush()
    pr = m.PullRequest(
        repository_id=repo.id,
        github_pr_number=7,
        title="Add feature",
        author_login="octocat",
        head_sha="abc123",
        base_branch="main",
        head_branch="feature",
        state="open",
    )
    user = m.User(github_login="octocat", github_user_id="123")
    db_session.add_all([pr, user])
    db_session.flush()
    db_session.add(
        m.AutoApprovalEligibility(
            repository_id=repo.id,
            user_id=user.id,
            is_enabled=True,
            max_risk_threshold=39,
        )
    )
    db_session.commit()
    return repo, pr


def _patch_review_dependencies(
    monkeypatch,
    *,
    expected_head="abc123",
    live_checks=None,
    live_errors=(),
    auto_merge_result=None,
):
    monkeypatch.setattr(
        github_module.github_client,
        "get_pull_request",
        lambda *args, **kwargs: {
            "state": "open",
            "head_sha": expected_head,
            "base_ref": "main",
        },
    )
    monkeypatch.setattr(
        github_module.github_client,
        "get_pr_files",
        lambda *args, **kwargs: [
            {
                "file_path": "app/service.py",
                "change_type": "modified",
                "additions": 1,
                "deletions": 0,
                "patch_excerpt": "+value = 1",
            }
        ],
    )
    posted_reviews = []
    posted_comments = []

    def post_review(*args, **kwargs):
        posted_reviews.append((args, kwargs))
        return {
            "succeeded": True,
            "status_code": 200,
            "payload": {"id": len(posted_reviews)},
        }

    monkeypatch.setattr(github_module.github_client, "post_review", post_review)
    monkeypatch.setattr(
        github_module.github_client,
        "post_issue_comment",
        lambda *args, **kwargs: (
            posted_comments.append((args, kwargs))
            or {"succeeded": True, "status_code": 201, "payload": {"id": 1}}
        ),
    )
    monkeypatch.setattr(
        github_module.github_client,
        "dismiss_review",
        lambda *args, **kwargs: {"succeeded": True, "status_code": 200, "payload": {}},
    )
    monkeypatch.setattr(
        groq_module.groq_client,
        "score_dimensions",
        lambda *args, **kwargs: _provider_result(),
    )
    monkeypatch.setattr(
        pipeline.policy_engine,
        "load_repo_policy",
        lambda _repo: pipeline.policy_engine.load_default_policy(),
    )
    if live_checks is None:
        live_checks = [
            {
                "identity": "live-check",
                "kind": "check_run",
                "name": "live-confirmation",
                "status": "completed",
                "conclusion": "success",
                "sha": expected_head,
            }
        ]
    monkeypatch.setattr(
        pipeline,
        "_fetch_ci_checks",
        lambda *args, **kwargs: (live_checks, [], list(live_errors)),
    )
    if auto_merge_result is None:
        auto_merge_result = SimpleNamespace(
            status="DASHBOARD_CONFIGURATION_MISSING",
            reason="No saved dashboard configuration authorizes auto-merge.",
            response_status=None,
            response_payload=None,
            merge_commit_sha=None,
            ci_state=None,
            ci_checks=None,
            required_checks=None,
        )
    monkeypatch.setattr(
        pipeline.auto_merge, "attempt", lambda **kwargs: auto_merge_result
    )
    return posted_reviews, posted_comments


def _valid_ai_scores():
    return {
        dimension: {"score": 20, "rationale": "test rationale"}
        for dimension in (
            "change_surface",
            "reversibility",
            "data_security",
            "operational",
            "verification",
            "policy_compliance",
        )
    } | {"summary": "test summary", "findings": []}


def _provider_result(analysis=None):
    return AIAnalysisResult(
        analysis=analysis or _valid_ai_scores(),
        provider="groq",
        model="openai/gpt-oss-120b",
    )


def _add_ci_revision(db_session, repo, pr, state, checks=(), required=()):
    revision = m.CIRevision(
        repository_id=repo.id,
        pull_request_id=pr.id,
        head_sha=pr.head_sha,
        state=state,
        discovery_complete=state not in {"UNKNOWN", "UNAVAILABLE"},
        error="GitHub check discovery API unavailable"
        if state == "UNAVAILABLE"
        else None,
        required_checks=list(required),
    )
    db_session.add(revision)
    db_session.flush()
    for check in checks:
        db_session.add(
            m.CIExecution(
                ci_revision_id=revision.id,
                identity=check.get("identity", check["name"]),
                kind=check.get("kind", "check_run"),
                provider=check.get("provider"),
                app_id=check.get("app_id"),
                context=check.get("context"),
                name=check.get("name"),
                status=check.get("status"),
                conclusion=check.get("conclusion"),
                external_url=check.get("url"),
                head_sha=pr.head_sha,
            )
        )
    db_session.commit()
    return revision


def _run(db_session, repo, pr):
    return pipeline.run_analysis(db_session, repo, pr, repo.installation_id)


def test_passed_ci_and_eligible_low_risk_publish_approval(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    monkeypatch.setattr(
        groq_module.groq_client,
        "score_dimensions",
        lambda *args, **kwargs: _provider_result(),
    )
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [
            {
                "name": "unit-tests",
                "provider": "GitHub Actions",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )

    review = _run(db_session, repo, pr)

    assert review.decision == m.DecisionStatus.APPROVED
    assert review.lifecycle_state == "APPROVED"
    assert review.ai_state == pr.ai_state == "AI_COMPLETED"
    assert review.ci_verified_at is not None
    assert review.analysis_completed_at is not None
    assert len(posted) == 1
    assert posted[0][0][5] == "APPROVE"
    assert posted[0][1]["commit_id"] == pr.head_sha


def test_groq_timeout_is_unavailable_without_fallback_risk_or_approval(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)

    def timeout(*args, **kwargs):
        raise AIUnavailable(
            "Groq analysis could not be completed.",
            error_code="GROQ_REQUEST_TIMEOUT",
            retryable_at=datetime.now(UTC_TZ).replace(tzinfo=None)
            + timedelta(seconds=30),
        )

    monkeypatch.setattr(groq_module.groq_client, "score_dimensions", timeout)
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [
            {
                "name": "unit-tests",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )

    review = _run(db_session, repo, pr)

    assert review.decision == m.DecisionStatus.REVIEW_UNAVAILABLE
    assert review.lifecycle_state == "REVIEW_UNAVAILABLE"
    assert review.ai_state == pr.ai_state == "AI_UNAVAILABLE"
    assert review.analysis_completed_at is None
    assert review.risk_category == "NOT_EVALUATED"
    assert review.overall_score == 0
    assert review.github_review_state == "COMMENT"
    assert review.auto_merge_status is None
    assert len(posted) == 1
    assert posted[0][0][5] == "COMMENT"
    assert "AI Review — **NOT COMPLETED**" in posted[0][0][4]
    assert "**Review Decision:** UNAVAILABLE" in posted[0][0][4]
    assert "**Risk Score:** Not Evaluated" in posted[0][0][4]
    assert "Risk Breakdown" not in posted[0][0][4]
    assert "504 Deadline expired" not in posted[0][0][4]
    assert "**No AI approval decision has been made.**" in posted[0][0][4]
    evidence = (
        db_session.query(m.Evidence)
        .filter_by(review_id=review.id, evidence_type="error", source="ai_provider")
        .one()
    )
    assert "GROQ_REQUEST_TIMEOUT" in evidence.content
    assert "504 Deadline expired" not in evidence.content


@pytest.mark.parametrize(
    ("failure_kind", "expected_state", "expected_error_code", "has_retry_time"),
    [
        ("quota", "AI_UNAVAILABLE", "GROQ_QUOTA_EXCEEDED", True),
        ("malformed", "AI_INVALID_RESPONSE", "GROQ_INVALID_RESPONSE", False),
    ],
)
def test_ai_failures_persist_diagnostics_without_completion_or_approval(
    db_session,
    repo_and_pr,
    monkeypatch,
    failure_kind,
    expected_state,
    expected_error_code,
    has_retry_time,
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    retryable_at = datetime.now(UTC_TZ).replace(tzinfo=None) + timedelta(minutes=5)

    def fail_analysis(*args, **kwargs):
        if failure_kind == "quota":
            raise AIUnavailable(
                "Groq quota exhausted.",
                error_code="GROQ_QUOTA_EXCEEDED",
                retryable_at=retryable_at,
            )
        raise AIInvalidResponse("Groq response was malformed.")

    monkeypatch.setattr(groq_module.groq_client, "score_dimensions", fail_analysis)
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [{"name": "unit-tests", "status": "completed", "conclusion": "success"}],
    )
    auto_merge_attempts = []
    monkeypatch.setattr(
        pipeline.auto_merge,
        "attempt",
        lambda **kwargs: auto_merge_attempts.append(kwargs),
    )

    review = _run(db_session, repo, pr)

    assert review.decision == m.DecisionStatus.REVIEW_UNAVAILABLE
    assert review.lifecycle_state == "REVIEW_UNAVAILABLE"
    assert review.ai_state == pr.ai_state == expected_state
    assert review.ai_error_code == pr.ai_error_code == expected_error_code
    assert review.ai_attempt_id and review.ai_attempt_id == pr.ai_attempt_id
    assert (review.ai_retryable_at == retryable_at) is has_retry_time
    assert review.analysis_completed_at is None
    assert review.risk_category == "NOT_EVALUATED"
    assert review.overall_score == 0
    assert review.auto_merge_status is None
    assert review.github_review_state == "COMMENT"
    assert len(posted) == 1
    assert posted[0][0][5] == "COMMENT"
    assert "Not Evaluated" in posted[0][0][4]
    assert "APPROVE" not in posted[0][0][4]
    assert not auto_merge_attempts


def test_database_persistence_failure_rolls_back_without_github_decision(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)

    def unavailable(*args, **kwargs):
        raise AIUnavailable(
            "Groq is unavailable.",
            error_code="GROQ_UNAVAILABLE",
        )

    monkeypatch.setattr(groq_module.groq_client, "score_dimensions", unavailable)
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [{"name": "unit-tests", "status": "completed", "conclusion": "success"}],
    )
    original_flush = db_session.flush

    def fail_review_flush(objects=None):
        if any(isinstance(item, m.Review) for item in db_session.new):
            raise DataError(
                "INSERT INTO reviews",
                {},
                Exception("value too long for type character varying"),
            )
        return original_flush(objects)

    monkeypatch.setattr(db_session, "flush", fail_review_flush)

    with pytest.raises(DataError, match="value too long"):
        _run(db_session, repo, pr)

    db_session.rollback()
    db_session.refresh(pr)
    assert pr.ai_state == "AI_UNAVAILABLE"
    assert pr.ai_error_code == "GROQ_UNAVAILABLE"
    assert pr.ai_attempt_id
    assert db_session.query(m.Review).count() == 0
    assert not posted


def test_failed_final_review_publication_retries_with_same_marker(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    _patch_review_dependencies(monkeypatch)
    monkeypatch.setattr(
        groq_module.groq_client,
        "score_dimensions",
        lambda *args, **kwargs: _provider_result(),
    )
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [{"name": "unit-tests", "status": "completed", "conclusion": "success"}],
    )
    created_reviews = {}
    requests = []

    def post_review(*args, **kwargs):
        marker = kwargs["idempotency_marker"]
        requests.append(marker)
        if marker in created_reviews:
            return {
                "succeeded": True,
                "status_code": 200,
                "payload": created_reviews[marker],
                "already_exists": True,
            }
        created_reviews[marker] = {"id": 91, "body": args[4]}
        return {
            "succeeded": False,
            "status_code": 504,
            "payload": {"message": "request timed out after publication"},
        }

    monkeypatch.setattr(github_module.github_client, "post_review", post_review)

    with pytest.raises(RuntimeError, match="GitHub rejected review publication"):
        _run(db_session, repo, pr)

    retried_review = _run(db_session, repo, pr)

    assert retried_review.github_action_confirmed is True
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert len(created_reviews) == 1


def test_ci_failure_arriving_during_ai_analysis_blocks_final_decision(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [
            {
                "name": "unit-tests",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )
    ci_events = []

    def fetch_checks(*args, **kwargs):
        ci_events.append("ci")
        conclusion = "success" if len(ci_events) == 1 else "failure"
        return (
            [
                {
                    "identity": "live-check",
                    "kind": "check_run",
                    "name": "unit-tests",
                    "status": "completed",
                    "conclusion": conclusion,
                    "sha": pr.head_sha,
                }
            ],
            [],
            [],
        )

    def score_during_analysis(*args, **kwargs):
        ci_events.append("ai")
        return _provider_result()

    monkeypatch.setattr(pipeline, "_fetch_ci_checks", fetch_checks)
    monkeypatch.setattr(
        groq_module.groq_client, "score_dimensions", score_during_analysis
    )

    review = _run(db_session, repo, pr)

    assert ci_events == ["ci", "ai", "ci"]
    assert review.decision == m.DecisionStatus.BLOCKED
    assert review.github_review_state == "REQUEST_CHANGES"
    assert len(posted) == 1
    assert posted[0][0][5] == "REQUEST_CHANGES"


def test_ci_completion_during_ai_analysis_queues_full_revision_reconciliation(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    revision = _add_ci_revision(
        db_session,
        repo,
        pr,
        "IN_PROGRESS",
        [
            {
                "name": "unit-tests",
                "status": "in_progress",
                "conclusion": None,
            }
        ],
    )

    def complete_ci_during_analysis(*args, **kwargs):
        execution = (
            db_session.query(m.CIExecution).filter_by(ci_revision_id=revision.id).one()
        )
        execution.status = "completed"
        execution.conclusion = "success"
        revision.state = "PASSED"
        revision.discovery_complete = True
        revision.error = None
        db_session.commit()
        return _provider_result()

    monkeypatch.setattr(
        groq_module.groq_client,
        "score_dimensions",
        complete_ci_during_analysis,
    )

    result = _run(db_session, repo, pr)

    assert result is None
    assert db_session.query(m.Review).filter_by(pull_request_id=pr.id).count() == 0
    reconciliation = (
        db_session.query(m.BackgroundJob)
        .filter_by(job_type="REVIEW", state="QUEUED")
        .one()
    )
    assert reconciliation.payload["head_sha"] == pr.head_sha
    assert posted == []

    pipeline.process_review_job(db_session, reconciliation.payload)

    review = (
        db_session.query(m.Review)
        .filter_by(pull_request_id=pr.id, head_sha=pr.head_sha)
        .one()
    )
    assert review.decision == m.DecisionStatus.APPROVED
    assert [call[0][5] for call in posted] == ["APPROVE"]


def test_failed_ci_blocks_approval_and_lists_provider_and_details(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "FAILED",
        [
            {
                "name": "dependency-audit",
                "provider": "GitHub Actions",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "dependency-audit/security",
                "provider": "Security Checks",
                "status": "completed",
                "conclusion": "failure",
                "url": "https://github.com/octo/widgets/actions/runs/99",
            },
        ],
    )

    review = _run(db_session, repo, pr)
    body = posted[0][0][4]

    assert review.decision == m.DecisionStatus.BLOCKED
    assert review.lifecycle_state == "BLOCKED"
    assert posted[0][0][5] == "REQUEST_CHANGES"
    assert "BLOCKED: CI Failed" in body
    assert "dependency-audit/security" in body
    assert "Security Checks" in body
    assert "actions/runs/99" in body
    assert "Approval is blocked because CI failed" in body


def test_stored_pass_is_not_enough_when_live_ci_recheck_fails(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    failing_check = {
        "identity": "live-failure",
        "kind": "check_run",
        "name": "dependency-audit",
        "provider": "Security Checks",
        "status": "completed",
        "conclusion": "failure",
        "sha": pr.head_sha,
    }
    posted, _ = _patch_review_dependencies(monkeypatch, live_checks=[failing_check])
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [
            {
                "name": "unit-tests",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )

    review = _run(db_session, repo, pr)

    assert review.decision == m.DecisionStatus.BLOCKED
    assert posted[0][0][5] == "REQUEST_CHANGES"
    assert "dependency-audit" in posted[0][0][4]


def test_live_ci_api_error_replaces_stored_pass_with_waiting_state(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(
        monkeypatch,
        live_errors=["check-run API returned 403"],
    )
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [
            {
                "name": "unit-tests",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )

    review = _run(db_session, repo, pr)
    revision = db_session.query(m.CIRevision).filter_by(head_sha=pr.head_sha).one()

    assert review.decision == m.DecisionStatus.WAITING_FOR_CI
    assert revision.state == "UNAVAILABLE"
    assert "403" in revision.error
    assert posted == []


@pytest.mark.parametrize("dismissal_error", [None, "GitHub refused to dismiss review."])
def test_ci_failure_during_auto_merge_recheck_dismisses_approval_and_blocks(
    db_session, repo_and_pr, monkeypatch, dismissal_error
):
    repo, pr = repo_and_pr
    auto_merge_result = SimpleNamespace(
        status="CI_FAILED",
        reason="At least one applicable CI check failed on the reviewed SHA.",
        response_status=None,
        response_payload=None,
        merge_commit_sha=None,
        ci_state="FAILED",
        ci_checks=[
            {
                "identity": "late-failure",
                "kind": "check_run",
                "name": "integration-tests",
                "provider": "GitHub Actions",
                "status": "completed",
                "conclusion": "failure",
                "url": "https://github.com/octo/widgets/actions/runs/99",
                "sha": pr.head_sha,
            }
        ],
        required_checks=[],
    )
    posted, _ = _patch_review_dependencies(
        monkeypatch, auto_merge_result=auto_merge_result
    )
    if dismissal_error:
        monkeypatch.setattr(
            pipeline,
            "_dismiss_review_approval",
            lambda *args, **kwargs: dismissal_error,
        )
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [
            {
                "name": "unit-tests",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )

    review = _run(db_session, repo, pr)

    assert review.decision == m.DecisionStatus.BLOCKED
    assert review.lifecycle_state == "BLOCKED"
    assert review.github_review_state == "REQUEST_CHANGES"
    assert review.auto_merge_status == "CI_FAILED"
    assert [call[0][5] for call in posted] == ["APPROVE", "REQUEST_CHANGES"]
    assert "integration-tests" in posted[-1][0][4]
    if dismissal_error:
        assert dismissal_error in review.decision_reasons
        dismissal_finding = (
            db_session.query(m.Finding)
            .filter(
                m.Finding.review_id == review.id,
                m.Finding.title == "Existing SecurePR approval could not be dismissed",
            )
            .one()
        )
        assert dismissal_finding.description == dismissal_error
    revision = db_session.query(m.CIRevision).filter_by(head_sha=pr.head_sha).one()
    assert revision.state == "FAILED"


@pytest.mark.parametrize(
    ("state", "checks", "required"),
    [
        (
            "IN_PROGRESS",
            [
                {
                    "name": "unit-tests",
                    "status": "in_progress",
                    "conclusion": None,
                }
            ],
            (),
        ),
        ("UNKNOWN", (), ()),
        ("UNAVAILABLE", (), ()),
        ("NOT_CONFIGURED", (), ()),
        (
            "IN_PROGRESS",
            [
                {
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                }
            ],
            ({"context": "release-gate", "app_id": None},),
        ),
    ],
)
def test_unverified_ci_never_publishes_final_decision(
    db_session, repo_and_pr, monkeypatch, state, checks, required
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    _add_ci_revision(db_session, repo, pr, state, checks, required)
    monkeypatch.setattr(
        pipeline.policy_engine,
        "evaluate_policy",
        lambda *args, **kwargs: pytest.fail(
            "Policy evaluation must wait until CI verification succeeds."
        ),
    )

    review = _run(db_session, repo, pr)

    assert review.decision == m.DecisionStatus.WAITING_FOR_CI
    assert review.lifecycle_state == "WAITING_FOR_CI"
    assert review.github_action_confirmed is False
    assert posted == []
    assert (
        db_session.query(m.BackgroundJob)
        .filter(m.BackgroundJob.job_type == "REVIEW_STATUS")
        .count()
        >= 1
    )


def test_failed_required_commit_status_blocks_passing_check_run(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "FAILED",
        [
            {
                "name": "build",
                "status": "completed",
                "conclusion": "success",
            },
            {
                "name": "release-gate",
                "context": "release-gate",
                "kind": "commit_status",
                "provider": "Legacy CI",
                "status": "completed",
                "conclusion": "failure",
            },
        ],
        [{"context": "release-gate", "app_id": None}],
    )

    review = _run(db_session, repo, pr)

    assert review.decision == m.DecisionStatus.BLOCKED
    assert posted[0][0][5] == "REQUEST_CHANGES"
    assert "Legacy CI" in posted[0][0][4]


def test_repeated_same_revision_review_does_not_repeat_ai_or_publish(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [
            {
                "name": "build",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )
    ai_calls = []
    monkeypatch.setattr(
        groq_module.groq_client,
        "score_dimensions",
        lambda *args, **kwargs: ai_calls.append(True) or _provider_result(),
    )

    first = _run(db_session, repo, pr)
    second = _run(db_session, repo, pr)

    assert first.id == second.id
    assert len(ai_calls) == 1
    assert len(posted) == 1


def test_concurrent_workers_publish_only_one_final_review(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [
            {
                "name": "build",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )
    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    repo_id = repo.id
    pull_request_id = pr.id

    def run_worker():
        session = factory()
        try:
            worker_repo = session.query(m.Repository).filter_by(id=repo_id).one()
            worker_pr = session.query(m.PullRequest).filter_by(id=pull_request_id).one()
            try:
                review = _run(session, worker_repo, worker_pr)
            except pipeline.RevisionLockUnavailable:
                return None
            return review.id if review else None
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: run_worker(), range(2)))

    assert len([result for result in results if result]) == 1
    assert len(posted) == 1
    assert (
        db_session.query(m.Review)
        .filter_by(pull_request_id=pr.id, head_sha=pr.head_sha)
        .count()
        == 1
    )


def test_old_head_job_cannot_publish_after_head_has_changed(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch, expected_head="new-sha")
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "PASSED",
        [
            {
                "name": "build",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    )

    result = pipeline.run_analysis(
        db_session,
        repo,
        pr,
        repo.installation_id,
        expected_head_sha="abc123",
    )

    assert result is None
    assert pr.head_sha == "new-sha"
    assert posted == []


def test_duplicate_ci_webhook_delivery_enqueues_one_refresh_job(
    db_session, repo_and_pr
):
    _, pr = repo_and_pr
    payload = {
        "repository": {"id": 1, "full_name": "octo/widgets"},
        "installation": {"id": 999},
        "action": "completed",
        "check_run": {
            "head_sha": pr.head_sha,
            "pull_requests": [{"number": pr.github_pr_number}],
        },
    }

    first = _handle_check_event(db_session, payload, "check_run", "delivery-123")
    second = _handle_check_event(db_session, payload, "check_run", "delivery-123")

    assert first["status"] == second["status"] == "accepted"
    jobs = (
        db_session.query(m.BackgroundJob)
        .filter(m.BackgroundJob.job_type == "CI_REFRESH")
        .all()
    )
    assert len(jobs) == 1
    assert (
        not db_session.query(m.BackgroundJob)
        .filter(m.BackgroundJob.job_type == "REVIEW")
        .count()
    )


def test_ci_failure_known_during_partial_discovery_still_blocks(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    _add_ci_revision(
        db_session,
        repo,
        pr,
        "IN_PROGRESS",
        [
            {
                "identity": "known-failure",
                "name": "unit-tests",
                "provider": "GitHub Actions",
                "status": "completed",
                "conclusion": "failure",
            }
        ],
    )
    revision = db_session.query(m.CIRevision).filter_by(head_sha=pr.head_sha).one()
    revision.error = "commit status API returned 503"
    revision.discovery_complete = False
    revision.state = "FAILED"
    db_session.commit()

    review = _run(db_session, repo, pr)

    assert review.decision == m.DecisionStatus.BLOCKED
    assert posted[0][0][5] == "REQUEST_CHANGES"
    evidence = (
        db_session.query(m.Evidence)
        .filter(
            m.Evidence.review_id == review.id,
            m.Evidence.evidence_type == "ci_result",
        )
        .one()
    )
    assert json.loads(evidence.content)["error"] == "commit status API returned 503"


def test_failed_ci_then_passed_reconciles_the_same_revision(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted, _ = _patch_review_dependencies(monkeypatch)
    revision = _add_ci_revision(
        db_session,
        repo,
        pr,
        "FAILED",
        [
            {
                "name": "unit-tests",
                "provider": "GitHub Actions",
                "status": "completed",
                "conclusion": "failure",
            }
        ],
    )

    blocked = _run(db_session, repo, pr)
    assert blocked.decision == m.DecisionStatus.BLOCKED

    execution = (
        db_session.query(m.CIExecution)
        .filter(m.CIExecution.ci_revision_id == revision.id)
        .one()
    )
    execution.conclusion = "success"
    revision.state = "PASSED"
    revision.error = None
    revision.discovery_complete = True
    db_session.commit()

    approved = _run(db_session, repo, pr)

    assert approved.id != blocked.id
    assert approved.head_sha == blocked.head_sha == pr.head_sha
    assert approved.decision == m.DecisionStatus.APPROVED
    assert approved.is_reevaluation is True
    assert [call[0][5] for call in posted] == ["REQUEST_CHANGES", "APPROVE"]
