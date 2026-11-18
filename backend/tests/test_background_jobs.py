from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.db import Base
from app.models import models as m
from app.services.background_jobs import MAX_ATTEMPTS, _Worker, enqueue_job


def test_job_and_idempotency_survive_database_session_restart(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jobs.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, future=True)

    with factory() as first_session:
        first = enqueue_job(
            first_session, "REVIEW", "review:repo:pr:sha",
            {"repository_id": "repo", "head_sha": "sha"},
        )
        first_session.commit()
        first_id = first.id

    with factory() as restarted_session:
        duplicate = enqueue_job(
            restarted_session, "REVIEW", "review:repo:pr:sha",
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
        job_type="CI_REFRESH", idempotency_key="refresh:expired",
        payload={}, state="RUNNING", attempts=MAX_ATTEMPTS,
        available_at=datetime.utcnow() - timedelta(minutes=10),
        lease_until=datetime.utcnow() - timedelta(minutes=1),
    )
    session.add(job)
    session.commit()

    assert _Worker("CI_REFRESH")._claim(session) is None

    session.refresh(job)
    assert job.state == "FAILED"
    assert "final attempt" in job.last_error
    session.close()
