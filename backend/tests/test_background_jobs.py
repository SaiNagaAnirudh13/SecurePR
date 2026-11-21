import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.exc import DataError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models import models as m
from app.services import pipeline
from app.services.background_jobs import (
    LEASE_SECONDS,
    MAX_ATTEMPTS,
    _BackgroundJobLease,
    _record_job_failure,
    _Worker,
    enqueue_job,
)
from app.services.job_errors import RevisionLockUnavailable


def test_job_and_idempotency_survive_database_session_restart(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, future=True)

    with factory() as first_session:
        first = enqueue_job(
            first_session,
            "REVIEW",
            "review:repo:pr:sha",
            {"repository_id": "repo", "head_sha": "sha"},
        )
        first_session.commit()
        first_id = first.id

    with factory() as restarted_session:
        duplicate = enqueue_job(
            restarted_session,
            "REVIEW",
            "review:repo:pr:sha",
            {"repository_id": "repo", "head_sha": "sha"},
        )
        restarted_session.commit()
        assert duplicate.id == first_id
        assert restarted_session.query(m.BackgroundJob).count() == 1


def test_expired_final_attempt_is_marked_failed(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'expired.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    job = m.BackgroundJob(
        job_type="CI_REFRESH",
        idempotency_key="refresh:expired",
        payload={},
        state="RUNNING",
        attempts=MAX_ATTEMPTS,
        available_at=datetime.utcnow() - timedelta(minutes=10),  # noqa: DTZ003
        lease_until=datetime.utcnow() - timedelta(minutes=1),  # noqa: DTZ003
    )
    session.add(job)
    session.commit()

    assert _Worker("CI_REFRESH")._claim(session) is None

    session.refresh(job)
    assert job.state == "FAILED"
    assert "final attempt" in job.last_error
    session.close()


def test_expired_running_job_is_reclaimed_after_worker_restart(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, future=True)
    with factory() as first_session:
        job = m.BackgroundJob(
            job_type="REVIEW",
            idempotency_key="review:recovered",
            payload={"repository_id": "repo", "head_sha": "sha"},
            state="RUNNING",
            attempts=1,
            available_at=datetime.utcnow() - timedelta(minutes=10),  # noqa: DTZ003
            lease_until=datetime.utcnow() - timedelta(minutes=1),  # noqa: DTZ003
        )
        first_session.add(job)
        first_session.commit()
        job_id = job.id

    with factory() as restarted_session:
        reclaimed = _Worker("REVIEW")._claim(restarted_session)

        assert reclaimed is not None
        assert reclaimed.id == job_id
        assert reclaimed.state == "RUNNING"
        assert reclaimed.attempts == 2
        assert reclaimed.lease_token == 1
        assert reclaimed.lease_until > datetime.utcnow()  # noqa: DTZ003
    engine.dispose()


def test_failed_job_can_be_recovered_with_same_idempotency_key(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'failed-recovery.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    failed = m.BackgroundJob(
        job_type="REVIEW",
        idempotency_key="review:failed",
        payload={"repository_id": "repo", "head_sha": "sha"},
        state="FAILED",
        attempts=MAX_ATTEMPTS,
        available_at=datetime.utcnow(),  # noqa: DTZ003
        last_error="temporary GitHub failure",
    )
    session.add(failed)
    session.commit()

    recovered = enqueue_job(
        session,
        "REVIEW",
        "review:failed",
        {"repository_id": "repo", "head_sha": "sha"},
    )

    assert recovered.id == failed.id
    assert recovered.state == "QUEUED"
    assert recovered.attempts == 0
    assert recovered.last_error is None
    session.commit()
    session.close()
    engine.dispose()


def test_non_retryable_database_failure_stops_job_immediately(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'data-error.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    job = m.BackgroundJob(
        job_type="REVIEW",
        idempotency_key="review:schema-error",
        payload={},
        state="RUNNING",
        attempts=1,
        available_at=datetime.utcnow(),
        lease_until=datetime.utcnow() + timedelta(minutes=5),
    )
    session.add(job)
    session.commit()

    _record_job_failure(
        session,
        job,
        DataError("UPDATE reviews", {}, Exception("value exceeds column length")),
    )

    assert job.state == "FAILED"
    assert "value exceeds column length" in job.last_error
    session.close()
    engine.dispose()


def test_transient_database_failure_remains_retryable(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'operational-error.db'}", future=True
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    job = m.BackgroundJob(
        job_type="REVIEW",
        idempotency_key="review:transient-error",
        payload={},
    )
    session.add(job)
    session.commit()

    claimed = _Worker("REVIEW")._claim(session)
    assert claimed is not None
    _record_job_failure(
        session,
        claimed,
        OperationalError("SELECT 1", {}, Exception("connection interrupted")),
        expected_lease_token=claimed.lease_token,
    )

    persisted = session.query(m.BackgroundJob).filter_by(id=claimed.id).one()
    assert persisted.state == "QUEUED"
    assert persisted.attempts == 1
    session.close()
    engine.dispose()


