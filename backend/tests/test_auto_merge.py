"""Safety-gate tests for persisted dashboard-authorized GitHub auto-merge."""

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models import models as m
from app.services import auto_merge

CHECKS = [
    {
        "kind": "check_run",
        "name": name,
        "status": "completed",
        "conclusion": "success",
    }
    for name in ("build", "custom-provider-check")
]


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'auto_merge.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()


def _objects(db_session):
    repo = m.Repository(
        github_repo_id="10",
        owner_login="octo",
        name="widgets",
        full_name="octo/widgets",
        installation_id="99",
    )
    db_session.add(repo)
    db_session.flush()
    pr = m.PullRequest(
        repository_id=repo.id,
        github_pr_number=4,
        author_login="octocat",
        head_sha="head1",
        base_branch="main",
        state="open",
        ai_state="AI_COMPLETED",
    )
    db_session.add(pr)
    db_session.flush()
    user = m.User(github_login="octocat")
    db_session.add(user)
    db_session.flush()
    db_session.add(
        m.AutoApprovalEligibility(
            repository_id=repo.id,
            user_id=user.id,
            is_enabled=True,
            max_risk_threshold=39,
        )
    )
    review = m.Review(
        pull_request_id=pr.id,
        head_sha="head1",
        decision=m.DecisionStatus.APPROVED,
        ai_state="AI_COMPLETED",
        overall_score=20,
        risk_category="LOW",
        github_action_confirmed=True,
        analysis_completed_at=datetime(2026, 1, 1),
    )
    db_session.add(review)
    db_session.commit()
    return repo, pr, review


def _enable(monkeypatch):
    monkeypatch.setattr(
        auto_merge,
        "get_settings",
        lambda: SimpleNamespace(
            AUTO_MERGE_METHOD="SQUASH",
        ),
    )


def _github_ok(monkeypatch, *, mergeable_state="clean", review_decision="APPROVED"):
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_pull_request",
        lambda *args: {
            "state": "open",
            "mergeable": True,
            "mergeable_state": mergeable_state,
            "head_sha": "head1",
            "base_ref": "main",
        },
    )
    monkeypatch.setattr(
        auto_merge.github_client, "get_check_runs", lambda *args: CHECKS
    )
    monkeypatch.setattr(
        auto_merge.github_client, "get_commit_statuses", lambda *args: []
    )
    monkeypatch.setattr(
        auto_merge.github_client, "get_required_status_checks", lambda *args: []
    )
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_required_pull_request_reviews",
        lambda *args: {
            "required_approving_review_count": 1,
            "dismiss_stale_reviews": True,
        },
    )
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_auto_merge_capabilities",
        lambda *args: {
            "succeeded": True,
            "status_code": 200,
            "payload": {
                "repository": {
                    "autoMergeAllowed": True,
                    "mergeCommitAllowed": True,
                    "squashMergeAllowed": True,
                    "rebaseMergeAllowed": True,
                    "pullRequest": {
                        "id": "PR_node_id",
                        "state": "OPEN",
                        "isDraft": False,
                        "headRefOid": "head1",
                        "reviewDecision": review_decision,
                        "autoMergeRequest": None,
                    },
                },
            },
        },
    )
    monkeypatch.setattr(
        auto_merge.github_client,
        "disable_pull_request_auto_merge",
        lambda *args: {"succeeded": True, "status_code": 200, "payload": {}},
    )
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: {
            "succeeded": True,
            "status_code": 200,
            "payload": {
                "enablePullRequestAutoMerge": {
                    "pullRequest": {
                        "autoMergeRequest": {
                            "enabledAt": "2026-01-01T00:00:00Z",
                            "mergeMethod": "SQUASH",
                        },
                    },
                },
            },
        },
    )


def _attempt(db_session, repo, pr, review):
    return auto_merge.attempt(
        db=db_session,
        repo=repo,
        pr=pr,
        review=review,
        installation_id="99",
        findings=[],
    )


def test_approved_low_risk_requests_github_auto_merge_and_waits_for_confirmation(
    db_session, monkeypatch
):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "AUTO_MERGE_ENABLED"
    assert result.merge_commit_sha is None
    assert "not yet merged" in result.reason


@pytest.mark.parametrize(
    "ai_state",
    ["AI_PENDING", "AI_RUNNING", "AI_FAILED", "AI_UNAVAILABLE", "AI_INVALID_RESPONSE"],
)
def test_auto_merge_is_blocked_without_completed_ai_analysis(
    db_session, monkeypatch, ai_state
):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    pr.ai_state = ai_state
    review.ai_state = ai_state
    called = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_pull_request",
        lambda *args: called.append(True),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "NOT_ELIGIBLE"
    assert called == []


