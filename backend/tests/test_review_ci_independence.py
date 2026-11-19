import os
import time
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.api.webhooks import _handle_check_event  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.integrations import gemini_client as gemini_module  # noqa: E402
from app.integrations import github_client as github_module  # noqa: E402
from app.models import models as m  # noqa: E402
from app.services import pipeline  # noqa: E402
from app.services.background_jobs import enqueue_job  # noqa: E402


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
        github_repo_id="1", owner_login="octo", name="widgets",
        full_name="octo/widgets", installation_id="999",
    )
    db_session.add(repo)
    db_session.flush()
    pr = m.PullRequest(
        repository_id=repo.id, github_pr_number=7, title="Add feature",
        author_login="octocat", head_sha="abc123", base_branch="main",
        head_branch="feature", state="open",
    )
    db_session.add(pr)
    db_session.commit()
    return repo, pr


def _patch_review_dependencies(monkeypatch, *, expected_head="abc123"):
    monkeypatch.setattr(
        github_module.github_client, "get_pull_request",
        lambda *args, **kwargs: {
            "state": "open", "head_sha": expected_head, "base_ref": "main",
        },
    )
    monkeypatch.setattr(
        github_module.github_client, "get_pr_files",
        lambda *args, **kwargs: [{
            "file_path": "app/service.py", "change_type": "modified",
            "additions": 1, "deletions": 0, "patch_excerpt": "+value = 1",
        }],
    )
    posted = []

    def post_review(*args, **kwargs):
        posted.append((args, kwargs))
        return {"succeeded": True, "status_code": 200, "payload": {"id": len(posted)}}

    monkeypatch.setattr(github_module.github_client, "post_review", post_review)
    monkeypatch.setattr(
        github_module.github_client, "post_issue_comment",
        lambda *args, **kwargs: {"succeeded": True, "status_code": 201, "payload": {"id": 1}},
    )
    monkeypatch.setattr(
        github_module.github_client, "notify_owner",
        lambda *args, **kwargs: {"succeeded": True, "status_code": 200},
    )
    monkeypatch.setattr(
        gemini_module.gemini_client, "score_dimensions",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        pipeline.policy_engine, "load_repo_policy",
        lambda _repo: pipeline.policy_engine.load_default_policy(),
    )
    return posted


