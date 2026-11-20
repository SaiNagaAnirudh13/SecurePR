"""Safety-gate tests for GitHub's protected auto-merge flow."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models import models as m
from app.services import auto_merge

POLICY = {
    "blocked_paths": [],
    "human_review_required_paths": [],
    "max_files_for_auto_approval": 25,
    "disallowed_authors": [],
}
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
        is_auto_merge_enabled=True,
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
    )
    db_session.add(pr)
    db_session.flush()
    review = m.Review(
        pull_request_id=pr.id,
        head_sha="head1",
        decision=m.DecisionStatus.APPROVED,
        overall_score=20,
        risk_category="LOW",
        github_action_confirmed=True,
    )
    db_session.add(review)
    db_session.commit()
    return repo, pr, review


def _enable(monkeypatch, *, low_risk_only=True):
    monkeypatch.setattr(
        auto_merge,
        "get_settings",
        lambda: SimpleNamespace(
            AUTO_MERGE_ENABLED=True,
            AUTO_MERGE_LOW_RISK_ONLY=low_risk_only,
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


def _attempt(db_session, repo, pr, review, policy=POLICY, author_threshold=39):
    return auto_merge.attempt(
        db=db_session,
        repo=repo,
        pr=pr,
        review=review,
        installation_id="99",
        policy_doc=policy,
        findings=[],
        author_eligible=True,
        author_threshold=author_threshold,
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


def test_global_or_repository_opt_out_never_calls_github(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    called = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_pull_request",
        lambda *args: called.append(True),
    )
    repo.is_auto_merge_enabled = False

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "DISABLED_REPOSITORY"
    assert called == []

    repo.is_auto_merge_enabled = True
    monkeypatch.setattr(
        auto_merge,
        "get_settings",
        lambda: SimpleNamespace(
            AUTO_MERGE_ENABLED=False,
            AUTO_MERGE_LOW_RISK_ONLY=True,
            AUTO_MERGE_METHOD="SQUASH",
        ),
    )
    assert _attempt(db_session, repo, pr, review).status == "DISABLED_GLOBAL"
    assert called == []


def test_policy_and_risk_eligibility_are_enforced(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch, low_risk_only=False)
    _github_ok(monkeypatch)
    review.risk_category = "MEDIUM"
    review.overall_score = 45

    result = _attempt(db_session, repo, pr, review)
    assert result.status == "NOT_ELIGIBLE"
    assert "policy" in result.reason

    policy = {**POLICY, "auto_merge_allowed_risk_categories": ["LOW", "MEDIUM"]}
    assert (
        _attempt(db_session, repo, pr, review, policy, author_threshold=60).status
        == "AUTO_MERGE_ENABLED"
    )


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
