"""Database-backed workers for durable review and CI jobs."""

import logging
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import (
    DataError,
    DBAPIError,
    IntegrityError,
    OperationalError,
    ProgrammingError,
)

from app.core.db import get_session_factory
from app.models import models as m
from app.services.job_errors import RevisionLockUnavailable

logger = logging.getLogger("securepr.jobs")

JOB_TYPES = (
    "REVIEW",
    "CI_REFRESH",
    "CI_NOTIFY",
    "REVIEW_STATUS",
    "AUTO_MERGE_CANCEL",
    "AUTO_MERGE_RETRY",
)
MAX_ATTEMPTS = 5
LEASE_SECONDS = 300
POLL_SECONDS = 1
NON_RETRYABLE_DATABASE_ERRORS = (DataError, IntegrityError, ProgrammingError)
TRANSIENT_DATABASE_SQLSTATES = {
    "40001",
    "40P01",
    "55P03",
    "57014",
}


def _is_transient_database_error(exc: Exception) -> bool:
    if not isinstance(exc, DBAPIError):
        return False
    if exc.connection_invalidated:
        return True
    original = exc.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(
        original, "pgcode", None
    )
    if sqlstate:
        return sqlstate in TRANSIENT_DATABASE_SQLSTATES or sqlstate.startswith("08")
    return isinstance(exc, OperationalError)