def test_zero_ci_checks_publishes_review_before_configured_deadline(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted = _patch_review_dependencies(monkeypatch)

    def ci_must_not_be_discovered_by_review(*args, **kwargs):
        raise AssertionError("AI review must not call CI discovery.")

    monkeypatch.setattr(github_module.github_client, "get_check_runs", ci_must_not_be_discovered_by_review)
    monkeypatch.setattr(github_module.github_client, "get_check_suites", ci_must_not_be_discovered_by_review)
    monkeypatch.setattr(github_module.github_client, "get_commit_statuses", ci_must_not_be_discovered_by_review)
    monkeypatch.setattr(pipeline.settings, "REVIEW_RESULT_DEADLINE_SECONDS", 2)

    enqueue_job(
        db_session, "REVIEW", f"review:{repo.id}:{pr.id}:{pr.head_sha}",
        {"repository_id": repo.id, "pull_request_id": pr.id,
         "installation_id": repo.installation_id, "head_sha": pr.head_sha},
    )
    db_session.commit()
    job_payload = db_session.query(m.BackgroundJob).first().payload

    started = time.monotonic()
    pipeline.process_review_job(db_session, job_payload)
    elapsed = time.monotonic() - started

    review = db_session.query(m.Review).filter(m.Review.pull_request_id == pr.id).one()
    assert review.github_action_confirmed is True
    assert len(posted) == 1
    assert elapsed < 2
    assert "No checks discovered" not in posted[0][0][4]  # discovery has not completed yet
    assert "CI status unknown" in posted[0][0][4]


def test_in_progress_check_does_not_delay_ai_review(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted = _patch_review_dependencies(monkeypatch)
    revision = m.CIRevision(
        repository_id=repo.id, pull_request_id=pr.id,
        head_sha=pr.head_sha, state="IN_PROGRESS",
    )
    db_session.add(revision)
    db_session.flush()
    db_session.add(m.CIExecution(
        ci_revision_id=revision.id, identity="run:42", kind="check_run",
        provider="custom-ci", name="custom verification", status="in_progress",
        head_sha=pr.head_sha,
    ))
    db_session.commit()

    def ci_must_not_be_polled_by_review(*args, **kwargs):
        raise AssertionError("Pending CI must not be polled by AI review.")

    monkeypatch.setattr(github_module.github_client, "get_check_runs", ci_must_not_be_polled_by_review)
    monkeypatch.setattr(github_module.github_client, "get_check_suites", ci_must_not_be_polled_by_review)
    monkeypatch.setattr(github_module.github_client, "get_commit_statuses", ci_must_not_be_polled_by_review)
    started = time.monotonic()

    result = pipeline.run_analysis(db_session, repo, pr, repo.installation_id)

    assert time.monotonic() - started < 2
    assert result.github_action_confirmed is True
    assert result.decision != m.DecisionStatus.WAITING_FOR_CI
    assert len(posted) == 1
    assert "In progress" in posted[0][0][4]
    assert "CI monitoring continues" in posted[0][0][4]

    duplicate = pipeline.run_analysis(db_session, repo, pr, repo.installation_id)
    assert duplicate.id == result.id
    assert len(posted) == 1


def test_confirmed_ci_failure_blocks_without_reporting_passed(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted = _patch_review_dependencies(monkeypatch)
    revision = m.CIRevision(
        repository_id=repo.id, pull_request_id=pr.id,
        head_sha=pr.head_sha, state="FAILED",
    )
    db_session.add(revision)
    db_session.flush()
    db_session.add(m.CIExecution(
        ci_revision_id=revision.id, identity="jenkins:task:9", kind="check_run",
        provider="Jenkins", name="arbitrary-security-stage", status="completed",
        conclusion="failure", head_sha=pr.head_sha,
        external_url="https://ci.example.test/job/9",
    ))
    db_session.commit()

    review = pipeline.run_analysis(db_session, repo, pr, repo.installation_id)

    assert review.decision == m.DecisionStatus.BLOCKED
    assert "failed" in posted[0][0][4].casefold()
    assert "All discovered checks passed" not in posted[0][0][4]


def test_stale_review_job_reconciles_to_live_head_before_publication(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted = _patch_review_dependencies(monkeypatch, expected_head="new-sha")

    pipeline.process_review_job(db_session, {
        "repository_id": repo.id, "pull_request_id": pr.id,
        "installation_id": repo.installation_id, "head_sha": "abc123",
    })

    review = db_session.query(m.Review).filter(m.Review.pull_request_id == pr.id).one()
    assert pr.head_sha == "new-sha"
    assert review.head_sha == "new-sha"
    assert len(posted) == 1


def test_review_status_job_publishes_final_ci_and_analysis_results(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    review = m.Review(
        pull_request_id=pr.id,
        head_sha=pr.head_sha,
        decision=m.DecisionStatus.MANUAL_REVIEW,
        decision_reasons=["A human reviewer is required."],
        overall_score=64,
        risk_category="MEDIUM",
        score_data_security=75,
        github_review_state="COMMENT",
        github_action_confirmed=True,
        auto_merge_status="DISABLED_GLOBAL",
        analysis_completed_at=datetime.utcnow(),
    )
    db_session.add(review)
    db_session.flush()
    db_session.add(m.Finding(
        review_id=review.id,
        dimension="data_security",
        severity="HIGH",
        title="Review secret handling",
        description="Sensitive data path changed.",
        file_path="app/secrets.py",
        line_reference="42",
    ))
    revision = m.CIRevision(
        repository_id=repo.id,
        pull_request_id=pr.id,
        head_sha=pr.head_sha,
        state="FAILED",
    )
    db_session.add(revision)
    db_session.flush()
    db_session.add(m.CIExecution(
        ci_revision_id=revision.id,
        identity="check_run:10",
        kind="check_run",
        provider="GitHub Actions",
        name="unit-tests",
        status="completed",
        conclusion="failure",
        head_sha=pr.head_sha,
    ))
    db_session.commit()
    published = []
    monkeypatch.setattr(
        github_module.github_client,
        "post_issue_comment",
        lambda *args, **kwargs: (
            published.append((args, kwargs))
            or {"succeeded": True, "status_code": 201, "payload": {"id": 5}}
        ),
    )

    pipeline.process_review_status_job(db_session, {
        "repository_id": repo.id,
        "pull_request_id": pr.id,
        "installation_id": repo.installation_id,
        "head_sha": pr.head_sha,
    })

    assert len(published) == 1
    body = published[0][0][4]
    assert "SecurePR Review — Waiting for CI" in body
    assert "64.0/100" not in body
    assert "**CI status:** Failed" in body
    assert "unit-tests" in body
    assert db_session.query(m.GithubAction).filter(
        m.GithubAction.action_type == "update_review_status_comment"
    ).one().succeeded is True


def test_duplicate_ci_webhook_delivery_enqueues_one_refresh_job(
    db_session, repo_and_pr
):
    repo, pr = repo_and_pr
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
    jobs = db_session.query(m.BackgroundJob).filter(
        m.BackgroundJob.job_type == "CI_REFRESH"
    ).all()
    assert len(jobs) == 1
    assert not db_session.query(m.BackgroundJob).filter(
        m.BackgroundJob.job_type == "REVIEW"
    ).count()


def test_late_ci_failure_updates_state_without_separate_notification(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    _patch_review_dependencies(monkeypatch)
    review = m.Review(
        pull_request_id=pr.id, head_sha=pr.head_sha,
        decision=m.DecisionStatus.MANUAL_REVIEW,
        github_action_confirmed=True, analysis_completed_at=datetime.utcnow(),
    )
    db_session.add(review)
    db_session.commit()
    monkeypatch.setattr(github_module.github_client, "get_check_runs", lambda *args: [{
        "identity": "check_run:99", "kind": "check_run", "name": "external-release-gate",
        "provider": "Jenkins", "status": "completed", "conclusion": "timed_out",
        "sha": pr.head_sha, "url": "https://ci.example.test/runs/99",
    }])
    monkeypatch.setattr(github_module.github_client, "get_commit_statuses", lambda *args: [])
    monkeypatch.setattr(github_module.github_client, "get_check_suites", lambda *args: [])

    pipeline.process_ci_refresh_job(db_session, {
        "repository_id": repo.id, "pull_request_id": pr.id,
        "installation_id": repo.installation_id, "head_sha": pr.head_sha,
        "reason": "webhook",
    })

    revision = db_session.query(m.CIRevision).filter(m.CIRevision.head_sha == pr.head_sha).one()
    notify_jobs = db_session.query(m.BackgroundJob).filter(
        m.BackgroundJob.job_type == "CI_NOTIFY"
    ).all()
    assert revision.state == "FAILED"
    assert len(notify_jobs) == 0


def test_partial_ci_api_failure_preserves_previously_discovered_executions(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    revision = m.CIRevision(
        repository_id=repo.id,
        pull_request_id=pr.id,
        head_sha=pr.head_sha,
        state="IN_PROGRESS",
    )
    db_session.add(revision)
    db_session.flush()
    existing = m.CIExecution(
        ci_revision_id=revision.id,
        identity="check_run:existing",
        kind="check_run",
        name="build",
        status="in_progress",
        head_sha=pr.head_sha,
    )
    db_session.add(existing)
    db_session.commit()
    monkeypatch.setattr(
        pipeline,
        "_fetch_ci_checks",
        lambda *args, **kwargs: ([], [], ["check_runs: GitHub returned 503"]),
    )

    pipeline.process_ci_refresh_job(db_session, {
        "repository_id": repo.id,
        "pull_request_id": pr.id,
        "installation_id": repo.installation_id,
        "head_sha": pr.head_sha,
        "reason": "webhook",
    })

    db_session.refresh(revision)
    assert revision.state == "UNAVAILABLE"
    assert db_session.query(m.CIExecution).filter(
        m.CIExecution.ci_revision_id == revision.id,
        m.CIExecution.identity == "check_run:existing",
    ).one().status == "in_progress"
