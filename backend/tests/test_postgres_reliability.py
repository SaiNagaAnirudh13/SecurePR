"""PostgreSQL-only concurrency regressions; set TEST_POSTGRES_URL to run."""

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

import run as run_module
from app.core.db import Base
from app.models import models as m
from app.services import background_jobs, pipeline

POSTGRES_URL = os.getenv("TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="TEST_POSTGRES_URL is required for PostgreSQL reliability tests",
)


@pytest.fixture
def lock_session(monkeypatch):
    url = make_url(POSTGRES_URL)
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("TEST_POSTGRES_URL must point to a local PostgreSQL test server.")
    engine = create_engine(url, future=True)
    monkeypatch.setattr(pipeline, "_dedicated_lock_engine", None)
    session = Session(bind=engine, future=True)
    yield session
    session.close()
    if pipeline._dedicated_lock_engine is not None:
        pipeline._dedicated_lock_engine.dispose()
    monkeypatch.setattr(pipeline, "_dedicated_lock_engine", None)
    engine.dispose()


@pytest.fixture
def isolated_job_sessions():
    if not POSTGRES_URL:
        pytest.skip("TEST_POSTGRES_URL is required")
    url = make_url(POSTGRES_URL)
    if url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("TEST_POSTGRES_URL must point to a local PostgreSQL test server.")
    schema = f"securepr_test_{uuid4().hex}"
    admin_engine = create_engine(url, future=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"},
        future=True,
    )
    Base.metadata.create_all(
        engine,
        tables=[
            m.Repository.__table__,
            m.PullRequest.__table__,
            m.BackgroundJob.__table__,
        ],
    )
    factory = sessionmaker(bind=engine, future=True)
    try:
        yield factory
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin_engine.dispose()


def _lock_identity(repository_id: str, pr_number: int) -> int:
    import hashlib

    resource_id = hashlib.sha256(f"{repository_id}:{pr_number}".encode()).hexdigest()
    resource = f"securepr:review:{resource_id}"
    return int.from_bytes(
        hashlib.sha256(resource.encode("utf-8")).digest()[:8],
        byteorder="big",
        signed=True,
    )


def test_concurrent_workers_serialize_same_pull_request(lock_session):
    repository_id = str(uuid4())
    active = 0
    max_active = 0
    guard = threading.Lock()
    barrier = threading.Barrier(2)

    def worker():
        nonlocal active, max_active
        with Session(bind=lock_session.get_bind(), future=True) as db:
            barrier.wait(timeout=5)
            with pipeline._revision_finalization_lock(
                db, repository_id, 17, head_sha="sha-one"
            ):
                with guard:
                    active += 1
                    max_active = max(max_active, active)
                time.sleep(0.15)
                with guard:
                    active -= 1

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _index: worker(), range(2)))

    assert max_active == 1
    assert isinstance(pipeline._dedicated_lock_engine.pool, NullPool)


def test_lock_timeout_is_bounded_and_lock_can_be_reacquired(lock_session, monkeypatch):
    repository_id = str(uuid4())
    monkeypatch.setattr(pipeline, "LOCK_WAIT_SECONDS", 0.15)
    monkeypatch.setattr(pipeline, "LOCK_POLL_SECONDS", 0.01)
    other = Session(bind=lock_session.get_bind(), future=True)
    started = time.monotonic()
    try:
        with (
            pipeline._revision_finalization_lock(
                lock_session, repository_id, 18, head_sha="sha-one"
            ),
            pytest.raises(pipeline.RevisionLockUnavailable),
            pipeline._revision_finalization_lock(
                other, repository_id, 18, head_sha="sha-one"
            ),
        ):
            pytest.fail("A concurrent worker bypassed the held lock.")
        assert time.monotonic() - started < 2
        with pipeline._revision_finalization_lock(
            other, repository_id, 18, head_sha="sha-one"
        ):
            pass
    finally:
        other.close()


def test_lock_is_released_after_exception_without_leaking_session_lock(lock_session):
    repository_id = str(uuid4())
    with (
        pytest.raises(RuntimeError, match="simulated review failure"),
        pipeline._revision_finalization_lock(
            lock_session, repository_id, 19, head_sha="sha-one"
        ),
    ):
        raise RuntimeError("simulated review failure")

    key = _lock_identity(repository_id, 19)
    with pipeline._lock_engine(lock_session).connect() as connection:
        assert connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        ).scalar_one()
        assert connection.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": key}
        ).scalar_one()


