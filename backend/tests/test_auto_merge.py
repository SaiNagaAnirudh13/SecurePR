"""Safety-gate tests for auto-merge; all GitHub calls are faked."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models import models as m
from app.services import auto_merge

POLICY = {"blocked_paths": [], "human_review_required_paths": [], "max_files_for_auto_approval": 25,
          "disallowed_authors": []}
CHECKS = [{"name": n, "status": "completed", "conclusion": "success"}
          for n in ("arbitrary-build-stage", "custom-provider-check")]


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'auto_merge.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    yield session
    session.close()


def _objects(db_session):
    repo = m.Repository(github_repo_id="10", owner_login="octo", name="widgets", full_name="octo/widgets",
                        installation_id="99", is_auto_merge_enabled=True)
    db_session.add(repo); db_session.flush()
    pr = m.PullRequest(repository_id=repo.id, github_pr_number=4, author_login="octocat", head_sha="head1",
                       base_branch="main", state="open")
    db_session.add(pr); db_session.flush()
    review = m.Review(pull_request_id=pr.id, head_sha="head1", decision=m.DecisionStatus.APPROVED,
                      overall_score=20, risk_category="LOW", github_action_confirmed=True)
    db_session.add(review); db_session.commit()
    return repo, pr, review


def _enable(monkeypatch):
    monkeypatch.setattr(auto_merge, "get_settings", lambda: SimpleNamespace(
        AUTO_MERGE_ENABLED=True, AUTO_MERGE_LOW_RISK_ONLY=True, AUTO_MERGE_REQUIRE_ALL_CI=True))


def _github_ok(monkeypatch):
    monkeypatch.setattr(auto_merge.github_client, "get_pull_request", lambda *a: {
        "state": "open", "mergeable": True, "mergeable_state": "clean", "head_sha": "head1", "base_ref": "main"})
    monkeypatch.setattr(auto_merge.github_client, "get_check_runs", lambda *a: CHECKS)
    monkeypatch.setattr(auto_merge.github_client, "get_commit_statuses", lambda *a: [])
    monkeypatch.setattr(auto_merge.github_client, "get_check_suites", lambda *a: [])
    monkeypatch.setattr(auto_merge.github_client, "merge_pull_request", lambda *a: {
        "succeeded": True, "status_code": 200, "payload": {"merged": True, "sha": "merge1"}})


def _attempt(db_session, repo, pr, review):
    return auto_merge.attempt(db=db_session, repo=repo, pr=pr, review=review, installation_id="99",
                              policy_doc=POLICY, findings=[], author_eligible=True, author_threshold=39)


def test_low_risk_ci_passed_merges(db_session, monkeypatch):
    repo, pr, review = _objects(db_session); _enable(monkeypatch); _github_ok(monkeypatch)
    result = _attempt(db_session, repo, pr, review)
    assert result.status == "MERGED" and result.merge_commit_sha == "merge1"


def test_disabled_never_calls_github(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    monkeypatch.setattr(auto_merge, "get_settings", lambda: SimpleNamespace(
        AUTO_MERGE_ENABLED=False, AUTO_MERGE_LOW_RISK_ONLY=True, AUTO_MERGE_REQUIRE_ALL_CI=True))
    assert _attempt(db_session, repo, pr, review).status == "DISABLED_GLOBAL"


def test_repository_opt_in_is_required(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    _github_ok(monkeypatch)
    repo.is_auto_merge_enabled = False

    assert _attempt(db_session, repo, pr, review).status == "DISABLED_REPOSITORY"


def test_medium_or_high_findings_never_merge(db_session, monkeypatch):
    repo, pr, review = _objects(db_session); _enable(monkeypatch); _github_ok(monkeypatch)
    review.risk_category = "MEDIUM"
    assert _attempt(db_session, repo, pr, review).status == "NOT_ELIGIBLE"
    review.risk_category = "LOW"
    result = auto_merge.attempt(db=db_session, repo=repo, pr=pr, review=review, installation_id="99",
                                policy_doc=POLICY, findings=[{"severity": "HIGH"}], author_eligible=True,
                                author_threshold=39)
    assert result.status == "NOT_ELIGIBLE"


def test_risk_categories_require_explicit_repository_policy_permission(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    monkeypatch.setattr(auto_merge, "get_settings", lambda: SimpleNamespace(
        AUTO_MERGE_ENABLED=True, AUTO_MERGE_LOW_RISK_ONLY=False, AUTO_MERGE_REQUIRE_ALL_CI=True))
    _github_ok(monkeypatch)
    review.risk_category = "MEDIUM"
    review.overall_score = 45

    def attempt(policy_doc):
        return auto_merge.attempt(
            db=db_session,
            repo=repo,
            pr=pr,
            review=review,
            installation_id="99",
            policy_doc=policy_doc,
            findings=[],
            author_eligible=True,
            author_threshold=60,
        )

    for category in ("MEDIUM", "HIGH"):
        review.risk_category = category
        result = attempt(POLICY)
        assert result.status == "NOT_ELIGIBLE"
        assert "policy" in result.reason

    review.risk_category = "MEDIUM"
    policy = {**POLICY, "auto_merge_allowed_risk_categories": ["LOW", "MEDIUM"]}
    result = attempt(policy)
    assert result.status == "MERGED"


def test_conflict_or_ci_pending_requires_manual_merge(db_session, monkeypatch):
    repo, pr, review = _objects(db_session); _enable(monkeypatch); _github_ok(monkeypatch)
    monkeypatch.setattr(auto_merge.github_client, "get_pull_request", lambda *a: {
        "state": "open", "mergeable": False, "mergeable_state": "dirty", "head_sha": "head1", "base_ref": "main"})
    assert _attempt(db_session, repo, pr, review).status == "MANUAL_REQUIRED"


def test_in_progress_ci_never_allows_auto_merge(db_session, monkeypatch):
    repo, pr, review = _objects(db_session)
    _enable(monkeypatch)
    monkeypatch.setattr(auto_merge, "get_settings", lambda: SimpleNamespace(
        AUTO_MERGE_ENABLED=True, AUTO_MERGE_LOW_RISK_ONLY=True, AUTO_MERGE_REQUIRE_ALL_CI=False))
    _github_ok(monkeypatch)
    monkeypatch.setattr(
        auto_merge.github_client,
        "get_check_runs",
        lambda *args: [
            {"name": "arbitrary-check", "status": "in_progress", "conclusion": None}
        ],
    )
    merged = []
    monkeypatch.setattr(
        auto_merge.github_client,
        "merge_pull_request",
        lambda *args: merged.append(True) or {"succeeded": True, "status_code": 200, "payload": {"merged": True}},
    )

    result = _attempt(db_session, repo, pr, review)

    assert result.status == "MANUAL_REQUIRED"
    assert "in_progress" in result.reason
    assert merged == []


def test_circuit_breaker_requires_manual_merge(db_session, monkeypatch):
    repo, pr, review = _objects(db_session); _enable(monkeypatch); _github_ok(monkeypatch)
    monkeypatch.setattr(auto_merge.circuit_breaker, "is_tripped", lambda *a: (True, "emergency stop"))
    result = _attempt(db_session, repo, pr, review)
    assert result.status == "MANUAL_REQUIRED" and "Circuit breaker" in result.reason


def test_github_merge_failure_is_reported(db_session, monkeypatch):
    repo, pr, review = _objects(db_session); _enable(monkeypatch); _github_ok(monkeypatch)
    monkeypatch.setattr(auto_merge.github_client, "merge_pull_request", lambda *a: {
        "succeeded": False, "status_code": 403, "payload": {"message": "Resource not accessible by integration"}})
    result = _attempt(db_session, repo, pr, review)
    assert result.status == "FAILED" and "accessible" in result.reason
