from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from app.api.webhooks import _handle_check_event
from app.core.db import Base
from app.models import models as m


def _session_with_open_pr(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'check_events.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    repo = m.Repository(
        github_repo_id="1",
        owner_login="octo",
        name="widgets",
        full_name="octo/widgets",
        installation_id="999",
    )
    session.add(repo)
    session.flush()
    pr = m.PullRequest(
        repository_id=repo.id,
        github_pr_number=7,
        title="Add feature",
        head_sha="current-sha",
        state="open",
    )
    session.add(pr)
    session.commit()
    return engine, session, repo, pr


def _check_payload(head_sha, pull_requests=None):
    return {
        "action": "completed",
        "repository": {"id": 1, "full_name": "octo/widgets"},
        "installation": {"id": 999},
        "check_run": {
            "head_sha": head_sha,
            "pull_requests": pull_requests or [],
        },
    }


def test_check_event_queries_current_pr_revision_and_queues_ci_refresh(tmp_path):
    engine, session, repo, pr = _session_with_open_pr(tmp_path)
    try:
        result = _handle_check_event(
            session,
            _check_payload("current-sha", [{"number": 7}]),
            "check_run",
            "delivery-current",
        )

        job = session.query(m.BackgroundJob).one()
        assert result["status"] == "accepted"
        assert job.payload["pull_request_id"] == pr.id
        assert job.payload["head_sha"] == "current-sha"
        assert job.payload["repository_id"] == repo.id
    finally:
        session.close()
        engine.dispose()


def test_distinct_check_events_for_same_sha_queue_only_ci_refresh_jobs(tmp_path):
    engine, session, _, pr = _session_with_open_pr(tmp_path)
    try:
        check_run = _check_payload("current-sha", [{"number": 7}])
        check_suite = {
            **check_run,
            "check_suite": {
                "head_sha": "current-sha",
                "pull_requests": [{"number": 7}],
            },
        }

        _handle_check_event(session, check_run, "check_run", "delivery-run")
        _handle_check_event(session, check_suite, "check_suite", "delivery-suite")

        jobs = session.query(m.BackgroundJob).all()
        assert len(jobs) == 2
        assert {job.job_type for job in jobs} == {"CI_REFRESH"}
        assert {job.payload["head_sha"] for job in jobs} == {"current-sha"}
        assert {job.payload["pull_request_id"] for job in jobs} == {pr.id}
    finally:
        session.close()
        engine.dispose()


def test_unknown_check_sha_is_ignored_without_queueing(tmp_path):
    engine, session, _, _ = _session_with_open_pr(tmp_path)
    try:
        result = _handle_check_event(
            session,
            _check_payload("unknown-sha", [{"number": 7}]),
            "check_run",
            "delivery-unknown",
        )

        assert result["status"] == "ignored"
        assert session.query(m.BackgroundJob).count() == 0
    finally:
        session.close()
        engine.dispose()


def test_check_event_for_new_revision_does_not_match_stale_pr_data(tmp_path):
    engine, session, _, pr = _session_with_open_pr(tmp_path)
    try:
        pr.head_sha = "new-sha"
        session.commit()

        result = _handle_check_event(
            session,
            _check_payload("old-sha", [{"number": 7}]),
            "check_run",
            "delivery-stale",
        )

        assert result["status"] == "ignored"
        assert session.query(m.BackgroundJob).count() == 0
    finally:
        session.close()
        engine.dispose()


def test_check_event_is_scoped_to_github_associated_pr_numbers(tmp_path):
    engine, session, repo, original_pr = _session_with_open_pr(tmp_path)
    try:
        other_pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=8,
            head_sha="current-sha",
            state="open",
        )
        session.add(other_pr)
        session.commit()

        _handle_check_event(
            session,
            _check_payload("current-sha", [{"number": 7}]),
            "check_run",
            "delivery-associated-pr",
        )

        jobs = session.query(m.BackgroundJob).all()
        assert len(jobs) == 1
        assert jobs[0].payload["pull_request_id"] == original_pr.id
    finally:
        session.close()
        engine.dispose()


def test_database_failure_returns_retryable_error_and_rolls_back():
    class FailedSession:
        rolled_back = False

        def query(self, *_args, **_kwargs):
            raise SQLAlchemyError("database schema unavailable")

        def rollback(self):
            self.rolled_back = True

    db = FailedSession()
    try:
        _handle_check_event(
            db,
            _check_payload("current-sha", [{"number": 7}]),
            "check_run",
            "delivery-db-error",
        )
    except HTTPException as exc:
        assert exc.status_code == 503
        assert "database operation failed" in exc.detail
    else:
        raise AssertionError("Database failures must not be reported as success.")

    assert db.rolled_back