def test_auto_merge_is_blocked_when_review_head_sha_is_stale(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    pr.head_sha = "new-head"
    called = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_pull_request",
        lambda *args: called.append(True),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "NOT_ELIGIBLE"
    assert called == []


def test_saved_dashboard_opt_out_never_calls_github(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    called = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_pull_request",
        lambda *args: called.append(True),
    )
    eligibility = db_session.query(m.AutoApprovalEligibility).one()
    eligibility.is_enabled = False

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "DISABLED_DASHBOARD_CONFIGURATION"
    assert called == []

    eligibility.is_enabled = True
    eligibility.max_risk_threshold = 39
    db_session.delete(eligibility)
    assert (
        _attempt(db_session, repo, pr, review).status
        == "DASHBOARD_CONFIGURATION_MISSING"
    )
    assert called == []


def test_saved_threshold_is_enforced(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    review.overall_score = 45
    eligibility = db_session.query(m.AutoApprovalEligibility).one()
    eligibility.max_risk_threshold = 39

    result = _attempt(db_session, repo, pr, review)
    assert result.status == "NOT_ELIGIBLE"
    assert "threshold" in result.reason

    eligibility.max_risk_threshold = 60
    assert _attempt(db_session, repo, pr, review).status == "AUTO_MERGE_ENABLED"


def test_repository_ownership_mismatch_fails_closed(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    repo.owner_login = "different-owner"
    called = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_pull_request",
        lambda *args: called.append(True),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "DASHBOARD_CONFIGURATION_MISSING"
    assert called == []


def test_auto_merge_requires_completed_review_timestamp(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    review.analysis_completed_at = None
    called = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_pull_request",
        lambda *args: called.append(True),
    )

    assert _attempt(db_session, repo, pr, review).status == "NOT_ELIGIBLE"
    assert called == []


def test_latest_saved_threshold_is_rechecked_before_github_mutation(
    db_session, monkeypatch
):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    original_capabilities = auto_merge.github_client.get_auto_merge_capabilities
    requests = []

    def lower_threshold(*args):
        eligibility = db_session.query(m.AutoApprovalEligibility).one()
        eligibility.max_risk_threshold = 10
        db_session.commit()
        return original_capabilities(*args)

    monkeypatch.setattr(
        auto_merge.github_client, "get_auto_merge_capabilities", lower_threshold
    )
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: requests.append(args),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "NOT_ELIGIBLE"
    assert "latest saved dashboard risk threshold" in result.reason
    assert requests == []


def test_policy_revision_change_during_activation_cancels_request(
    db_session, monkeypatch
):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    cancellations = []

    def revoke_during_activation(*_args):
        eligibility = db_session.query(m.AutoApprovalEligibility).one()
        eligibility.is_enabled = False
        eligibility.policy_revision += 1
        db_session.commit()
        return {
            "succeeded": True,
            "status_code": 200,
            "payload": {
                "enablePullRequestAutoMerge": {
                    "pullRequest": {
                        "autoMergeRequest": {
                            "enabledAt": "2026-01-01T00:00:00Z",
                            "mergeMethod": "SQUASH",
                        }
                    }
                }
            },
        }

    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        revoke_during_activation,
    )
    monkeypatch.setattr(
        auto_merge.github_client,
        "disable_pull_request_auto_merge",
        lambda *args: (
            cancellations.append(args)
            or {"succeeded": True, "status_code": 200, "payload": {}}
        ),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "NOT_ELIGIBLE"
    assert "authorization changed" in result.reason
    assert cancellations == [("99", "PR_node_id")]


def test_head_change_during_final_revalidation_never_requests_auto_merge(
    db_session, monkeypatch
):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    original_get_pr = auto_merge.github_client.get_pull_request
    reads = []
    requests = []

    def change_head_on_final_read(*args):
        reads.append(True)
        response = original_get_pr(*args)
        if len(reads) == 2:
            response["head_sha"] = "new-head"
        return response

    monkeypatch.setattr(
        auto_merge.github_client, "get_pull_request", change_head_on_final_read
    )
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: requests.append(args),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "NOT_ELIGIBLE"
    assert len(reads) == 2
    assert requests == []


def test_head_change_after_enable_is_cancelled_and_never_reported_as_enabled(
    db_session, monkeypatch
):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    original_get_pr = auto_merge.github_client.get_pull_request
    reads = []
    cancellations = []

    def change_head_after_enable(*args):
        reads.append(True)
        response = original_get_pr(*args)
        if len(reads) == 3:
            response["head_sha"] = "new-head"
        return response

    monkeypatch.setattr(
        auto_merge.github_client, "get_pull_request", change_head_after_enable
    )
    monkeypatch.setattr(
        auto_merge.github_client,
        "disable_pull_request_auto_merge",
        lambda *args: (
            cancellations.append(args)
            or {"succeeded": True, "status_code": 200, "payload": {}}
        ),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "NOT_ELIGIBLE"
    assert "request was cancelled" in result.reason
    assert len(cancellations) == 1


def test_missing_stale_review_protection_fails_closed(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_required_pull_request_reviews",
        lambda *args: (_ for _ in ()).throw(
            ValueError("Stale reviews are not dismissed.")
        ),
    )
    requests = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: requests.append(args),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "BRANCH_PROTECTION_UNAVAILABLE"
    assert requests == []


def test_revoking_saved_configuration_cancels_and_audits_pending_merge(
    db_session, monkeypatch
):
    _repo, pr, review = _objects(db_session)
    review.auto_merge_status = "AUTO_MERGE_ENABLED"
    db_session.commit()
    responses = [
        {
            "succeeded": True,
            "payload": {
                "repository": {
                    "pullRequest": {
                        "id": "PR_node_id",
                        "autoMergeRequest": {"enabledAt": "now"},
                    }
                }
            },
        },
        {
            "succeeded": True,
            "payload": {
                "repository": {
                    "pullRequest": {"id": "PR_node_id", "autoMergeRequest": None}
                }
            },
        },
    ]
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_auto_merge_capabilities",
        lambda *args: responses.pop(0),
    )
    cancellations = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "disable_pull_request_auto_merge",
        lambda *args: (
            cancellations.append(args)
            or {"succeeded": True, "status_code": 200, "payload": {}}
        ),
    )

    cancelled = auto_merge.cancel_pending_auto_merge(
        installation_id="99",
        owner="octo",
        repo_name="widgets",
        pr_number=4,
        db=db_session,
        pull_request_id=pr.id,
        cancellation_status="CANCELLED_DASHBOARD_CONFIGURATION",
        cancellation_reason="Auto-approval was disabled.",
        stale_only=False,
    )

    assert cancelled is True
    assert cancellations == [("99", "PR_node_id")]
    assert review.auto_merge_status == "CANCELLED_DASHBOARD_CONFIGURATION"
    audit = db_session.query(m.AuditLog).one()
    assert audit.action == "cancel_github_auto_merge"
    assert audit.details_json["reason"] == "Auto-approval was disabled."


@pytest.mark.parametrize(
    ("records", "state"),
    [
        ([{"name": "build", "status": "in_progress"}], "CI_PENDING"),
        (
            [{"name": "build", "status": "completed", "conclusion": "failure"}],
            "CI_FAILED",
        ),
    ],
)
def test_ci_pending_or_failed_never_requests_auto_merge(
    db_session, monkeypatch, records, state
):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    monkeypatch.setattr(
        auto_merge.github_client, "get_check_runs", lambda *args: records
    )
    requests = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: requests.append(True),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == state
    assert requests == []


def test_missing_required_check_does_not_allow_auto_merge(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_required_status_checks",
        lambda *args: [{"context": "release-gate", "app_id": None}],
    )
    requests = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: requests.append(True),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "CI_PENDING"
    assert requests == []


def test_required_review_is_reported_even_when_github_merge_state_is_blocked(
    db_session, monkeypatch
):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(
        monkeypatch,
        mergeable_state="blocked",
        review_decision="REVIEW_REQUIRED",
    )
    requests = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: requests.append(True),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "REQUIRED_REVIEW_PENDING"
    assert "required review approvals" in result.reason
    assert requests == []


def test_github_repository_auto_merge_setting_is_reported(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    capabilities = auto_merge.github_client.get_auto_merge_capabilities
    response = capabilities("99", "octo", "widgets", 4)
    response["payload"]["repository"]["autoMergeAllowed"] = False
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_auto_merge_capabilities",
        lambda *args: response,
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "GITHUB_AUTO_MERGE_DISABLED"
    assert "GitHub repository settings" in result.reason


def test_missing_required_review_does_not_request_auto_merge(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    capabilities = auto_merge.github_client.get_auto_merge_capabilities
    response = capabilities("99", "octo", "widgets", 4)
    response["payload"]["repository"]["pullRequest"]["reviewDecision"] = (
        "REVIEW_REQUIRED"
    )
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_auto_merge_capabilities",
        lambda *args: response,
    )
    requests = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: requests.append(True),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "REQUIRED_REVIEW_PENDING"
    assert requests == []


def test_missing_permission_and_mutation_rejection_are_recorded(
    db_session, monkeypatch
):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_auto_merge_capabilities",
        lambda *args: {
            "succeeded": False,
            "status_code": 403,
            "payload": {"message": "Resource not accessible by integration"},
        },
    )

    result = _attempt(db_session, repo, pr, review)
    assert result.status == "PERMISSION_MISSING"
    assert result.response_status == 403

    _github_ok(monkeypatch)
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: {
            "succeeded": False,
            "status_code": 403,
            "payload": {"message": "Resource not accessible by integration"},
        },
    )
    result = _attempt(db_session, repo, pr, review)
    assert result.status == "PERMISSION_MISSING"


def test_mutation_must_confirm_enabled_at(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: {
            "succeeded": True,
            "status_code": 200,
            "payload": {"enablePullRequestAutoMerge": {"pullRequest": {}}},
        },
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "FAILED"
    assert "without confirming" in result.reason


def test_merge_method_unavailable_is_not_silently_changed(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    capabilities = auto_merge.github_client.get_auto_merge_capabilities
    response = capabilities("99", "octo", "widgets", 4)
    response["payload"]["repository"]["squashMergeAllowed"] = False
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_auto_merge_capabilities",
        lambda *args: response,
    )
    requests = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "enable_pull_request_auto_merge",
        lambda *args: requests.append(True),
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "MERGE_METHOD_UNAVAILABLE"
    assert requests == []
