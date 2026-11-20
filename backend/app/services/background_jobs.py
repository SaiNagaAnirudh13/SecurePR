"""Database-backed workers for durable review and CI jobs."""
import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError

from app.core.db import get_session_factory
from app.models import models as m

logger = logging.getLogger("securepr.jobs")

JOB_TYPES = ("REVIEW", "CI_REFRESH", "CI_NOTIFY", "REVIEW_STATUS")
MAX_ATTEMPTS = 5
LEASE_SECONDS = 300
POLL_SECONDS = 1


def enqueue_job(db, job_type: str, idempotency_key: str, payload: dict, available_at=None) -> m.BackgroundJob:
    if job_type not in JOB_TYPES:
        raise ValueError(f"Unsupported background job type: {job_type}")
    existing = db.query(m.BackgroundJob).filter(
        m.BackgroundJob.idempotency_key == idempotency_key
    ).first()
    if existing:
        if existing.state == "FAILED":
            existing.state = "QUEUED"
            existing.attempts = 0
            existing.available_at = datetime.utcnow()
            existing.lease_until = None
            existing.last_error = None
            existing.updated_at = datetime.utcnow()
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
        existing = db.query(m.BackgroundJob).filter(
            m.BackgroundJob.idempotency_key == idempotency_key
        ).first()
        if existing:
            return existing
        raise


def background_worker_status() -> dict[str, bool]:
    with _workers_lock:
        workers = {worker.job_type: worker._thread.is_alive() for worker in _workers}
    return {job_type: workers.get(job_type, False) for job_type in JOB_TYPES}


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
            .first()
        )
        if not candidate:
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
        claimed = db.execute(
            update(m.BackgroundJob)
            .where(*filters)
            .values(
                state="RUNNING",
                attempts=m.BackgroundJob.attempts + 1,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                updated_at=now,
            )
        )
        if claimed.rowcount != 1:
            db.rollback()
            return None
        job_id = candidate.id
        db.commit()
        return db.query(m.BackgroundJob).filter(m.BackgroundJob.id == job_id).first()

    def _run_job(self, db, job):
        from app.services import pipeline

        if self.job_type == "REVIEW":
            pipeline.process_review_job(db, job.payload)
        elif self.job_type == "CI_REFRESH":
            pipeline.process_ci_refresh_job(db, job.payload)
        elif self.job_type == "CI_NOTIFY":
            pipeline.process_ci_notification_job(db, job.payload)
        elif self.job_type == "REVIEW_STATUS":
            pipeline.process_review_status_job(db, job.payload)

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
                m.Evidence.source == "gemini",
            )
            .first()
        )
        return evidence.content if evidence else "AI analysis failed; manual review required."

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
                try:
                    self._log_job(db, job, "RUNNING")
                    self._run_job(db, job)
                    failure_reason = self._analysis_failure_reason(db, job)
                    job.state = "COMPLETED"
                    job.last_error = None
                    job.lease_until = None
                    job.updated_at = datetime.utcnow()
                    db.commit()
                    self._log_job(db, job, "COMPLETED", failure_reason)
                except Exception as exc:
                    db.rollback()
                    job = db.query(m.BackgroundJob).filter(m.BackgroundJob.id == job.id).first()
                    if job:
                        job.last_error = str(exc)[:4000]
                        job.lease_until = None
                        job.updated_at = datetime.utcnow()
                        if job.attempts >= MAX_ATTEMPTS:
                            job.state = "FAILED"
                        else:
                            job.state = "QUEUED"
                            delay = min(2 ** job.attempts, 60)
                            job.available_at = datetime.utcnow() + timedelta(seconds=delay)
                        db.commit()
                    logger.exception(
                        "Background job failed",
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
            except Exception:
                db.rollback()
                logger.exception("Background worker iteration failed: type=%s", self.job_type)
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