def _record_job_failure(
    db, job, exc: Exception, *, expected_lease_token: int | None = None
) -> bool:
    lock_conflict = isinstance(exc, RevisionLockUnavailable)
    non_retryable_database_error = isinstance(exc, NON_RETRYABLE_DATABASE_ERRORS) or (
        isinstance(exc, DBAPIError) and not _is_transient_database_error(exc)
    )
    now = datetime.utcnow()
    if lock_conflict:
        state = "QUEUED"
        # A lock deferral is not an execution attempt; preserve the real retry budget.
        attempts = max(job.attempts - 1, 0)
        available_at = max(
            now + timedelta(seconds=1),
            min(exc.retry_at, now + timedelta(seconds=LEASE_SECONDS + 5)),
        )
    elif job.attempts >= MAX_ATTEMPTS or non_retryable_database_error:
        state = "FAILED"
        attempts = job.attempts
        available_at = job.available_at
    else:
        state = "QUEUED"
        attempts = job.attempts
        delay = min(2**job.attempts, 60)
        available_at = now + timedelta(seconds=delay)
    if expected_lease_token is None:
        job.last_error = str(exc)[:4000]
        job.lease_until = None
        job.updated_at = now
        job.state = state
        job.attempts = attempts
        job.available_at = available_at
    else:
        result = db.execute(
            update(m.BackgroundJob)
            .where(
                m.BackgroundJob.id == job.id,
                m.BackgroundJob.state == "RUNNING",
                m.BackgroundJob.lease_token == expected_lease_token,
                m.BackgroundJob.lease_until > now,
            )
            .values(
                state=state,
                last_error=str(exc)[:4000],
                lease_until=None,
                attempts=attempts,
                available_at=available_at,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            return False
    db.commit()
    return True


def enqueue_job(
    db, job_type: str, idempotency_key: str, payload: dict, available_at=None
) -> m.BackgroundJob:
    if job_type not in JOB_TYPES:
        raise ValueError(f"Unsupported background job type: {job_type}")
    existing = (
        db.query(m.BackgroundJob)
        .filter(m.BackgroundJob.idempotency_key == idempotency_key)
        .first()
    )
    if existing:
        if existing.state == "FAILED":
            existing.state = "QUEUED"
            existing.attempts = 0
            existing.available_at = datetime.utcnow()  # noqa: DTZ003
            existing.lease_until = None
            existing.last_error = None
            existing.updated_at = datetime.utcnow()  # noqa: DTZ003
            db.flush()
        return existing
    job = m.BackgroundJob(
        job_type=job_type,
        idempotency_key=idempotency_key,
        payload=payload,
        available_at=available_at or datetime.utcnow(),
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
        return job
    except IntegrityError:
        existing = (
            db.query(m.BackgroundJob)
            .filter(m.BackgroundJob.idempotency_key == idempotency_key)
            .first()
        )
        if existing:
            return existing
        raise


def background_worker_status() -> dict[str, bool]:
    with _workers_lock:
        workers = {worker.job_type: worker._thread.is_alive() for worker in _workers}
    return {job_type: workers.get(job_type, False) for job_type in JOB_TYPES}


class _BackgroundJobLease:
    def __init__(self, session_factory, job_id: str, lease_token: int) -> None:
        self._session_factory = session_factory
        self.job_id = job_id
        self.lease_token = lease_token
        self._lost = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._heartbeat,
            name=f"securepr-job-lease-{job_id[:8]}",
            daemon=True,
        )

    def start(self) -> None:
        self.assert_owned()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise RuntimeError("Background job lease ownership could not be verified.")
        now = datetime.utcnow()
        db = self._session_factory()
        try:
            result = db.execute(
                update(m.BackgroundJob)
                .where(
                    m.BackgroundJob.id == self.job_id,
                    m.BackgroundJob.state == "RUNNING",
                    m.BackgroundJob.lease_token == self.lease_token,
                    m.BackgroundJob.lease_until > now,
                )
                .values(
                    lease_until=now + timedelta(seconds=LEASE_SECONDS),
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                db.rollback()
                self._lost.set()
                raise RuntimeError("Background job lease ownership was lost.")
            db.commit()
        except RuntimeError:
            raise
        except Exception as exc:
            db.rollback()
            self._lost.set()
            logger.exception(
                "Background job lease renewal failed: job_id=%s lease_token=%s",
                self.job_id,
                self.lease_token,
                extra={
                    "job_id": self.job_id,
                    "lease_token": self.lease_token,
                    "failure_reason": f"{type(exc).__name__}: {exc}",
                },
            )
            raise RuntimeError(
                "Background job lease renewal failed; stale work is fenced."
            ) from exc
        finally:
            db.close()

    def _heartbeat(self) -> None:
        interval = max(1, LEASE_SECONDS // 3)
        while not self._stop.wait(interval):
            try:
                self.assert_owned()
            except RuntimeError:
                return


class _Worker:
    def __init__(self, job_type: str):
        self.job_type = job_type
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"securepr-{job_type.casefold()}-worker",
            daemon=True,
        )

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _claim(self, db):
        now = datetime.utcnow()
        db.query(m.BackgroundJob).filter(
            m.BackgroundJob.job_type == self.job_type,
            m.BackgroundJob.state == "RUNNING",
            m.BackgroundJob.attempts >= MAX_ATTEMPTS,
            m.BackgroundJob.lease_until <= now,
        ).update(
            {
                m.BackgroundJob.state: "FAILED",
                m.BackgroundJob.last_error: "Worker lease expired after the final attempt.",
                m.BackgroundJob.lease_until: None,
                m.BackgroundJob.updated_at: now,
            },
            synchronize_session=False,
        )
        db.commit()
        candidate = (
            db.query(m.BackgroundJob)
            .filter(
                m.BackgroundJob.job_type == self.job_type,
                m.BackgroundJob.attempts < MAX_ATTEMPTS,
                or_(
                    and_(
                        m.BackgroundJob.state == "QUEUED",
                        m.BackgroundJob.available_at <= now,
                    ),
                    and_(
                        m.BackgroundJob.state == "RUNNING",
                        m.BackgroundJob.lease_until <= now,
                    ),
                ),
            )
            .order_by(m.BackgroundJob.available_at, m.BackgroundJob.created_at)
            .with_for_update(skip_locked=True)
            .first()
        )
        if not candidate:
            db.rollback()
            return None

        old_state = candidate.state
        old_lease = candidate.lease_until
        filters = [
            m.BackgroundJob.id == candidate.id,
            m.BackgroundJob.state == old_state,
        ]
        if old_state == "QUEUED":
            filters.append(m.BackgroundJob.available_at <= now)
        else:
            filters.append(m.BackgroundJob.lease_until == old_lease)
        next_attempts = candidate.attempts + 1
        next_lease_token = candidate.lease_token + 1
        claimed = db.execute(
            update(m.BackgroundJob)
            .where(*filters)
            .values(
                state="RUNNING",
                attempts=next_attempts,
                lease_token=next_lease_token,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return None
        job = SimpleNamespace(
            id=candidate.id,
            job_type=candidate.job_type,
            payload=candidate.payload,
            state="RUNNING",
            attempts=next_attempts,
            lease_token=next_lease_token,
            available_at=candidate.available_at,
            lease_until=now + timedelta(seconds=LEASE_SECONDS),
        )
        db.commit()
        return job

    def _run_job(self, db, job):
        from app.services import pipeline

        if self.job_type == "REVIEW":
            payload = dict(job.payload or {})
            payload["_job_id"] = job.id
            payload["_retry_count"] = max(job.attempts - 1, 0)
            pipeline.process_review_job(db, payload)
        elif self.job_type == "CI_REFRESH":
            pipeline.process_ci_refresh_job(db, job.payload)
        elif self.job_type == "CI_NOTIFY":
            pipeline.process_ci_notification_job(db, job.payload)
        elif self.job_type == "REVIEW_STATUS":
            payload = dict(job.payload or {})
            payload["_job_id"] = job.id
            payload["_retry_count"] = max(job.attempts - 1, 0)
            pipeline.process_review_status_job(db, payload)
        elif self.job_type == "AUTO_MERGE_CANCEL":
            payload = dict(job.payload or {})
            payload["_job_id"] = job.id
            payload["_retry_count"] = max(job.attempts - 1, 0)
            pipeline.process_auto_merge_cancel_job(db, payload)
        elif self.job_type == "AUTO_MERGE_RETRY":
            payload = dict(job.payload or {})
            payload["_job_id"] = job.id
            pipeline.process_auto_merge_retry_job(db, payload)

    def _log_job(self, db, job, status: str, failure_reason: str | None = None) -> None:
        payload = job.payload or {}
        repository_id = payload.get("repository_id")
        pull_request_id = payload.get("pull_request_id")
        repo = (
            db.query(m.Repository).filter(m.Repository.id == repository_id).first()
            if repository_id
            else None
        )
        pr = (
            db.query(m.PullRequest).filter(m.PullRequest.id == pull_request_id).first()
            if pull_request_id
            else None
        )
        head_sha = payload.get("head_sha") or (pr.head_sha if pr else None)
        publication_result = "not_applicable"
        if pr and head_sha:
            publications = (
                db.query(m.ReviewPublication)
                .filter(
                    m.ReviewPublication.pull_request_id == pr.id,
                    m.ReviewPublication.head_sha == head_sha,
                )
                .all()
            )
            publication_result = (
                ",".join(
                    f"{record.message_type}:{record.state}" for record in publications
                )
                or "not_attempted"
            )
        fields = {
            "job_id": job.id,
            "job_type": job.job_type,
            "repository": repo.full_name if repo else "unknown",
            "pr_number": pr.github_pr_number if pr else None,
            "head_sha": head_sha or "unknown",
            "worker_name": self._thread.name,
            "attempt_number": job.attempts,
            "job_status": status,
            "failure_reason": failure_reason or "none",
            "publication_result": publication_result,
        }
        logger.info(
            "Background job job_id=%(job_id)s job_type=%(job_type)s "
            "repository=%(repository)s pr_number=%(pr_number)s "
            "head_sha=%(head_sha)s worker_name=%(worker_name)s "
            "attempt_number=%(attempt_number)s job_status=%(job_status)s "
            "failure_reason=%(failure_reason)s publication_result=%(publication_result)s",
            fields,
            extra=fields,
        )

    def _analysis_failure_reason(self, db, job) -> str | None:
        if job.job_type != "REVIEW":
            return None
        payload = job.payload or {}
        review = (
            db.query(m.Review)
            .filter(
                m.Review.pull_request_id == payload.get("pull_request_id"),
                m.Review.head_sha == payload.get("head_sha"),
                m.Review.lifecycle_state == "ANALYSIS_FAILED",
            )
            .order_by(m.Review.created_at.desc())
            .first()
        )
        if not review:
            return None
        evidence = (
            db.query(m.Evidence)
            .filter(
                m.Evidence.review_id == review.id,
                m.Evidence.evidence_type == "error",
                m.Evidence.source == "ai_provider",
            )
            .first()
        )
        return (
            evidence.content
            if evidence
            else "AI analysis failed; manual review required."
        )

    def _ai_retry_at(self, db, job) -> datetime | None:
        if job.job_type != "REVIEW":
            return None
        payload = job.payload or {}
        pr = (
            db.query(m.PullRequest)
            .filter(m.PullRequest.id == payload.get("pull_request_id"))
            .first()
        )
        if (
            not pr
            or pr.head_sha != payload.get("head_sha")
            or pr.ai_state != "AI_UNAVAILABLE"
            or pr.ai_retryable_at is None
            or pr.ai_retryable_at <= datetime.utcnow()
        ):
            return None
        return pr.ai_retryable_at

    def _run(self):
        session_factory = get_session_factory()
        while not self._stop.is_set():
            db = session_factory()
            try:
                job = self._claim(db)
                if job is None:
                    db.close()
                    self._stop.wait(POLL_SECONDS)
                    continue
                claimed_lease_token = job.lease_token
                job_lease = _BackgroundJobLease(
                    session_factory, job.id, claimed_lease_token
                )
                try:
                    job_lease.start()
                    self._log_job(db, job, "RUNNING")
                    db.commit()
                    self._run_job(db, job)
                    job_lease.assert_owned()
                    current_job = (
                        db.query(m.BackgroundJob)
                        .filter(m.BackgroundJob.id == job.id)
                        .populate_existing()
                        .first()
                    )
                    if (
                        current_job is None
                        or current_job.state != "RUNNING"
                        or current_job.lease_token != claimed_lease_token
                    ):
                        logger.warning(
                            "Discarded completed work from a stale job lease: "
                            "job_id=%s lease_token=%s current_lease_token=%s "
                            "job_state=%s",
                            job.id,
                            claimed_lease_token,
                            current_job.lease_token if current_job else "missing",
                            current_job.state if current_job else "missing",
                            extra={
                                "job_id": job.id,
                                "lease_token": claimed_lease_token,
                                "current_lease_token": (
                                    current_job.lease_token
                                    if current_job
                                    else "missing"
                                ),
                                "job_state": (
                                    current_job.state if current_job else "missing"
                                ),
                            },
                        )
                        if current_job:
                            self._log_job(db, current_job, "STALE_WORKER")
                        continue
                    failure_reason = self._analysis_failure_reason(db, job)
                    retry_at = self._ai_retry_at(db, job)
                    values: dict[str, object] = {
                        "state": "QUEUED" if retry_at else "COMPLETED",
                        "last_error": failure_reason if retry_at else None,
                        "lease_until": None,
                        "updated_at": datetime.utcnow(),
                    }
                    if retry_at:
                        values["attempts"] = 0
                        values["available_at"] = retry_at
                    completed = db.execute(
                        update(m.BackgroundJob)
                        .where(
                            m.BackgroundJob.id == job.id,
                            m.BackgroundJob.state == "RUNNING",
                            m.BackgroundJob.lease_token == claimed_lease_token,
                            m.BackgroundJob.lease_until > datetime.utcnow(),
                        )
                        .values(**values)
                    )
                    if completed.rowcount != 1:
                        db.rollback()
                        current_job = (
                            db.query(m.BackgroundJob)
                            .filter(m.BackgroundJob.id == job.id)
                            .first()
                        )
                        if current_job:
                            self._log_job(db, current_job, "STALE_WORKER")
                        continue
                    db.commit()
                    current_job = (
                        db.query(m.BackgroundJob)
                        .filter(m.BackgroundJob.id == job.id)
                        .first()
                    )
                    if not current_job:
                        raise RuntimeError("Completed background job disappeared.")
                    self._log_job(
                        db,
                        current_job,
                        "RESCHEDULED" if retry_at else "COMPLETED",
                        failure_reason,
                    )
                except Exception as exc:
                    db.rollback()
                    job = (
                        db.query(m.BackgroundJob)
                        .filter(m.BackgroundJob.id == job.id)
                        .first()
                    )
                    failure_recorded = bool(
                        job
                        and _record_job_failure(
                            db,
                            job,
                            exc,
                            expected_lease_token=claimed_lease_token,
                        )
                    )
                    job = (
                        db.query(m.BackgroundJob)
                        .filter(m.BackgroundJob.id == job.id)
                        .first()
                        if job
                        else None
                    )
                    if not failure_recorded and job:
                        logger.warning(
                            "Discarded failure from a stale job lease: job_id=%s "
                            "lease_token=%s current_lease_token=%s job_state=%s",
                            job.id,
                            claimed_lease_token,
                            job.lease_token,
                            job.state,
                            extra={
                                "job_id": job.id,
                                "lease_token": claimed_lease_token,
                                "current_lease_token": job.lease_token,
                                "job_state": job.state,
                            },
                        )
                    payload = job.payload or {} if job else {}
                    repository = (
                        db.query(m.Repository)
                        .filter(
                            m.Repository.id == payload.get("repository_id")
                        )
                        .first()
                        if payload.get("repository_id")
                        else None
                    )
                    pull_request = (
                        db.query(m.PullRequest)
                        .filter(
                            m.PullRequest.id == payload.get("pull_request_id")
                        )
                        .first()
                        if payload.get("pull_request_id")
                        else None
                    )
                    lock_fields = {
                        "job_id": job.id if job else "unknown",
                        "job_type": self.job_type,
                        "repository": repository.full_name if repository else "unknown",
                        "pr_number": (
                            pull_request.github_pr_number if pull_request else None
                        ),
                        "repository_id": payload.get("repository_id"),
                        "pull_request_id": payload.get("pull_request_id"),
                        "head_sha": payload.get("head_sha", "unknown"),
                        "operation": (
                            exc.operation
                            if isinstance(exc, RevisionLockUnavailable)
                            else "unknown"
                        ),
                        "lease_owner": (
                            exc.lease_owner
                            if isinstance(exc, RevisionLockUnavailable)
                            else "unknown"
                        ),
                        "failure_reason": f"{type(exc).__name__}: {exc}",
                    }
                    logger.warning(
                        "Background job execution deferred or failed: "
                        "job_id=%(job_id)s job_type=%(job_type)s "
                        "repository=%(repository)s pr_number=%(pr_number)s "
                        "repository_id=%(repository_id)s "
                        "pull_request_id=%(pull_request_id)s "
                        "head_sha=%(head_sha)s operation=%(operation)s "
                        "lease_owner=%(lease_owner)s failure_reason=%(failure_reason)s",
                        lock_fields,
                        extra=lock_fields,
                    )
                    if not isinstance(exc, RevisionLockUnavailable):
                        logger.exception(
                            "Background job failed%s",
                            " with a non-retryable database error"
                            if isinstance(exc, NON_RETRYABLE_DATABASE_ERRORS)
                            else "",
                            extra={
                                "job_id": job.id if job else "unknown",
                                "job_type": self.job_type,
                                "repository": "unknown",
                                "pr_number": None,
                                "head_sha": "unknown",
                                "worker_name": self._thread.name,
                                "attempt_number": job.attempts if job else "unknown",
                                "job_status": job.state if job else "unknown",
                                "failure_reason": f"{type(exc).__name__}: {exc}",
                                "publication_result": "unknown",
                            },
                        )
                    if job:
                        self._log_job(
                            db,
                            job,
                            job.state,
                            f"{type(exc).__name__}: {exc}",
                        )
                finally:
                    job_lease.stop()
            except Exception:
                db.rollback()
                logger.exception(
                    "Background worker iteration failed: type=%s", self.job_type
                )
                self._stop.wait(POLL_SECONDS)
            finally:
                db.close()


_workers: list[_Worker] = []
_workers_lock = threading.Lock()


def start_background_workers() -> None:
    with _workers_lock:
        if _workers:
            return
        _workers.extend(_Worker(job_type) for job_type in JOB_TYPES)
        for worker in _workers:
            worker.start()
    logger.info("Started durable background workers: %s", ", ".join(JOB_TYPES))
    for worker in _workers:
        logger.info(
            "Background worker started: job_type=%s worker_name=%s worker_alive=%s",
            worker.job_type,
            worker._thread.name,
            worker._thread.is_alive(),
            extra={
                "job_type": worker.job_type,
                "worker_name": worker._thread.name,
                "worker_alive": worker._thread.is_alive(),
            },
        )


def stop_background_workers() -> None:
    with _workers_lock:
        workers = list(_workers)
        _workers.clear()
    for worker in workers:
        worker.stop()
    logger.info("Stopped durable background workers")
