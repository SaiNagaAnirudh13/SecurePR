from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.exc import DataError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models import models as m
from app.services.background_jobs import (
    MAX_ATTEMPTS,
    _record_job_failure,
    _Worker,
    enqueue_job,
)


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
        OperationalError("SELECT 1", {}, Exception("connection interrupted")),
    )

    assert job.state == "QUEUED"
    session.close()
    engine.dispose()
