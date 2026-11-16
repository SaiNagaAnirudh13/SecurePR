"""
End-to-end test of the CI-wait pipeline using a real (SQLite) DB session and
a monkeypatched GitHub client, so no network access or real credentials are
needed. Exercises: initial analysis -> WAITING_FOR_CI placeholder -> a
second analysis pass once checks complete -> final APPROVED review linked
back to the placeholder via superseded_review_id.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.api.dashboard import _ci_status_summary  # noqa: E402
from app.api.webhooks import _handle_check_event  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.integrations import github_client as gh_mod  # noqa: E402
from app.models import models as m  # noqa: E402
from app.services import pipeline  # noqa: E402


def test_sql_server_revision_lock_uses_database_application_lock():
    executed = []

    class FakeResult:
        def scalar(self):
            return 0

    class FakeConnection:
        def execute(self, statement, params):
            executed.append((str(statement), params))
            return FakeResult()

        def close(self):
            pass

    connection = FakeConnection()
    bind = SimpleNamespace(
        dialect=SimpleNamespace(name="mssql"),
        connect=lambda: connection,
    )
    fake_session = SimpleNamespace(get_bind=lambda: bind)

    with pipeline._revision_finalization_lock(fake_session, "repo-id", 7):
        assert len(executed) == 1

    assert len(executed) == 2
    assert "sp_getapplock" in executed[0][0]
    assert "sp_releaseapplock" in executed[1][0]
    assert executed[0][1]["resource"] == executed[1][1]["resource"]


def test_postgresql_revision_lock_uses_session_advisory_lock():
    executed = []

    class FakeResult:
        def scalar(self):
            return True

    class FakeConnection:
        def execute(self, statement, params=None):
            executed.append((str(statement), params))
            return FakeResult()

        def close(self):
            pass

    connection = FakeConnection()
    bind = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        connect=lambda: connection,
    )
    fake_session = SimpleNamespace(get_bind=lambda: bind)

    with pipeline._revision_finalization_lock(fake_session, "repo-id", 7):
        assert len(executed) == 2

    assert len(executed) == 3
    assert "pg_advisory_lock" in executed[1][0]
    assert "pg_advisory_unlock" in executed[2][0]
    assert executed[1][1]["resource_key"] == executed[2][1]["resource_key"]


@pytest.fixture()
def db_session(tmp_path):
    db_path = tmp_path / "ci_wait_test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, future=True)
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture()
def repo_and_pr(db_session):
    repo = m.Repository(github_repo_id="1", owner_login="octo", name="widgets",
                         full_name="octo/widgets", installation_id="999")
    db_session.add(repo)
    db_session.flush()
    pr = m.PullRequest(repository_id=repo.id, github_pr_number=7, title="Add feature",
                        author_login="octocat", head_sha="abc123", base_branch="main",
                        head_branch="feature", state="open")
    db_session.add(pr)
    db_session.commit()
    return repo, pr


def _patch_github(monkeypatch, changed_files, check_runs_sequence, mandatory_checks=None):
    """check_runs_sequence: a list of "snapshots" returned by successive
    get_check_runs() calls, to simulate CI progressing over time."""
    calls = {"n": 0}

    def fake_get_pr_files(*a, **kw):
        return changed_files

    def fake_get_check_runs(*a, **kw):
        idx = min(calls["n"], len(check_runs_sequence) - 1)
        calls["n"] += 1
        return check_runs_sequence[idx]

    posted_reviews = []

    def fake_post_review(*a, **kw):
        posted_reviews.append({"body": a[4], "event": a[5]})
        return {"succeeded": True, "status_code": 200, "payload": {}}

    def fake_notify_owner(*a, **kw):
        return {"succeeded": True, "status_code": 200}

    monkeypatch.setattr(gh_mod.github_client, "get_pr_files", fake_get_pr_files)
    monkeypatch.setattr(gh_mod.github_client, "get_check_runs", fake_get_check_runs)
    monkeypatch.setattr(gh_mod.github_client, "get_commit_statuses", lambda *a, **kw: [])
    monkeypatch.setattr(gh_mod.github_client, "get_check_suites", lambda *a, **kw: [])
    monkeypatch.setattr(gh_mod.github_client, "get_pull_request", lambda *a, **kw: {
        "state": "open", "head_sha": "abc123", "base_ref": "main",
    })
    monkeypatch.setattr(gh_mod.github_client, "post_review", fake_post_review)
    monkeypatch.setattr(gh_mod.github_client, "notify_owner", fake_notify_owner)
    monkeypatch.setattr(pipeline.settings, "CI_DISCOVERY_STABILIZATION_SECONDS", 0)
    policy = pipeline.policy_engine.load_default_policy()
    policy["mandatory_ci_checks"] = (
        ["build", "unit-tests", "lint"] if mandatory_checks is None else mandatory_checks
    )
    policy["ci_check_aliases"] = {
        "build": ["Build / Install dependencies", "build / install dependencies"],
        "unit-tests": ["Tests / Unit tests", "tests / unit tests", "test"],
        "lint": ["Lint / Ruff", "lint / ruff"],
    }
    monkeypatch.setattr(
        pipeline.policy_engine,
        "load_repo_policy",
        lambda _repo_name: policy,
    )

    # Gemini is best-effort and should fail closed (no key configured in tests)
    from app.integrations import gemini_client as gemini_mod
    monkeypatch.setattr(gemini_mod.gemini_client, "score_dimensions", lambda *a, **kw: None)
    return posted_reviews


def test_waits_then_finalizes_once_ci_completes(db_session, repo_and_pr, monkeypatch, caplog):
    caplog.set_level("INFO", logger="securepr.pipeline")
    repo, pr = repo_and_pr
    files = [{"file_path": "app/utils.py", "additions": 2, "deletions": 1, "patch_excerpt": "+def greet(): pass"}]
    pending_checks = [{"name": "build", "status": "completed", "conclusion": "success"},
                       {"name": "unit-tests", "status": "in_progress", "conclusion": None},
                       {"name": "lint", "status": "queued", "conclusion": None}]
    completed_checks = [{"name": "build", "status": "completed", "conclusion": "success"},
                         {"name": "unit-tests", "status": "completed", "conclusion": "success"},
                         {"name": "lint", "status": "completed", "conclusion": "success"}]
    # Three snapshots: still-pending, still-pending (idempotency check), then completed.
    posted_reviews = _patch_github(monkeypatch, files, [pending_checks, pending_checks, completed_checks])

    # First pass: mandatory checks still running -> WAITING_FOR_CI placeholder
    review1 = pipeline.run_analysis(db_session, repo, pr, "999")
    assert review1.decision == m.DecisionStatus.WAITING_FOR_CI
    assert review1.is_ci_wait_placeholder is True
    assert set(review1.ci_wait_pending_checks) == {"unit-tests", "lint"}

    # Calling again before CI has moved on should reuse the SAME placeholder,
    # not create a second one (idempotency / no comment spam).
    review1_again = pipeline.run_analysis(db_session, repo, pr, "999")
    assert review1_again.id == review1.id

    # CI finishes -> next pass finalizes and links back to the placeholder
    review2 = pipeline.run_analysis(db_session, repo, pr, "999")
    assert review2.decision in (m.DecisionStatus.APPROVED, m.DecisionStatus.MANUAL_REVIEW)
    assert review2.is_ci_wait_placeholder is False
    assert review2.superseded_review_id == review1.id
    assert [review["event"] for review in posted_reviews] == ["COMMENT", "COMMENT"]
    assert any(
        getattr(record, "timing_event", None) == "analysis"
        and getattr(record, "stage", None) == "total"
        and record.duration_ms >= 0
        for record in caplog.records
    )
    assert "Waiting for CI" in posted_reviews[0]["body"]
    assert "SecurePR Review" in posted_reviews[1]["body"]

    # GitHub retries completed deliveries.  Once finalized, an identical
    # analysis request must reuse the final row and must not post again.
    review2_again = pipeline.run_analysis(db_session, repo, pr, "999")
    assert review2_again.id == review2.id
    assert len(posted_reviews) == 2

    all_reviews = db_session.query(m.Review).filter(m.Review.pull_request_id == pr.id).all()
    assert len(all_reviews) == 2  # exactly one placeholder + one final, no duplicates


def test_new_check_during_finalization_restarts_stabilization(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    files = [{"file_path": "README.md", "additions": 1, "deletions": 0, "patch_excerpt": ""}]
    initial = [{
        "identity": "check:1",
        "name": "Build",
        "provider": "GitHub Actions",
        "sha": "abc123",
        "status": "completed",
        "conclusion": "success",
    }]
    with_new_failure = initial + [{
        "identity": "check:2",
        "name": "Security Scan",
        "provider": "GitHub Actions",
        "sha": "abc123",
        "status": "completed",
        "conclusion": "failure",
    }]
    posted_reviews = _patch_github(
        monkeypatch,
        files,
        [initial, with_new_failure, with_new_failure, with_new_failure],
        mandatory_checks=[],
    )

    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    assert waiting.decision == m.DecisionStatus.WAITING_FOR_CI
    assert waiting.is_ci_wait_placeholder is True
    assert [review["event"] for review in posted_reviews] == ["COMMENT"]

    final = pipeline.run_analysis(db_session, repo, pr, "999")
    assert final.decision != m.DecisionStatus.WAITING_FOR_CI
    assert final.is_ci_wait_placeholder is False
    ci_evidence = (
        db_session.query(m.Evidence)
        .filter(m.Evidence.review_id == final.id, m.Evidence.evidence_type == "ci_result")
        .one()
    )
    ci_result = json.loads(ci_evidence.content)
    assert ci_result["state"] == "FAILURE"
    assert {check["name"] for check in ci_result["checks"]} == {"Build", "Security Scan"}
    assert len(posted_reviews) == 2


def test_timed_out_revision_verification_closes_wait_without_publishing(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    files = [{"file_path": "README.md", "additions": 1, "deletions": 0, "patch_excerpt": ""}]
    checks = [{
        "identity": "check:1",
        "name": "Build",
        "provider": "GitHub Actions",
        "sha": "abc123",
        "status": "completed",
        "conclusion": "success",
    }]
    posted_reviews = _patch_github(
        monkeypatch, files, [checks, checks], mandatory_checks=[]
    )
    pull_request_calls = {"count": 0}

    def fail_final_revision_check(*_args, **_kwargs):
        pull_request_calls["count"] += 1
        if pull_request_calls["count"] == 1:
            return {"state": "open", "head_sha": "abc123", "base_ref": "main"}
        raise RuntimeError("temporary GitHub API failure")

    monkeypatch.setattr(
        gh_mod.github_client, "get_pull_request", fail_final_revision_check
    )
    review = pipeline.run_analysis(
        db_session, repo, pr, "999", force_final=True
    )

    assert review.decision == m.DecisionStatus.MANUAL_REVIEW
    assert review.is_ci_wait_placeholder is False
    assert review.analysis_completed_at is not None
    assert len(posted_reviews) == 1
    assert "holding its final decision" in posted_reviews[0]["body"]
    ci_evidence = (
        db_session.query(m.Evidence)
        .filter(m.Evidence.review_id == review.id, m.Evidence.evidence_type == "ci_result")
        .one()
    )
    assert json.loads(ci_evidence.content)["state"] == "TIMEOUT"


def test_completed_check_event_finalizes_waiting_review_once(db_session, repo_and_pr, monkeypatch):
    """The check_run/check_suite fast path re-evaluates the matching head SHA.

    Calling it twice models the normal pair of GitHub completion events (or a
    retried delivery) and verifies that it produces exactly one final comment.
    """
    repo, pr = repo_and_pr
    files = [{"file_path": "README.md", "additions": 2, "deletions": 1, "patch_excerpt": ""}]
    pending_checks = [{"name": "build", "status": "completed", "conclusion": "success"},
                      {"name": "unit-tests", "status": "in_progress", "conclusion": None},
                      {"name": "lint", "status": "completed", "conclusion": "success"}]
    completed_checks = [{"name": "build", "status": "completed", "conclusion": "success"},
                        {"name": "unit-tests", "status": "completed", "conclusion": "success"},
                        {"name": "lint", "status": "completed", "conclusion": "success"}]
    posted_reviews = _patch_github(monkeypatch, files, [pending_checks, completed_checks])

    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    assert waiting.decision == m.DecisionStatus.WAITING_FOR_CI

    # The handler owns its own session in production; point it at this test DB.
    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)
    pipeline.handle_check_event_finalization(repo.id, pr.head_sha, "999")
    pipeline.handle_check_event_finalization(repo.id, pr.head_sha, "999")

    db_session.expire_all()
    reviews = (db_session.query(m.Review).filter(m.Review.pull_request_id == pr.id)
               .order_by(m.Review.created_at).all())
    assert [review.decision for review in reviews] == [m.DecisionStatus.WAITING_FOR_CI, m.DecisionStatus.MANUAL_REVIEW]
    assert reviews[-1].superseded_review_id == waiting.id
    assert [review["event"] for review in posted_reviews] == ["COMMENT", "COMMENT"]
    assert "found 1 changed file(s)" in posted_reviews[0]["body"]


@pytest.mark.parametrize("finalization_path", ["webhook", "polling"])
def test_production_check_names_wait_for_lint_then_finalize_once(
    finalization_path, db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    files = [{
        "file_path": "README.md",
        "additions": 1,
        "deletions": 0,
        "patch_excerpt": "",
    }]
    production_checks = [
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "test", "status": "completed", "conclusion": "success"},
        {"name": "dependency-audit", "status": "completed", "conclusion": "success"},
        {"name": "secret-scan", "status": "completed", "conclusion": "success"},
    ]
    completed_checks = production_checks + [
        {"name": "lint", "status": "completed", "conclusion": "success"},
    ]
    posted_reviews = _patch_github(
        monkeypatch, files, [production_checks, completed_checks]
    )

    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    assert waiting.decision == m.DecisionStatus.WAITING_FOR_CI
    assert "lint" in waiting.ci_wait_pending_checks

    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)
    if finalization_path == "webhook":
        pipeline.handle_check_event_finalization(
            repo.id, pr.head_sha, "999", "check_run"
        )
        pipeline.handle_check_event_finalization(
            repo.id, pr.head_sha, "999", "check_suite"
        )
    else:
        monkeypatch.setattr(pipeline.settings, "CI_WAIT_POLL_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(pipeline.settings, "CI_WAIT_TIMEOUT_SECONDS", 5)
        pipeline._ci_wait_poll_loop(repo.id, pr.id, "999", pr.head_sha)

    db_session.expire_all()
    reviews = db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == pr.head_sha,
        m.Review.is_ci_wait_placeholder.is_(False),
    ).all()
    final_reviews = [review for review in reviews if not review.is_ci_wait_placeholder]
    assert len(final_reviews) == 1
    assert final_reviews[0].superseded_review_id == waiting.id
    assert [review["event"] for review in posted_reviews] == ["COMMENT", "COMMENT"]


@pytest.mark.parametrize("event_name", ["check_run", "check_suite"])
def test_ci_completion_webhook_finalizes_initially_pending_pr(
    event_name, db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    _patch_github(
        monkeypatch,
        [{"file_path": "app/utils.py", "additions": 1, "deletions": 0, "patch_excerpt": ""}],
        [
            [{"name": "build", "status": "in_progress", "conclusion": None}],
            [
                {"name": "build", "status": "completed", "conclusion": "success"},
                {"name": "unit-tests", "status": "completed", "conclusion": "success"},
                {"name": "lint", "status": "completed", "conclusion": "success"},
            ],
        ],
    )
    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    assert waiting.decision == m.DecisionStatus.WAITING_FOR_CI

    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)
    payload = {
        "action": "completed",
        "repository": {"id": 1, "full_name": "octo/widgets"},
        "check_run": {"head_sha": pr.head_sha, "pull_requests": [{"number": 7}]},
        "check_suite": {"head_sha": pr.head_sha},
    }
    background_tasks = BackgroundTasks()

    response = _handle_check_event(db_session, background_tasks, payload, event_name)

    assert response["status"] == "accepted"
    assert len(background_tasks.tasks) == 1
    task = background_tasks.tasks[0]
    task.func(*task.args, **task.kwargs)

    db_session.expire_all()
    reviews = db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == pr.head_sha,
    ).all()
    final_reviews = [review for review in reviews if not review.is_ci_wait_placeholder]
    assert len(final_reviews) == 1
    assert final_reviews[0].decision != m.DecisionStatus.WAITING_FOR_CI


def test_nonterminal_check_webhook_refreshes_discovery_without_duplicate_review(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    pending = [{
        "identity": "check_run:401",
        "id": 401,
        "kind": "check_run",
        "name": "Queued external workflow",
        "provider": "external-ci",
        "status": "queued",
        "conclusion": None,
        "sha": pr.head_sha,
    }]
    posted_reviews = _patch_github(
        monkeypatch, [], [pending], mandatory_checks=[]
    )
    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)
    background_tasks = BackgroundTasks()

    response = _handle_check_event(
        db_session,
        background_tasks,
        {
            "action": "created",
            "repository": {"id": 1, "full_name": "octo/widgets"},
            "check_run": {
                "head_sha": pr.head_sha,
                "pull_requests": [{"number": pr.github_pr_number}],
            },
        },
        "check_run",
    )

    assert response["status"] == "accepted"
    assert len(background_tasks.tasks) == 1
    task = background_tasks.tasks[0]
    task.func(*task.args, **task.kwargs)
    db_session.expire_all()
    reviews = db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == pr.head_sha,
    ).all()
    assert [review.id for review in reviews] == [waiting.id]
    assert len(posted_reviews) == 1


def test_check_event_before_wait_row_is_persisted_still_finalizes(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted_reviews = _patch_github(
        monkeypatch,
        [{"file_path": "README.md", "additions": 1, "deletions": 0, "patch_excerpt": ""}],
        [[
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "unit-tests", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "success"},
        ]],
    )
    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)

    pipeline.handle_check_event_finalization(repo.id, pr.head_sha, "999", "check_run")
    initial_analysis = pipeline.run_analysis(db_session, repo, pr, "999")

    db_session.expire_all()
    final_reviews = db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == pr.head_sha,
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
    ).all()
    assert len(final_reviews) == 1
    assert initial_analysis.id == final_reviews[0].id
    assert [review["event"] for review in posted_reviews] == ["COMMENT"]


def test_unrelated_commit_event_is_ignored(db_session, repo_and_pr, monkeypatch):
    repo, pr = repo_and_pr
    posted_reviews = _patch_github(monkeypatch, [], [[]])
    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)

    pipeline.handle_check_event_finalization(repo.id, "unrelated-sha", "999", "check_run")

    assert db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id
    ).count() == 0
    assert posted_reviews == []


def test_new_head_sha_ignores_old_ci_event_and_finalizes_only_current_revision(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    _patch_github(
        monkeypatch,
        [],
        [
            [{"name": "build", "status": "in_progress", "conclusion": None}],
            [{"name": "build", "status": "in_progress", "conclusion": None}],
            [
                {"name": "build", "status": "completed", "conclusion": "success"},
                {"name": "unit-tests", "status": "completed", "conclusion": "success"},
                {"name": "lint", "status": "completed", "conclusion": "success"},
            ],
        ],
    )
    old_sha = pr.head_sha
    old_wait = pipeline.run_analysis(db_session, repo, pr, "999")
    pr.head_sha = "new-head-sha"
    db_session.commit()
    monkeypatch.setattr(gh_mod.github_client, "get_pull_request", lambda *a, **kw: {
        "state": "open", "head_sha": "new-head-sha", "base_ref": "main",
    })
    new_wait = pipeline.run_analysis(db_session, repo, pr, "999")
    assert old_wait.decision == new_wait.decision == m.DecisionStatus.WAITING_FOR_CI

    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)
    background_tasks = BackgroundTasks()
    stale_response = _handle_check_event(
        db_session,
        background_tasks,
        {
            "action": "completed",
            "repository": {"id": 1, "full_name": "octo/widgets"},
            "check_run": {"head_sha": old_sha},
        },
        "check_run",
    )
    assert stale_response["status"] == "ignored"
    assert not background_tasks.tasks

    current_response = _handle_check_event(
        db_session,
        background_tasks,
        {
            "action": "completed",
            "repository": {"id": 1, "full_name": "octo/widgets"},
            "check_suite": {"head_sha": "new-head-sha"},
        },
        "check_suite",
    )
    assert current_response["status"] == "accepted"
    task = background_tasks.tasks[0]
    task.func(*task.args, **task.kwargs)

    db_session.expire_all()
    old_finals = db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == old_sha,
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
    ).count()
    new_finals = db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == "new-head-sha",
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
    ).count()
    assert old_finals == 0
    assert new_finals == 1


def test_repeated_and_concurrent_completion_events_publish_once(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted_reviews = _patch_github(
        monkeypatch,
        [{"file_path": "app/utils.py", "additions": 1, "deletions": 0, "patch_excerpt": ""}],
        [[
            {"name": "build", "status": "in_progress", "conclusion": None},
        ]],
    )
    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    assert waiting.decision == m.DecisionStatus.WAITING_FOR_CI

    completed = [
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "unit-tests", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "completed", "conclusion": "success"},
    ]
    monkeypatch.setattr(gh_mod.github_client, "get_check_runs", lambda *a, **kw: completed)
    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)
    repository_id = repo.id
    pull_request_id = pr.id
    head_sha = pr.head_sha

    def deliver(event_name):
        pipeline.handle_check_event_finalization(
            repository_id, head_sha, "999", event_name
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(deliver, ["check_run", "check_suite"]))
    deliver("check_run")

    db_session.expire_all()
    reviews = db_session.query(m.Review).filter(
        m.Review.pull_request_id == pull_request_id,
        m.Review.head_sha == head_sha,
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
        m.Review.is_reevaluation == False,  # noqa: E712
    ).all()
    assert len(reviews) == 1
    assert db_session.query(m.Notification).filter(
        m.Notification.review_id == reviews[0].id
    ).count() == 1
    assert [review["event"] for review in posted_reviews] == ["COMMENT", "COMMENT"]


def test_ci_status_webhook_finalizes_current_head(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    _patch_github(
        monkeypatch,
        [{"file_path": "README.md", "additions": 1, "deletions": 0, "patch_excerpt": ""}],
        [
            [{"name": "build", "status": "in_progress", "conclusion": None}],
            [
                {"name": "build", "status": "completed", "conclusion": "success"},
                {"name": "unit-tests", "status": "completed", "conclusion": "success"},
                {"name": "lint", "status": "completed", "conclusion": "success"},
            ],
        ],
    )
    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    assert waiting.decision == m.DecisionStatus.WAITING_FOR_CI

    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)
    background_tasks = BackgroundTasks()
    response = _handle_check_event(
        db_session,
        background_tasks,
        {
            "repository": {"id": 1, "full_name": "octo/widgets"},
            "sha": pr.head_sha,
            "state": "success",
        },
        "status",
    )

    assert response["status"] == "accepted"
    assert len(background_tasks.tasks) == 1
    task = background_tasks.tasks[0]
    task.func(*task.args, **task.kwargs)

    db_session.expire_all()
    final_reviews = db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == pr.head_sha,
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
    ).all()
    assert len(final_reviews) == 1


def test_changed_file_fetch_failure_is_not_reported_as_an_empty_pr(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted_reviews = _patch_github(
        monkeypatch,
        [],
        [[
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "unit-tests", "status": "completed", "conclusion": "success"},
            {"name": "lint", "status": "completed", "conclusion": "success"},
        ]],
    )

    def failed_file_fetch(*args, **kwargs):
        raise RuntimeError("GitHub API unavailable")

    monkeypatch.setattr(gh_mod.github_client, "get_pr_files", failed_file_fetch)
    review = pipeline.run_analysis(db_session, repo, pr, "999")

    assert review.decision == m.DecisionStatus.MANUAL_REVIEW
    assert review.risk_category == "NOT_EVALUATED"
    assert any("could not provide" in reason for reason in review.decision_reasons)
    assert db_session.query(m.ChangedFile).filter(m.ChangedFile.review_id == review.id).count() == 0
    assert "Analysis incomplete" in posted_reviews[-1]["body"]
    assert "Changed Files (0 files" not in posted_reviews[-1]["body"]


def test_waiting_comment_does_not_claim_zero_files_when_github_fetch_fails(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted_reviews = _patch_github(
        monkeypatch,
        [],
        [[{"name": "build", "status": "in_progress", "conclusion": None}]],
    )
    monkeypatch.setattr(
        gh_mod.github_client,
        "get_pr_files",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("GitHub API unavailable")),
    )

    review = pipeline.run_analysis(db_session, repo, pr, "999")

    assert review.decision == m.DecisionStatus.WAITING_FOR_CI
    assert "could not retrieve the changed-file list" in posted_reviews[0]["body"]
    assert "found 0 changed file(s)" not in posted_reviews[0]["body"]


def test_polling_fallback_racing_webhook_publishes_once(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted_reviews = _patch_github(
        monkeypatch,
        [{"file_path": "README.md", "additions": 1, "deletions": 0, "patch_excerpt": ""}],
        [[{"name": "build", "status": "in_progress", "conclusion": None}]],
    )
    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    assert waiting.decision == m.DecisionStatus.WAITING_FOR_CI

    completed = [
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "unit-tests", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "completed", "conclusion": "success"},
    ]
    monkeypatch.setattr(gh_mod.github_client, "get_check_runs", lambda *a, **kw: completed)
    factory = sessionmaker(bind=db_session.get_bind(), future=True)
    monkeypatch.setattr(pipeline, "get_session_factory", lambda: factory)
    monkeypatch.setattr(pipeline.settings, "CI_WAIT_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(pipeline.settings, "CI_WAIT_TIMEOUT_SECONDS", 5)

    with ThreadPoolExecutor(max_workers=2) as executor:
        poll = executor.submit(
            pipeline._ci_wait_poll_loop, repo.id, pr.id, "999", pr.head_sha
        )
        event = executor.submit(
            pipeline.handle_check_event_finalization,
            repo.id,
            pr.head_sha,
            "999",
            "check_run",
        )
        event.result(timeout=3)
        poll.result(timeout=3)

    db_session.expire_all()
    finals = db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == pr.head_sha,
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
        m.Review.is_reevaluation == False,  # noqa: E712
    ).all()
    assert len(finals) == 1
    assert db_session.query(m.Notification).filter(
        m.Notification.review_id == finals[0].id
    ).count() == 1
    assert [review["event"] for review in posted_reviews] == ["COMMENT", "COMMENT"]


def test_force_final_routes_to_manual_review_not_silent_approval(db_session, repo_and_pr, monkeypatch):
    """Simulates the polling timeout path: checks are still pending, but
    force_final=True must never result in a silent APPROVED."""
    repo, pr = repo_and_pr
    files = [{"file_path": "README.md", "additions": 2, "deletions": 1, "patch_excerpt": ""}]
    pending_checks = [{"name": "build", "status": "in_progress", "conclusion": None}]
    _patch_github(monkeypatch, files, [pending_checks])

    review = pipeline.run_analysis(db_session, repo, pr, "999", force_final=True)
    assert review.decision in (m.DecisionStatus.MANUAL_REVIEW, m.DecisionStatus.BLOCKED)
    assert review.decision != m.DecisionStatus.APPROVED
    assert any("Timed out" in r for r in review.decision_reasons)


def test_critical_finding_waits_for_required_ci_before_final_block(db_session, repo_and_pr, monkeypatch):
    repo, pr = repo_and_pr
    files = [{"file_path": "infra/secrets/db.pem", "additions": 1, "deletions": 0, "patch_excerpt": ""}]
    pending_checks = [{"name": "build", "status": "in_progress", "conclusion": None}]
    _patch_github(monkeypatch, files, [pending_checks])

    review = pipeline.run_analysis(db_session, repo, pr, "999")
    assert review.decision == m.DecisionStatus.WAITING_FOR_CI
    assert review.is_ci_wait_placeholder is True


def test_no_checks_reported_is_waiting_not_a_final_decision(db_session, repo_and_pr, monkeypatch):
    """The GitHub Actions startup gap is the regression this workflow fixes."""
    repo, pr = repo_and_pr
    posted_reviews = _patch_github(monkeypatch, [], [[], []], mandatory_checks=[])
    monkeypatch.setattr(pipeline.settings, "CI_DISCOVERY_STABILIZATION_SECONDS", 60)

    first = pipeline.run_analysis(db_session, repo, pr, "999")
    second = pipeline.run_analysis(db_session, repo, pr, "999")

    assert first.decision == m.DecisionStatus.WAITING_FOR_CI
    assert second.id == first.id
    assert len(posted_reviews) == 1
    assert "Waiting for CI" in posted_reviews[0]["body"]


@pytest.mark.parametrize("check_count", [1, 4])
def test_dynamic_check_names_and_counts_finalize_without_name_configuration(
    check_count, db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    checks = [
        {
            "identity": f"check_run:{index}",
            "id": index,
            "kind": "check_run",
            "name": f"Workflow {index} / verification",
            "provider": f"provider-{index}",
            "status": "completed",
            "conclusion": "success",
            "url": f"https://github.com/octo/widgets/runs/{index}",
            "sha": pr.head_sha,
        }
        for index in range(check_count)
    ]
    _patch_github(
        monkeypatch, [], [checks], mandatory_checks=[]
    )

    review = pipeline.run_analysis(db_session, repo, pr, "999")
    evidence = db_session.query(m.Evidence).filter(
        m.Evidence.review_id == review.id,
        m.Evidence.evidence_type == "ci_result",
    ).one()
    result = json.loads(evidence.content)
    dashboard = _ci_status_summary(db_session, review.id)

    assert review.decision != m.DecisionStatus.WAITING_FOR_CI
    assert result["state"] == "SUCCESS"
    assert len(result["checks"]) == check_count
    assert dashboard["label"] == "passed"
    assert len(dashboard["checks"]) == check_count
    assert {check["name"] for check in result["checks"]} == {
        f"Workflow {index} / verification" for index in range(check_count)
    }
    assert all(check["sha"] == pr.head_sha for check in result["checks"])


def test_delayed_check_creation_resets_discovery_stabilization(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    delayed_check = {
        "identity": "check_run:202",
        "id": 202,
        "kind": "check_run",
        "name": "Delayed dependency audit",
        "provider": "third-party-ci",
        "status": "completed",
        "conclusion": "success",
        "url": "https://github.com/octo/widgets/runs/202",
        "sha": pr.head_sha,
    }
    posted_reviews = _patch_github(
        monkeypatch, [], [[], [delayed_check]], mandatory_checks=[]
    )
    monkeypatch.setattr(pipeline.settings, "CI_DISCOVERY_STABILIZATION_SECONDS", 60)

    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    evidence = db_session.query(m.Evidence).filter(
        m.Evidence.review_id == waiting.id,
        m.Evidence.evidence_type == "ci_result",
    ).one()
    old_result = json.loads(evidence.content)
    old_result["last_change_at"] = (datetime.utcnow() - timedelta(seconds=30)).isoformat()
    evidence.content = json.dumps(old_result)
    db_session.commit()

    still_waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    db_session.refresh(evidence)
    updated = json.loads(evidence.content)

    assert waiting.id == still_waiting.id
    assert updated["state"] == "DISCOVERING"
    assert updated["checks"][0]["identity"] == "check_run:202"
    assert updated["last_change_at"] > old_result["last_change_at"]
    assert len(posted_reviews) == 1


def test_no_ci_after_stabilization_finalizes_as_manual_not_success(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    posted_reviews = _patch_github(
        monkeypatch, [], [[]], mandatory_checks=[]
    )

    review = pipeline.run_analysis(db_session, repo, pr, "999")
    evidence = db_session.query(m.Evidence).filter(
        m.Evidence.review_id == review.id,
        m.Evidence.evidence_type == "ci_result",
    ).one()

    assert review.decision == m.DecisionStatus.MANUAL_REVIEW
    assert json.loads(evidence.content)["state"] == "NO_CI_CONFIGURED"
    assert "absence of checks as successful CI" in review.decision_reasons[0]
    assert "No CI checks detected" in posted_reviews[-1]["body"]


def test_head_change_during_analysis_prevents_stale_revision_finalization(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    checks = [{
        "identity": "check_run:301",
        "id": 301,
        "kind": "check_run",
        "name": "Custom build",
        "provider": "custom-ci",
        "status": "completed",
        "conclusion": "success",
        "sha": pr.head_sha,
    }]
    posted_reviews = _patch_github(
        monkeypatch, [], [checks], mandatory_checks=[]
    )
    live_heads = iter(("abc123", "new-head"))
    monkeypatch.setattr(gh_mod.github_client, "get_pull_request", lambda *a, **kw: {
        "state": "open", "head_sha": next(live_heads), "base_ref": "main",
    })

    review = pipeline.run_analysis(db_session, repo, pr, "999")

    assert review is None
    assert pr.head_sha == "new-head"
    assert db_session.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
    ).count() == 0
    assert posted_reviews == []


def test_github_ci_api_failure_times_out_to_manual_review(
    db_session, repo_and_pr, monkeypatch
):
    repo, pr = repo_and_pr
    _patch_github(monkeypatch, [], [[]], mandatory_checks=[])
    monkeypatch.setattr(
        gh_mod.github_client,
        "get_check_runs",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("rate limited")),
    )

    waiting = pipeline.run_analysis(db_session, repo, pr, "999")
    assert waiting.decision == m.DecisionStatus.WAITING_FOR_CI
    assert json.loads(db_session.query(m.Evidence).filter(
        m.Evidence.review_id == waiting.id,
        m.Evidence.evidence_type == "ci_result",
    ).one().content)["state"] == "ERROR"

    final = pipeline.run_analysis(db_session, repo, pr, "999", force_final=True)
    evidence = db_session.query(m.Evidence).filter(
        m.Evidence.review_id == final.id,
        m.Evidence.evidence_type == "ci_result",
    ).one()

    assert final.decision == m.DecisionStatus.MANUAL_REVIEW
    assert json.loads(evidence.content)["state"] == "TIMEOUT"


def test_legacy_commit_statuses_can_complete_the_ci_gate(db_session, repo_and_pr, monkeypatch):
    """A policy check reported through the legacy Status API is a valid CI result."""
    repo, pr = repo_and_pr
    files = [{"file_path": "README.md", "additions": 1, "deletions": 0, "patch_excerpt": ""}]
    posted_reviews = _patch_github(monkeypatch, files, [[], []])
    statuses = [
        {"name": "build", "status": "completed", "conclusion": "success"},
        {"name": "unit-tests", "status": "completed", "conclusion": "success"},
        {"name": "lint", "status": "completed", "conclusion": "success"},
    ]
    monkeypatch.setattr(gh_mod.github_client, "get_commit_statuses", lambda *a, **kw: statuses)

    review = pipeline.run_analysis(db_session, repo, pr, "999")

    assert review.decision in (m.DecisionStatus.APPROVED, m.DecisionStatus.MANUAL_REVIEW)
    assert review.is_ci_wait_placeholder is False
    assert [item["event"] for item in posted_reviews] == ["COMMENT"]


def test_new_head_sha_creates_a_new_wait_cycle(db_session, repo_and_pr, monkeypatch):
    repo, pr = repo_and_pr
    _patch_github(monkeypatch, [], [[], []])
    monkeypatch.setattr(pipeline.settings, "CI_DISCOVERY_STABILIZATION_SECONDS", 60)

    old_wait = pipeline.run_analysis(db_session, repo, pr, "999")
    pr.head_sha = "def456"
    db_session.commit()
    monkeypatch.setattr(gh_mod.github_client, "get_pull_request", lambda *a, **kw: {
        "state": "open", "head_sha": "def456", "base_ref": "main",
    })
    new_wait = pipeline.run_analysis(db_session, repo, pr, "999")

    assert old_wait.head_sha == "abc123"
    assert new_wait.head_sha == "def456"
    assert new_wait.id != old_wait.id
    assert new_wait.decision == m.DecisionStatus.WAITING_FOR_CI