def test_migration_lock_is_released_after_exception_and_wait_is_bounded(
    monkeypatch,
):
    engine = create_engine(POSTGRES_URL, poolclass=NullPool, future=True)
    monkeypatch.setattr(run_module, "_MIGRATION_LOCK_WAIT_SECONDS", 0.15)
    try:
        started = time.monotonic()
        with (
            engine.connect() as owner,
            engine.connect() as contender,
            run_module._production_migration_lock(owner),
            pytest.raises(TimeoutError, match="migration lock"),
            run_module._production_migration_lock(contender),
        ):
            pytest.fail("A concurrent startup bypassed the migration lock.")
        assert time.monotonic() - started < 2
        with (
            engine.connect() as connection,
            pytest.raises(RuntimeError, match="simulated migration failure"),
            run_module._production_migration_lock(connection),
        ):
            raise RuntimeError("simulated migration failure")
        with (
            engine.connect() as connection,
            run_module._production_migration_lock(connection),
        ):
            pass
    finally:
        engine.dispose()


def test_concurrent_job_claim_is_atomic(isolated_job_sessions):
    with isolated_job_sessions() as db:
        db.add(
            m.BackgroundJob(
                job_type="REVIEW",
                idempotency_key=f"atomic:{uuid4()}",
                payload={"head_sha": "sha-one"},
            )
        )
        db.commit()

    barrier = threading.Barrier(2)

    def claim():
        with isolated_job_sessions() as db:
            barrier.wait(timeout=5)
            job = background_jobs._Worker("REVIEW")._claim(db)
            return job.id if job else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(lambda _index: claim(), range(2)))

    assert sum(job_id is not None for job_id in claimed) == 1


def test_concurrent_duplicate_enqueues_preserve_idempotency(isolated_job_sessions):
    key = f"duplicate:{uuid4()}"
    barrier = threading.Barrier(2)

    def enqueue():
        with isolated_job_sessions() as db:
            barrier.wait(timeout=5)
            job = background_jobs.enqueue_job(
                db,
                "REVIEW",
                key,
                {"head_sha": "sha-one"},
            )
            job_id = job.id
            db.commit()
            return job_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        job_ids = list(pool.map(lambda _index: enqueue(), range(2)))

    assert job_ids[0] == job_ids[1]
    with isolated_job_sessions() as db:
        assert db.query(m.BackgroundJob).filter_by(idempotency_key=key).count() == 1


def test_expired_job_lease_is_reclaimed_with_new_fencing_token(
    isolated_job_sessions,
):
    with isolated_job_sessions() as db:
        db.add(
            m.BackgroundJob(
                job_type="REVIEW",
                idempotency_key=f"lease-expiry:{uuid4()}",
                payload={"head_sha": "sha-one"},
            )
        )
        db.commit()
        first = background_jobs._Worker("REVIEW")._claim(db)
        assert first is not None
        assert first.lease_token == 1
        first_id = first.id

        db.query(m.BackgroundJob).filter_by(id=first_id).update(
            {m.BackgroundJob.lease_until: datetime(2000, 1, 1)},
            synchronize_session=False,
        )
        db.commit()
        reclaimed = background_jobs._Worker("REVIEW")._claim(db)

        assert reclaimed is not None
        assert reclaimed.id == first_id
        assert reclaimed.attempts == 2
        assert reclaimed.lease_token == 2


def test_stale_worker_cannot_fail_a_reclaimed_job(isolated_job_sessions):
    with isolated_job_sessions() as db:
        db.add(
            m.BackgroundJob(
                job_type="REVIEW",
                idempotency_key=f"stale-failure:{uuid4()}",
                payload={"head_sha": "sha-one"},
            )
        )
        db.commit()
        first = background_jobs._Worker("REVIEW")._claim(db)
        assert first is not None
        first_token = first.lease_token
        db.query(m.BackgroundJob).filter_by(id=first.id).update(
            {m.BackgroundJob.lease_until: datetime(2000, 1, 1)},
            synchronize_session=False,
        )
        db.commit()
        reclaimed = background_jobs._Worker("REVIEW")._claim(db)
        assert reclaimed is not None

        recorded = background_jobs._record_job_failure(
            db,
            reclaimed,
            RuntimeError("failure from expired worker"),
            expected_lease_token=first_token,
        )

        current = db.query(m.BackgroundJob).filter_by(id=first.id).one()
        assert recorded is False
        assert current.state == "RUNNING"
        assert current.lease_token == first_token + 1


def test_postgres_review_status_job_ignores_stale_sha(isolated_job_sessions):
    with isolated_job_sessions() as db:
        repo = m.Repository(
            github_repo_id=f"repo-{uuid4()}",
            owner_login="octo",
            name="widgets",
            full_name="octo/widgets",
            installation_id="99",
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=29,
            head_sha="current-sha",
            state="open",
        )
        db.add(pr)
        db.commit()

        pipeline.process_review_status_job(
            db,
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "head_sha": "stale-sha",
                "_job_id": "stale-sha-job",
            },
        )

        assert db.query(m.PullRequest).filter_by(id=pr.id).one().head_sha == (
            "current-sha"
        )