def test_stale_worker_failure_cannot_overwrite_newer_lease(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stale-lease.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    job = m.BackgroundJob(
        job_type="REVIEW",
        idempotency_key="review:stale-lease",
        payload={},
        state="RUNNING",
        attempts=2,
        lease_token=2,
    )
    session.add(job)
    session.commit()

    updated = _record_job_failure(
        session,
        job,
        OperationalError("SELECT 1", {}, Exception("stale worker")),
        expected_lease_token=1,
    )

    session.refresh(job)
    assert updated is False
    assert job.state == "RUNNING"
    assert job.lease_token == 2
    session.close()
    engine.dispose()


def test_revision_lock_conflict_is_deferred_without_spending_attempts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'lock-conflict.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    now = datetime.utcnow()  # noqa: DTZ003
    job = m.BackgroundJob(
        job_type="REVIEW",
        idempotency_key="review:lock-conflict",
        payload={"head_sha": "sha-one"},
        state="RUNNING",
        attempts=MAX_ATTEMPTS,
        lease_token=7,
        available_at=now,
        lease_until=now + timedelta(seconds=LEASE_SECONDS),
    )
    session.add(job)
    session.commit()

    updated = _record_job_failure(
        session,
        job,
        RevisionLockUnavailable(
            "Another worker owns the revision lease.",
            retry_at=now + timedelta(seconds=90),
            lease_owner="another_worker",
            operation="review-analysis",
        ),
        expected_lease_token=7,
    )

    session.refresh(job)
    assert updated is True
    assert job.state == "QUEUED"
    assert job.attempts == MAX_ATTEMPTS - 1
    assert job.available_at >= now + timedelta(seconds=89)
    assert "owns the revision lease" in job.last_error
    session.close()
    engine.dispose()


def test_initial_and_reevaluation_share_the_same_revision_lock(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'review-lease.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine, future=True)()
    repo = m.Repository(
        github_repo_id="lease-test",
        owner_login="octo",
        name="widgets",
        full_name="octo/widgets",
        installation_id="99",
    )
    session.add(repo)
    session.flush()
    pr = m.PullRequest(
        repository_id=repo.id,
        github_pr_number=9,
        author_login="octocat",
        head_sha="sha-one",
        base_branch="main",
        state="open",
    )
    session.add(pr)
    session.commit()
    operations = []

    class Lease:
        def assert_owned(self):
            return None

    @contextmanager
    def fake_lock(*args, **kwargs):
        operations.append(kwargs["operation"])
        yield Lease()

    monkeypatch.setattr(pipeline, "_revision_finalization_lock", fake_lock)
    monkeypatch.setattr(pipeline, "_run_analysis_locked", lambda *args, **kwargs: None)

    pipeline.run_analysis(session, repo, pr, "99")
    pipeline.run_analysis(
        session,
        repo,
        pr,
        "99",
        is_reevaluation=True,
        previous_review_id="previous-review",
    )

    assert operations == ["review-analysis", "review-analysis"]
    session.close()
    engine.dispose()


def test_background_job_lease_is_renewed_during_long_work(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'job-heartbeat.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr("app.services.background_jobs.LEASE_SECONDS", 3)
    with factory() as db:
        job = m.BackgroundJob(
            job_type="REVIEW",
            idempotency_key="review:heartbeat",
            payload={},
            state="RUNNING",
            attempts=1,
            lease_token=1,
            available_at=datetime.utcnow(),  # noqa: DTZ003
            lease_until=datetime.utcnow() + timedelta(seconds=3),  # noqa: DTZ003
        )
        db.add(job)
        db.commit()
        original_lease_until = job.lease_until
        lease = _BackgroundJobLease(factory, job.id, job.lease_token)
        lease.start()
        try:
            time.sleep(1.2)
            db.refresh(job)
            assert job.lease_until > original_lease_until
        finally:
            lease.stop()
    engine.dispose()


def test_revision_operation_lease_renews_without_holding_db_transaction(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'revision-heartbeat.db'}", future=True
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, future=True)
    monkeypatch.setattr(pipeline, "REVISION_OPERATION_LEASE_SECONDS", 3)
    with factory() as db:
        repo = m.Repository(
            github_repo_id="lease-repo",
            owner_login="octo",
            name="widgets",
            full_name="octo/widgets",
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=4,
            head_sha="sha-one",
            state="open",
        )
        db.add(pr)
        db.commit()

        with pipeline._revision_finalization_lock(
            db,
            repo.id,
            pr.github_pr_number,
            head_sha=pr.head_sha,
            operation="test-renewal",
        ) as lease:
            assert not db.in_transaction()
            with factory() as check_db:
                operation = check_db.query(m.RevisionOperation).one()
                original_lease_until = operation.lease_until
            time.sleep(1.2)
            with factory() as check_db:
                operation = check_db.query(m.RevisionOperation).one()
                assert operation.lease_until > original_lease_until
            lease.assert_owned()
            assert not db.in_transaction()
    engine.dispose()
