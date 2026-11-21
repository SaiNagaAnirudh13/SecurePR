"""Independent AI-review and persistent CI-monitoring workflows."""

import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator, cast

import requests
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.integrations.ai_provider import AIAnalysisResult, AIInvalidResponse
from app.integrations.github_client import github_client
from app.integrations.groq_client import groq_client
from app.models import models as m
from app.services import (
    auto_merge,
    ci_status,
    circuit_breaker,
    decision_engine,
    impact_simulator,
    policy_engine,
    risk_engine,
)
from app.services.background_jobs import enqueue_job
from app.services.comment_formatter import (
    format_comment,
    format_review_status_comment,
)

logger = logging.getLogger("securepr.pipeline")
CI_FETCH_ERRORS = (
    requests.RequestException,
    TimeoutError,
    ValueError,
    RuntimeError,
)
settings = get_settings()
REVISION_OPERATION_LEASE_SECONDS = 300
_local_revision_locks: dict[tuple[str, int, str, str], threading.RLock] = {}
_local_revision_locks_guard = threading.Lock()


class RevisionLockUnavailable(TimeoutError):
    """Raised when another worker currently owns a revision operation."""


def _for_update(query, entity):
    if query.session.get_bind().dialect.name == "mssql":
        return query.with_hint(entity, "WITH (UPDLOCK, ROWLOCK)", dialect_name="mssql")
    return query.with_for_update()


def _claim_revision_operation(
    db: Session,
    repository_id: str,
    pr_number: int,
    head_sha: str | None,
    operation: str,
) -> tuple[str, str, str]:
    """Claim a revision operation in a short row-locking transaction."""
    try:
        pr_query = (
            db.query(m.PullRequest)
            .filter(
                m.PullRequest.repository_id == repository_id,
                m.PullRequest.github_pr_number == pr_number,
            )
        )
        pr = _for_update(pr_query, m.PullRequest).first()
        if pr is None:
            raise RuntimeError(
                "Cannot claim a revision operation for a missing pull request."
            )
        revision_sha = head_sha or pr.head_sha
        if not revision_sha:
            raise RuntimeError(
                "Cannot claim a revision operation without a commit SHA."
            )

        identity = {
            "repository_id": repository_id,
            "pull_request_id": pr.id,
            "head_sha": revision_sha,
            "operation": operation,
        }
        if db.get_bind().dialect.name == "postgresql":
            db.execute(
                pg_insert(m.RevisionOperation)
                .values(id=m.gen_uuid(), **identity)
                .on_conflict_do_nothing(
                    index_elements=[
                        m.RevisionOperation.repository_id,
                        m.RevisionOperation.pull_request_id,
                        m.RevisionOperation.head_sha,
                        m.RevisionOperation.operation,
                    ]
                )
            )
        else:
            try:
                with db.begin_nested():
                    db.add(m.RevisionOperation(**identity))
                    db.flush()
            except IntegrityError:
                pass

        operation_query = (
            db.query(m.RevisionOperation)
            .filter_by(**identity)
        )
        revision_operation = _for_update(
            operation_query, m.RevisionOperation
        ).one()
        now = datetime.utcnow()
        if (
            revision_operation.state == "RUNNING"
            and revision_operation.lease_until
            and revision_operation.lease_until > now
        ):
            raise RevisionLockUnavailable(
                "Another worker is processing this PR revision operation."
            )
        if revision_operation.available_at > now:
            raise RevisionLockUnavailable(
                "This PR revision operation is waiting for its retry backoff."
            )

        owner_token = m.gen_uuid()
        revision_operation.state = "RUNNING"
        revision_operation.owner_token = owner_token
        revision_operation.attempts += 1
        revision_operation.lease_until = now + timedelta(
            seconds=REVISION_OPERATION_LEASE_SECONDS
        )
        revision_operation.updated_at = now
        db.commit()
        return cast(str, revision_operation.id), owner_token, revision_sha
    except Exception:
        db.rollback()
        raise


def _finish_revision_operation(
    db: Session,
    operation_id: str,
    owner_token: str,
    error: Exception | None = None,
) -> None:
    try:
        operation_query = (
            db.query(m.RevisionOperation)
            .filter(m.RevisionOperation.id == operation_id)
        )
        revision_operation = _for_update(
            operation_query, m.RevisionOperation
        ).first()
        if (
            revision_operation is None
            or revision_operation.state != "RUNNING"
            or revision_operation.owner_token != owner_token
        ):
            db.rollback()
            logger.warning(
                "Revision operation lease expired before completion: operation_id=%s",
                operation_id,
            )
            return

        now = datetime.utcnow()
        revision_operation.state = "QUEUED" if error else "COMPLETED"
        revision_operation.owner_token = None
        revision_operation.lease_until = None
        revision_operation.last_error = str(error)[:4000] if error else None
        revision_operation.available_at = now
        revision_operation.updated_at = now
        db.commit()
    except Exception:
        db.rollback()
        raise


@contextmanager
def _revision_finalization_lock(
    db: Session,
    repository_id: str,
    pr_number: int,
    *,
    head_sha: str | None = None,
    operation: str = "review-finalization",
    job_id: str | None = None,
    retry_count: int = 0,
) -> Iterator[None]:
    """Hold only a durable lease; row-lock transactions end before yielding."""
    fields = {
        "repository_id": repository_id,
        "pr_number": pr_number,
        "head_sha": head_sha or "unknown",
        "operation": operation,
        "job_id": job_id or "unknown",
        "retry_count": retry_count,
    }
    local_lock = None
    if db.get_bind().dialect.name == "sqlite":
        lock_key = (repository_id, pr_number, head_sha or "", operation)
        with _local_revision_locks_guard:
            local_lock = _local_revision_locks.setdefault(
                lock_key, threading.RLock()
            )
        local_lock.acquire()
    try:
        db.commit()
        operation_id, owner_token, claimed_sha = _claim_revision_operation(
            db, repository_id, pr_number, head_sha, operation
        )
        fields["head_sha"] = claimed_sha
        logger.info(
            "PR revision operation claimed: repository_id=%(repository_id)s "
            "pr_number=%(pr_number)s head_sha=%(head_sha)s operation=%(operation)s "
            "job_id=%(job_id)s retry_count=%(retry_count)s",
            fields,
            extra=fields,
        )
        try:
            yield
        except Exception as exc:
            db.rollback()
            _finish_revision_operation(db, operation_id, owner_token, exc)
            raise
        else:
            db.commit()
            _finish_revision_operation(db, operation_id, owner_token)
            logger.info(
                "PR revision operation completed: repository_id=%s pr_number=%s "
                "head_sha=%s operation=%s job_id=%s retry_count=%s",
                repository_id,
                pr_number,
                claimed_sha,
                operation,
                job_id or "unknown",
                retry_count,
                extra=fields,
            )
    finally:
        if local_lock:
            local_lock.release()


def _verify_latest_pr_revision(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    expected_sha: str,
) -> tuple[bool, str | None]:
    """Verify GitHub first, then confirm the local revision under a short row lock."""
    db.commit()
    owner, repo_name = repo.full_name.split("/", 1)
    latest_pr = github_client.get_pull_request(
        installation_id, owner, repo_name, cast(int, pr.github_pr_number)
    )
    latest_sha = latest_pr.get("head_sha")
    if latest_pr.get("state") != "open" or latest_sha != expected_sha:
        return False, latest_sha if isinstance(latest_sha, str) else None

    return _confirm_pr_revision_in_database(db, pr, expected_sha), latest_sha


def _confirm_pr_revision_in_database(
    db: Session, pr: m.PullRequest, expected_sha: str
) -> bool:
    db.commit()
    try:
        current_query = (
            db.query(m.PullRequest)
            .filter(m.PullRequest.id == pr.id)
        )
        current_pr = _for_update(current_query, m.PullRequest).first()
        current = bool(
            current_pr
            and current_pr.state == "open"
            and current_pr.head_sha == expected_sha
        )
        db.commit()
        return current
    except Exception:
        db.rollback()
        raise


def _log_analysis_timing(
    stage: str, started_at: float, repo: m.Repository, pr: m.PullRequest
) -> None:
    duration_ms = round((time.perf_counter() - started_at) * 1000)
    logger.info(
        "SecurePR analysis timing: stage=%s duration_ms=%s repository=%s pr_number=%s head_sha=%s",
        stage,
        duration_ms,
        repo.full_name,
        pr.github_pr_number,
        pr.head_sha,
        extra={
            "timing_event": "analysis",
            "stage": stage,
            "duration_ms": duration_ms,
            "repository": repo.full_name,
            "pr_number": pr.github_pr_number,
            "head_sha": pr.head_sha,
        },
    )


def _queue_current_revision(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    head_sha: str,
) -> None:
    pr.head_sha = head_sha
    pr.ai_state = "AI_PENDING"
    pr.ai_error_code = None
    pr.ai_retryable_at = None
    pr.ai_attempt_id = None
    pr.updated_at = datetime.utcnow()  # noqa: DTZ003
    db.commit()
    _ci_revision(db, repo, pr, head_sha)
    enqueue_job(
        db,
        "REVIEW",
        f"review:{repo.id}:{pr.id}:{head_sha}",
        {
            "repository_id": repo.id,
            "pull_request_id": pr.id,
            "installation_id": installation_id,
            "head_sha": head_sha,
        },
    )
    enqueue_job(
        db,
        "CI_REFRESH",
        f"ci-initial:{repo.id}:{pr.id}:{head_sha}",
        {
            "repository_id": repo.id,
            "pull_request_id": pr.id,
            "installation_id": installation_id,
            "head_sha": head_sha,
            "event_name": "pull_request",
            "reason": "initial",
        },
    )
    _queue_review_status_update(db, repo, pr, head_sha)
    db.commit()


def _get_or_create_policy_row(db: Session, repo: m.Repository, policy_doc: dict) -> str:
    name = policy_doc.get("name", "default")
    version = policy_doc.get("version", 1)
    row = (
        db.query(m.Policy)
        .filter(
            m.Policy.repository_id == repo.id,
            m.Policy.name == name,
            m.Policy.version == version,
        )
        .first()
    )
    if row:
        return row.id
    row = m.Policy(
        repository_id=repo.id,
        name=name,
        version=version,
        content_json=policy_doc,
        is_active=True,
    )
    db.add(row)
    db.flush()
    return row.id


def _ci_revision(
    db: Session, repo: m.Repository, pr: m.PullRequest, head_sha: str
) -> m.CIRevision:
    revision = (
        db.query(m.CIRevision)
        .filter(
            m.CIRevision.repository_id == repo.id,
            m.CIRevision.pull_request_id == pr.id,
            m.CIRevision.head_sha == head_sha,
        )
        .first()
    )
    if revision:
        return revision
    revision = m.CIRevision(
        repository_id=repo.id,
        pull_request_id=pr.id,
        head_sha=head_sha,
        state="UNKNOWN",
    )
    db.add(revision)
    db.flush()
    return revision


def _ci_records(db: Session, revision: m.CIRevision) -> list[dict]:
    rows = (
        db.query(m.CIExecution)
        .filter(m.CIExecution.ci_revision_id == revision.id)
        .all()
    )
    return [
        {
            "identity": row.identity,
            "kind": row.kind,
            "provider": row.provider,
            "app_id": row.app_id,
            "context": row.context,
            "name": row.name,
            "status": row.status,
            "conclusion": row.conclusion,
            "url": row.external_url,
            "html_url": row.external_url,
            "sha": row.head_sha,
        }
        for row in rows
    ]


def _publication(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    head_sha: str,
    message_type: str,
    marker: str,
) -> m.ReviewPublication:
    record = (
        db.query(m.ReviewPublication)
        .filter(
            m.ReviewPublication.repository_id == repo.id,
            m.ReviewPublication.pull_request_id == pr.id,
            m.ReviewPublication.head_sha == head_sha,
            m.ReviewPublication.message_type == message_type,
        )
        .first()
    )
    if record:
        return record
    record = m.ReviewPublication(
        repository_id=repo.id,
        pull_request_id=pr.id,
        head_sha=head_sha,
        message_type=message_type,
        state="PENDING",
        marker=marker,
    )
    db.add(record)
    db.flush()
    return record


def _mark_publication_published(
    db: Session,
    record: m.ReviewPublication,
    result: dict,
) -> None:
    if not result.get("succeeded", False):
        return
    payload = result.get("payload")
    github_id = None
    if isinstance(payload, dict):
        github_id = payload.get("id")
    record.state = "PUBLISHED"
    record.github_message_id = str(github_id) if github_id is not None else None
    record.published_at = datetime.utcnow()


def _queue_review_status_update(
    db: Session, repo: m.Repository, pr: m.PullRequest, head_sha: str
) -> None:
    revision = (
        db.query(m.CIRevision)
        .filter(
            m.CIRevision.repository_id == repo.id,
            m.CIRevision.pull_request_id == pr.id,
            m.CIRevision.head_sha == head_sha,
        )
        .first()
    )
    records = _ci_records(db, revision) if revision else []
    review = (
        db.query(m.Review)
        .filter(
            m.Review.pull_request_id == pr.id,
            m.Review.head_sha == head_sha,
            m.Review.is_ci_wait_placeholder == False,
        )
        .order_by(m.Review.created_at.desc())
        .first()
    )
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "state": revision.state if revision else "UNKNOWN",
                "error": revision.error if revision else None,
                "required_checks": revision.required_checks if revision else [],
                "discovery_complete": revision.discovery_complete
                if revision
                else False,
                "records": records,
                "review": (
                    {
                        "id": review.id,
                        "decision": review.decision.value,
                        "github_action_confirmed": review.github_action_confirmed,
                        "auto_merge_status": review.auto_merge_status,
                    }
                    if review
                    else None
                ),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    enqueue_job(
        db,
        "REVIEW_STATUS",
        f"review-status:{pr.id}:{head_sha}:{fingerprint}",
        {
            "repository_id": repo.id,
            "pull_request_id": pr.id,
            "installation_id": repo.installation_id,
            "head_sha": head_sha,
        },
    )


def _queue_review_reconciliation(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    revision: m.CIRevision,
    *,
    include_missing_review: bool = False,
    is_reevaluation: bool = False,
    previous_review_id: str | None = None,
) -> None:
    review = (
        db.query(m.Review)
        .filter(
            m.Review.pull_request_id == pr.id,
            m.Review.head_sha == revision.head_sha,
            m.Review.is_ci_wait_placeholder == False,  # noqa: E712
        )
        .order_by(m.Review.created_at.desc())
        .first()
    )
    if not review and not include_missing_review:
        return
    fingerprint_values = {
        "state": revision.state,
        "error": revision.error,
        "required_checks": revision.required_checks or [],
        "discovery_complete": revision.discovery_complete,
        "records": _ci_records(db, revision),
        "review_id": review.id if review else None,
        "decision": review.decision.value if review else None,
    }
    if is_reevaluation:
        fingerprint_values["is_reevaluation"] = True
        fingerprint_values["previous_review_id"] = previous_review_id
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_values, sort_keys=True).encode("utf-8")
    ).hexdigest()
    reeval_key = (
        f":reevaluation:{previous_review_id or 'new'}" if is_reevaluation else ""
    )
    enqueue_job(
        db,
        "REVIEW",
        f"review-reconcile:{repo.id}:{pr.id}:{revision.head_sha}:{fingerprint}{reeval_key}",
        {
            "repository_id": repo.id,
            "pull_request_id": pr.id,
            "installation_id": repo.installation_id,
            "head_sha": revision.head_sha,
            "is_reevaluation": is_reevaluation,
            "previous_review_id": previous_review_id,
        },
    )


def _queue_late_ci_failures(
    db: Session, repo: m.Repository, pr: m.PullRequest, review: m.Review
) -> None:
    revision = (
        db.query(m.CIRevision)
        .filter(
            m.CIRevision.repository_id == repo.id,
            m.CIRevision.pull_request_id == pr.id,
            m.CIRevision.head_sha == review.head_sha,
        )
        .first()
    )
    if not revision:
        return
    _queue_ci_notifications(db, repo, pr, revision)


def _queue_ci_notifications(
    db: Session, repo: m.Repository, pr: m.PullRequest, revision: m.CIRevision
) -> None:
    """CI failure details are included in the authoritative review, not extra comments."""
    return


def _refresh_ci_before_approval(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    revision: m.CIRevision,
) -> ci_status.CIStatus:
    """Refresh GitHub's exact-revision CI state before calculating a decision."""
    owner, repo_name = repo.full_name.split("/", 1)
    db.commit()
    try:
        checks, required_checks, errors = _fetch_ci_checks(
            installation_id,
            owner,
            repo_name,
            revision.head_sha,
            pr.base_branch,
        )
    except CI_FETCH_ERRORS as exc:
        logger.exception(
            "Final CI verification failed before decision: repository=%s pr=%s sha=%s",
            repo.full_name,
            pr.github_pr_number,
            revision.head_sha,
        )
        checks, required_checks, errors = [], [], [str(exc)]

    if errors:
        _upsert_ci_execution_records(
            db, revision, checks, revision.head_sha, remove_missing=False
        )
        revision.error = "; ".join(errors)[:4000]
        revision.discovery_complete = False
    else:
        _upsert_ci_execution_records(db, revision, checks, revision.head_sha)
        revision.error = None
        revision.required_checks = required_checks
        revision.discovery_complete = True
        revision.last_verified_at = datetime.utcnow()

    records = _ci_records(db, revision)
    evaluated = ci_status.evaluate_ci_status(
        records,
        fetch_error=revision.error,
        discovery_complete=revision.discovery_complete,
        required_checks=revision.required_checks,
    )
    revision.state = evaluated.state
    revision.updated_at = datetime.utcnow()
    if errors:
        revision.retry_count += 1
        retry_cap = max(settings.CI_API_MAX_RETRIES, 1)
        delay = min(
            5 * (2 ** min(revision.retry_count - 1, retry_cap - 1)),
            120,
        )
        enqueue_job(
            db,
            "CI_REFRESH",
            f"ci-retry:{revision.id}:{revision.retry_count}",
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "installation_id": installation_id,
                "head_sha": revision.head_sha,
                "reason": "retry",
            },
            datetime.utcnow() + timedelta(seconds=delay),
        )
    elif evaluated.state in {
        "IN_PROGRESS",
        "UNKNOWN",
        "NOT_CONFIGURED",
        "NON_SUCCESS",
    }:
        revision.poll_count += 1
        poll_cap = max(settings.CI_FALLBACK_MAX_POLLS, 1)
        delay = min(
            settings.CI_FALLBACK_POLL_INTERVAL_SECONDS
            * (2 ** min(revision.poll_count - 1, poll_cap - 1, 5)),
            300,
        )
        enqueue_job(
            db,
            "CI_REFRESH",
            f"ci-poll:{revision.id}:{revision.poll_count}",
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "installation_id": installation_id,
                "head_sha": revision.head_sha,
                "reason": "poll",
            },
            datetime.utcnow() + timedelta(seconds=delay),
        )
    db.commit()
    return evaluated


def _publish_existing_review(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    review: m.Review,
) -> None:
    if review.github_action_confirmed:
        return
    body_row = (
        db.query(m.Evidence)
        .filter(
            m.Evidence.review_id == review.id,
            m.Evidence.evidence_type == "github_review_body",
        )
        .first()
    )
    review_id = review.id
    if not body_row:
        raise RuntimeError(
            f"Review {review_id} has no persisted GitHub publication body."
        )
    if review.github_review_state not in ("APPROVE", "COMMENT", "REQUEST_CHANGES"):
        raise RuntimeError(f"Review {review_id} has no valid GitHub review event.")

    if review.github_review_state == "APPROVE":
        revision = _ci_revision(db, repo, pr, review.head_sha)
        ci_evaluation = _refresh_ci_before_approval(
            db, repo, pr, installation_id, revision
        )
        if ci_evaluation.state != "PASSED":
            _queue_review_reconciliation(db, repo, pr, revision)
            _queue_review_status_update(db, repo, pr, review.head_sha)
            db.commit()
            return

    publication_type = f"FINAL_{review.decision.value}_{review.id}"
    marker = f"<!-- securepr-review:{pr.id}:{review.head_sha}:{review.decision.value}:{review.id} -->"
    owner, repo_name = repo.full_name.split("/", 1)
    pr_number = pr.github_pr_number
    publication = _publication(db, repo, pr, review.head_sha, publication_type, marker)
    if publication.state == "PUBLISHED":
        return
    body = body_row.content
    review_event = review.github_review_state
    review_head_sha = review.head_sha
    db.commit()
    try:
        live_pr = github_client.get_pull_request(
            installation_id, owner, repo_name, pr_number
        )
        if live_pr.get("state") != "open" or live_pr.get("head_sha") != review_head_sha:
            latest_sha = live_pr.get("head_sha")
            if isinstance(latest_sha, str) and latest_sha and latest_sha != pr.head_sha:
                _queue_current_revision(db, repo, pr, installation_id, latest_sha)
            return
        if not _confirm_pr_revision_in_database(db, pr, review_head_sha):
            return
        result = github_client.post_review(
            installation_id,
            owner,
            repo_name,
            pr_number,
            body,
            review_event,
            idempotency_marker=marker,
            commit_id=review_head_sha,
        )
        db.add(
            m.GithubAction(
                review_id=review_id,
                action_type="submit_review",
                request_payload={"event": review_event, "marker": marker},
                response_status=result.get("status_code"),
                response_payload=result.get("payload")
                if isinstance(result.get("payload"), dict)
                else None,
                succeeded=result.get("succeeded", False),
            )
        )
        review.github_action_confirmed = result.get("succeeded", False)
        _mark_publication_published(db, publication, result)
        db.commit()
        if not review.github_action_confirmed:
            raise RuntimeError(
                f"GitHub rejected review publication for review {review_id}."
            )
        if review.github_action_confirmed:
            _queue_late_ci_failures(db, repo, pr, review)
    except Exception:
        db.rollback()
        logger.exception("Failed to publish AI review: review_id=%s", review_id)
        raise


def _dismiss_review_approval(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    review: m.Review,
) -> str | None:
    publication = (
        db.query(m.ReviewPublication)
        .filter(
            m.ReviewPublication.repository_id == repo.id,
            m.ReviewPublication.pull_request_id == pr.id,
            m.ReviewPublication.head_sha == review.head_sha,
            m.ReviewPublication.message_type
            == (f"FINAL_{review.decision.value}_{review.id}"),
        )
        .first()
    )
    if not publication or not publication.github_message_id:
        return "The prior GitHub review could not be located for dismissal."
    owner, repo_name = repo.full_name.split("/", 1)
    pr_number = pr.github_pr_number
    review_id = review.id
    github_message_id = publication.github_message_id
    db.commit()
    try:
        result = github_client.dismiss_review(
            installation_id,
            owner,
            repo_name,
            pr_number,
            github_message_id,
            "SecurePR verification changed; this review no longer reflects the current CI decision.",
        )
    except CI_FETCH_ERRORS as exc:
        logger.exception(
            "Could not dismiss stale SecurePR review: review_id=%s", review_id
        )
        result = {
            "succeeded": False,
            "status_code": None,
            "payload": {"message": str(exc)},
        }
    db.add(
        m.GithubAction(
            review_id=review.id,
            action_type="dismiss_stale_review",
            response_status=result.get("status_code"),
            response_payload=result.get("payload")
            if isinstance(result.get("payload"), dict)
            else None,
            succeeded=result.get("succeeded", False),
        )
    )
    db.commit()
    if result.get("succeeded", False):
        return None
    payload = result.get("payload")
    reason = payload.get("message") if isinstance(payload, dict) else None
    return "GitHub could not dismiss the prior SecurePR review" + (
        f": {reason}" if reason else "."
    )


def run_analysis(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    is_reevaluation: bool = False,
    previous_review_id: str | None = None,
    expected_head_sha: str | None = None,
    trigger: str = "analysis",
    event_name: str | None = None,
    job_id: str | None = None,
    retry_count: int = 0,
) -> m.Review | None:
    """Run AI analysis for one revision without waiting on any CI operation."""
    repository_id = cast(str, repo.id)
    pull_request_id = cast(str, pr.id)
    pr_number = cast(int, pr.github_pr_number)
    with _revision_finalization_lock(
        db,
        repository_id,
        pr_number,
        head_sha=cast(str | None, expected_head_sha or pr.head_sha),
        operation=(
            f"review-analysis-reevaluation:{previous_review_id or 'initial'}"
            if is_reevaluation
            else "review-analysis"
        ),
        job_id=job_id,
        retry_count=retry_count,
    ):
        db.commit()
        db.expire_all()
        current_pr = (
            db.query(m.PullRequest).filter(m.PullRequest.id == pull_request_id).first()
        )
        current_repo = (
            db.query(m.Repository).filter(m.Repository.id == repository_id).first()
        )
        if current_pr is None or current_repo is None:
            return None
        if expected_head_sha and current_pr.head_sha != expected_head_sha:
            logger.info(
                "Ignoring stale review request: repository=%s pr=%s expected_sha=%s current_sha=%s",
                current_repo.full_name,
                pr_number,
                expected_head_sha,
                current_pr.head_sha,
            )
            return None
        if not is_reevaluation and previous_review_id is None:
            existing = (
                db.query(m.Review)
                .filter(
                    m.Review.pull_request_id == current_pr.id,
                    m.Review.head_sha == current_pr.head_sha,
                    m.Review.is_ci_wait_placeholder == False,  # noqa: E712
                )
                .order_by(m.Review.created_at.desc())
                .first()
            )
            if existing:
                revision = (
                    db.query(m.CIRevision)
                    .filter(
                        m.CIRevision.repository_id == current_repo.id,
                        m.CIRevision.pull_request_id == current_pr.id,
                        m.CIRevision.head_sha == current_pr.head_sha,
                    )
                    .first()
                )
                ci_evidence = (
                    db.query(m.Evidence)
                    .filter(
                        m.Evidence.review_id == existing.id,
                        m.Evidence.evidence_type == "ci_result",
                    )
                    .first()
                )
                try:
                    prior_ci_state = (
                        json.loads(ci_evidence.content).get("state")
                        if ci_evidence
                        else None
                    )
                except (TypeError, json.JSONDecodeError):
                    prior_ci_state = None
                should_reconcile = bool(
                    revision
                    and prior_ci_state != revision.state
                    and (
                        revision.state in {"PASSED", "FAILED"}
                        or existing.decision == m.DecisionStatus.APPROVED
                    )
                )
                if should_reconcile:
                    db.commit()
                    return _run_analysis_locked(
                        db,
                        current_repo,
                        current_pr,
                        installation_id,
                        is_reevaluation=True,
                        previous_review_id=cast(str, existing.id),
                        trigger="ci_reconciliation",
                        job_id=job_id,
                        retry_count=retry_count,
                    )
                if (
                    existing.ai_state == "AI_UNAVAILABLE"
                    and existing.ai_retryable_at is not None
                    and existing.ai_retryable_at <= datetime.utcnow()
                ):
                    db.commit()
                    return _run_analysis_locked(
                        db,
                        current_repo,
                        current_pr,
                        installation_id,
                        is_reevaluation=True,
                        previous_review_id=cast(str, existing.id),
                        trigger="provider_retry",
                        job_id=job_id,
                        retry_count=retry_count,
                    )
                if existing.decision == m.DecisionStatus.WAITING_FOR_CI:
                    return existing
                db.commit()
                _publish_existing_review(
                    db, current_repo, current_pr, installation_id, existing
                )
                return existing
        return _run_analysis_locked(
            db,
            current_repo,
            current_pr,
            installation_id,
            is_reevaluation=is_reevaluation,
            previous_review_id=cast(str | None, previous_review_id),
            trigger=trigger,
            event_name=event_name,
            job_id=job_id,
            retry_count=retry_count,
        )


def _run_analysis_locked(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    *,
    is_reevaluation: bool = False,
    previous_review_id: str | None = None,
    trigger: str = "analysis",
    event_name: str | None = None,
    job_id: str | None = None,
    retry_count: int = 0,
) -> m.Review | None:
    started_clock = time.perf_counter()
    started_at = datetime.utcnow()
    repository_full_name = repo.full_name
    pr_number = cast(int, pr.github_pr_number)
    db.commit()
    owner, repo_name = repository_full_name.split("/", 1)
    try:
        live_pr = github_client.get_pull_request(
            installation_id, owner, repo_name, pr_number
        )
    except Exception:
        logger.exception(
            "Could not verify PR before AI review: repository=%s pr=%s",
            repo.full_name,
            pr.github_pr_number,
        )
        raise
    if live_pr.get("state") != "open":
        pr.state = live_pr.get("state") or pr.state
        db.commit()
        return None
    live_sha = live_pr.get("head_sha")
    if not isinstance(live_sha, str) or not live_sha:
        raise ValueError("GitHub pull-request response is missing its head SHA.")
    if live_sha != pr.head_sha:
        logger.info(
            "Superseding stale analysis before publication: repository=%s pr=%s "
            "job_sha=%s live_sha=%s",
            repo.full_name,
            pr.github_pr_number,
            pr.head_sha,
            live_sha,
        )
        _queue_current_revision(db, repo, pr, installation_id, live_sha)
        return None

    stale_auto_merge = (
        db.query(m.Review.id)
        .filter(
            m.Review.pull_request_id == pr.id,
            m.Review.head_sha != pr.head_sha,
            m.Review.github_review_state == "APPROVE",
            m.Review.github_action_confirmed == True,  # noqa: E712
        )
        .first()
    )
    db.commit()
    if stale_auto_merge:
        owner, repo_name = repository_full_name.split("/", 1)
        try:
            auto_merge.cancel_pending_auto_merge(
                installation_id=installation_id,
                owner=owner,
                repo_name=repo_name,
                pr_number=pr_number,
                db=db,
                pull_request_id=cast(str, pr.id),
                new_head_sha=cast(str | None, pr.head_sha),
            )
        except Exception:
            logger.exception(
                "Could not reconcile auto-merge before processing a newer PR "
                "revision: repository=%s pr_number=%s head_sha=%s",
                repository_full_name,
                pr_number,
                pr.head_sha,
            )
            raise

    policy_doc = policy_engine.load_repo_policy(repo.full_name)
    policy_id = _get_or_create_policy_row(db, repo, policy_doc)
    revision = _ci_revision(db, repo, pr, pr.head_sha)
    db.commit()
    ci_records = _ci_records(db, revision)
    ci_execution_records = [
        item for item in ci_records if item["kind"] != "check_suite"
    ]
    ci_evaluation = ci_status.evaluate_ci_status(
        ci_records,
        fetch_error=revision.error,
        discovery_complete=revision.discovery_complete,
        required_checks=revision.required_checks,
    )
    if ci_evaluation.state == "PASSED":
        db.commit()
        ci_evaluation = _refresh_ci_before_approval(
            db, repo, pr, installation_id, revision
        )
        ci_records = _ci_records(db, revision)
        ci_execution_records = [
            item for item in ci_records if item["kind"] != "check_suite"
        ]

    analysis_ci_state = ci_evaluation.state
    analysis_complete = True
    changed_files_error = None
    db.commit()
    try:
        changed_files = github_client.get_pr_files(
            installation_id, owner, repo_name, pr.github_pr_number
        )
    except Exception as exc:
        logger.exception(
            "Could not fetch changed PR files: repository=%s pr=%s",
            repo.full_name,
            pr.github_pr_number,
        )
        changed_files = []
        analysis_complete = False
        changed_files_error = f"Could not retrieve changed files: {exc}"

    if ci_evaluation.state == "PASSED":
        policy_eval = policy_engine.evaluate_policy(
            policy_doc, changed_files, ci_execution_records, pr.author_login
        )
    else:
        policy_eval = policy_engine.PolicyEvaluation(
            policy_version=policy_doc.get("version", 1),
            policy_name=policy_doc.get("name", "default"),
        )

    pr.ai_state = "AI_RUNNING"
    pr.ai_error_code = None
    pr.ai_retryable_at = None
    pr.ai_attempt_id = m.gen_uuid()
    db.commit()

    ai_scores = None
    ai_analysis_error = None
    ai_error_code = None
    ai_provider_name = "groq"
    ai_model_name = settings.GROQ_MODEL
    ai_input_tokens = None
    ai_output_tokens = None
    if analysis_complete:
        ai_started = time.perf_counter()
        try:
            provider_result = groq_client.score_dimensions(changed_files)
            if not isinstance(provider_result, AIAnalysisResult):
                raise AIInvalidResponse("The AI provider returned an invalid result.")
            ai_scores = provider_result.analysis
            ai_provider_name = provider_result.provider
            ai_model_name = provider_result.model
            ai_input_tokens = provider_result.input_tokens
            ai_output_tokens = provider_result.output_tokens
            logger.info(
                "AI review completed: provider=%s model=%s repository=%s "
                "pr_number=%s head_sha=%s job_id=%s input_tokens=%s "
                "output_tokens=%s duration_ms=%s retry_count=%s",
                ai_provider_name,
                ai_model_name,
                repo.full_name,
                pr.github_pr_number,
                pr.head_sha,
                job_id or "unknown",
                ai_input_tokens,
                ai_output_tokens,
                round((time.perf_counter() - ai_started) * 1000),
                retry_count,
                extra={
                    "provider": ai_provider_name,
                    "model": ai_model_name,
                    "repository": repo.full_name,
                    "pr_number": pr.github_pr_number,
                    "head_sha": pr.head_sha,
                    "job_id": job_id or "unknown",
                    "input_tokens": ai_input_tokens,
                    "output_tokens": ai_output_tokens,
                    "duration_ms": round((time.perf_counter() - ai_started) * 1000),
                    "retry_count": retry_count,
                },
            )
        except Exception as exc:  # noqa: BLE001
            pr.ai_state = getattr(exc, "ai_state", "AI_FAILED")
            ai_error_code = getattr(exc, "error_code", "AI_ANALYSIS_FAILED")
            pr.ai_error_code = ai_error_code
            pr.ai_retryable_at = getattr(exc, "retryable_at", None)
            ai_analysis_error = "Groq analysis could not be completed."
            logger.error(
                "AI analysis failed: repository=%s pr=%s sha=%s state=%s error_code=%s exception_type=%s",
                repo.full_name,
                pr.github_pr_number,
                pr.head_sha,
                pr.ai_state,
                ai_error_code,
                type(exc).__name__,
                extra={
                    "repository": repo.full_name,
                    "pr_number": pr.github_pr_number,
                    "head_sha": pr.head_sha,
                    "ai_state": pr.ai_state,
                    "error_code": ai_error_code,
                    "exception_type": type(exc).__name__,
                    "provider": ai_provider_name,
                    "model": ai_model_name,
                    "retryable_at": pr.ai_retryable_at,
                    "duration_ms": round((time.perf_counter() - ai_started) * 1000),
                    "job_id": job_id or "unknown",
                    "retry_count": retry_count,
                },
            )
        else:
            pr.ai_state = "AI_COMPLETED"
            pr.ai_error_code = None
            pr.ai_retryable_at = None
    else:
        pr.ai_state = "AI_FAILED"
        ai_error_code = "PR_FILES_UNAVAILABLE"
        pr.ai_error_code = ai_error_code
        pr.ai_retryable_at = None
        ai_analysis_error = "The changed files could not be retrieved for AI analysis."
    db.commit()

    if ai_scores is not None:
        heuristic_scores = risk_engine.compute_deterministic_scores(
            changed_files, ci_execution_records
        )
        heuristic_scores["policy_compliance"] = (
            risk_engine.compute_policy_compliance_score(policy_eval.violations)
        )
        blended = risk_engine.blend_with_ai(heuristic_scores, ai_scores)
        assessment = risk_engine.compute_overall(blended)
    else:
        rationale = "Not evaluated because validated AI analysis is unavailable."
        blended = {
            dimension: risk_engine.DimensionScore(
                dimension=dimension,
                score=0,
                rationale=rationale,
                source="not_evaluated",
            )
            for dimension in risk_engine.RISK_DIMENSIONS
        }
        assessment = risk_engine.RiskAssessment(
            scores=blended, overall_score=0, category="NOT_EVALUATED"
        )

    findings: list[dict] = list(ai_scores.get("findings", [])) if ai_scores else []
    for path in policy_eval.blocked_paths_touched:
        findings.append(
            {
                "dimension": "policy_compliance",
                "severity": "CRITICAL",
                "title": "Blocked path modified",
                "description": f"'{path}' matches a blocked-path rule.",
                "file_path": path,
                "is_mandatory_blocker": True,
            }
        )
    if ci_evaluation.state == "FAILED":
        failed_records = [
            item
            for item in ci_execution_records
            if ci_status.classify_execution(item.get("status"), item.get("conclusion"))
            == "failed"
        ]
        policy_eval.violations.append("A discovered CI execution failed.")
        for record in failed_records:
            findings.append(
                {
                    "dimension": "verification",
                    "severity": "HIGH",
                    "title": f"CI failed: {record.get('name') or record.get('context') or 'Unnamed check'}",
                    "description": (
                        f"{record.get('provider') or 'CI provider'} reported "
                        f"{record.get('conclusion')} for this revision."
                    ),
                    "file_path": None,
                    "is_mandatory_blocker": True,
                }
            )
    for dim, score in blended.items():
        if score.score >= 70:
            findings.append(
                {
                    "dimension": dim,
                    "severity": "HIGH",
                    "title": f"High {dim.replace('_', ' ')} risk",
                    "description": score.rationale,
                    "file_path": None,
                    "is_mandatory_blocker": False,
                }
            )

    impact_items = impact_simulator.simulate(changed_files) if ai_scores else []
    cb_tripped, cb_reason = (
        circuit_breaker.is_tripped(db, repo.id, pr.author_login)
        if ci_evaluation.state == "PASSED"
        else (False, None)
    )
    author = (
        db.query(m.User).filter(m.User.github_login == pr.author_login).first()
        if ci_evaluation.state == "PASSED"
        else None
    )
    eligibility = None
    if author:
        eligibility = (
            db.query(m.AutoApprovalEligibility)
            .filter(
                m.AutoApprovalEligibility.repository_id == repo.id,
                m.AutoApprovalEligibility.user_id == author.id,
            )
            .first()
        )
    db.commit()
    latest_pr = github_client.get_pull_request(
        installation_id, owner, repo_name, pr_number
    )
    latest_sha = latest_pr.get("head_sha")
    if latest_pr.get("state") != "open":
        pr.state = latest_pr.get("state") or pr.state
        db.commit()
        return None
    if latest_sha != pr.head_sha:
        if isinstance(latest_sha, str) and latest_sha:
            _queue_current_revision(db, repo, pr, installation_id, latest_sha)
        logger.info(
            "Superseding in-flight analysis: repository=%s pr=%s reviewed_sha=%s current_sha=%s",
            repo.full_name,
            pr.github_pr_number,
            live_sha,
            latest_sha,
        )
        return None

    if ci_evaluation.state == "PASSED":
        db.commit()
        ci_evaluation = _refresh_ci_before_approval(
            db, repo, pr, installation_id, revision
        )

    final_ci_revision = (
        db.query(m.CIRevision)
        .filter(
            m.CIRevision.repository_id == repo.id,
            m.CIRevision.pull_request_id == pr.id,
            m.CIRevision.head_sha == pr.head_sha,
        )
        .first()
    )
    if final_ci_revision:
        ci_records = _ci_records(db, final_ci_revision)
        ci_execution_records = [
            item for item in ci_records if item["kind"] != "check_suite"
        ]
        ci_evaluation = ci_status.evaluate_ci_status(
            ci_records,
            fetch_error=final_ci_revision.error,
            discovery_complete=final_ci_revision.discovery_complete,
            required_checks=final_ci_revision.required_checks,
        )

    if analysis_ci_state != "PASSED" and ci_evaluation.state == "PASSED":
        _queue_review_reconciliation(
            db,
            repo,
            pr,
            final_ci_revision or revision,
            include_missing_review=True,
            is_reevaluation=is_reevaluation,
            previous_review_id=previous_review_id,
        )
        _queue_review_status_update(db, repo, pr, pr.head_sha)
        db.commit()
        _log_analysis_timing("total", started_clock, repo, pr)
        return None

    decision = decision_engine.decide(
        decision_engine.DecisionInput(
            overall_score=assessment.overall_score,
            risk_category=assessment.category,
            findings=findings,
            policy_violations=policy_eval.violations,
            policy_warnings=policy_eval.warnings,
            requires_human_review_by_policy=policy_eval.requires_human_review,
            circuit_breaker_tripped=cb_tripped,
            circuit_breaker_reason=cb_reason,
            author_eligible=bool(eligibility and eligibility.is_enabled),
            author_risk_threshold=eligibility.max_risk_threshold
            if eligibility
            else None,
            analysis_complete=ai_scores is not None,
            ai_state=pr.ai_state,
            ci_state=ci_evaluation.state,
            failed_checks=[
                item
                for item in ci_execution_records
                if ci_status.classify_execution(
                    item.get("status"), item.get("conclusion")
                )
                == "failed"
            ],
        )
    )
    if changed_files_error:
        decision.reasons.append(changed_files_error)
    if ai_analysis_error:
        decision.reasons.append(ai_analysis_error)

    current_revision, latest_sha = _verify_latest_pr_revision(
        db, repo, pr, installation_id, cast(str, pr.head_sha)
    )
    if not current_revision:
        if latest_sha and latest_sha != pr.head_sha:
            _queue_current_revision(db, repo, pr, installation_id, latest_sha)
        logger.info(
            "Discarding review decision for a stale PR revision: repository=%s "
            "pr=%s reviewed_sha=%s current_sha=%s",
            repo.full_name,
            pr_number,
            pr.head_sha,
            latest_sha or "unknown",
        )
        return None

    review = m.Review(
        pull_request_id=pr.id,
        policy_id=policy_id,
        head_sha=pr.head_sha,
        is_reevaluation=is_reevaluation,
        superseded_review_id=previous_review_id,
        ai_state=pr.ai_state,
        ai_error_code=ai_error_code,
        ai_retryable_at=pr.ai_retryable_at,
        ai_attempt_id=pr.ai_attempt_id,
        ai_provider=ai_provider_name,
        ai_model=ai_model_name,
        ai_input_tokens=ai_input_tokens,
        ai_output_tokens=ai_output_tokens,
        analysis_started_at=started_at,
    )
    db.add(review)
    try:
        db.flush()
    except SQLAlchemyError:
        db.rollback()
        logger.exception(
            "Review persistence failed; refusing to publish a GitHub decision: "
            "repository=%s pr=%s review_id=%s",
            repo.full_name,
            pr.github_pr_number,
            review.id,
        )
        raise
    for finding in findings:
        db.add(m.Finding(review_id=review.id, **finding))
    for item in impact_items:
        db.add(
            m.ImpactPrediction(
                review_id=review.id,
                component_type=item.component_type,
                component_name=item.component_name,
                confidence=item.confidence,
                evidence_summary=item.evidence_summary,
                potential_consequence=item.potential_consequence,
            )
        )
    for changed_file in changed_files:
        db.add(
            m.ChangedFile(
                review_id=review.id,
                file_path=changed_file["file_path"],
                change_type=changed_file.get("change_type"),
                additions=changed_file.get("additions", 0),
                deletions=changed_file.get("deletions", 0),
                is_blocked_path=changed_file["file_path"]
                in policy_eval.blocked_paths_touched,
                patch_excerpt=changed_file.get("patch_excerpt"),
            )
        )
    if ai_scores:
        for dimension, score in blended.items():
            db.add(
                m.Evidence(
                    review_id=review.id,
                    evidence_type="dimension_rationale",
                    source=score.source,
                    content=f"{dimension}: {score.rationale}",
                )
            )
    if ai_scores and ai_scores.get("summary"):
        db.add(
            m.Evidence(
                review_id=review.id,
                evidence_type="ai_rationale",
                source="ai_provider",
                content=ai_scores["summary"],
            )
        )
    if not analysis_complete:
        db.add(
            m.Evidence(
                review_id=review.id,
                evidence_type="error",
                source="github_fetch",
                content=changed_files_error,
            )
        )
    if ai_analysis_error:
        db.add(
            m.Evidence(
                review_id=review.id,
                evidence_type="error",
                source="ai_provider",
                content=f"{ai_analysis_error} Error code: {ai_error_code}.",
            )
        )
    db.add(
        m.Evidence(
            review_id=review.id,
            evidence_type="ci_result",
            source="securepr_ci_monitor",
            content=json.dumps(
                {
                    "state": ci_evaluation.state,
                    "sha": pr.head_sha,
                    "checks": ci_records,
                    "error": ci_evaluation.error,
                }
            ),
        )
    )
    review.score_change_surface = blended["change_surface"].score
    review.score_reversibility = blended["reversibility"].score
    review.score_data_security = blended["data_security"].score
    review.score_operational = blended["operational"].score
    review.score_verification = blended["verification"].score
    review.score_policy_compliance = blended["policy_compliance"].score
    review.overall_score = assessment.overall_score
    review.risk_category = assessment.category
    review.decision = decision.status
    review.decision_reasons = decision.reasons
    review.lifecycle_state = decision.status.value
    review.ci_verified_at = (
        datetime.utcnow() if ci_evaluation.state in {"PASSED", "FAILED"} else None
    )
    review.circuit_breaker_active = cb_tripped
    review.analysis_completed_at = datetime.utcnow() if ai_scores is not None else None
    if decision.status == m.DecisionStatus.WAITING_FOR_AI:
        review.lifecycle_state = "WAITING_FOR_AI"
    elif decision.status == m.DecisionStatus.REVIEW_UNAVAILABLE:
        review.lifecycle_state = "REVIEW_UNAVAILABLE"

    dashboard_url = f"{settings.DASHBOARD_ORIGIN}/#/pr/{pr.id}"
    body = format_comment(
        decision=decision.status.value,
        reasons=decision.reasons,
        overall_score=assessment.overall_score,
        category=assessment.category,
        dimension_scores=blended,
        findings=findings,
        changed_files=changed_files,
        circuit_breaker_tripped=cb_tripped,
        circuit_breaker_reason=cb_reason,
        impact_items=impact_items,
        policy_name=policy_eval.policy_name,
        policy_version=policy_eval.policy_version,
        dashboard_url=dashboard_url,
        ci_checks=ci_execution_records,
        ci_state=ci_evaluation.state,
        analysis_error=changed_files_error or ai_analysis_error,
        ai_state=pr.ai_state,
        ai_error_code=ai_error_code,
        ai_retryable_at=pr.ai_retryable_at,
    )
    if decision.status == m.DecisionStatus.WAITING_FOR_CI:
        previous_review = (
            db.query(m.Review).filter(m.Review.id == previous_review_id).first()
            if previous_review_id
            else db.query(m.Review)
            .filter(
                m.Review.pull_request_id == pr.id,
                m.Review.head_sha != review.head_sha,
                m.Review.github_review_state.in_(("APPROVE", "REQUEST_CHANGES")),
                m.Review.github_action_confirmed == True,  # noqa: E712
            )
            .order_by(m.Review.created_at.desc())
            .first()
        )
        if previous_review:
            if previous_review.github_review_state in ("APPROVE", "REQUEST_CHANGES"):
                dismissal_error = _dismiss_review_approval(
                    db, repo, pr, installation_id, previous_review
                )
                if dismissal_error:
                    decision.reasons.append(dismissal_error)
        db.add(
            m.Evidence(
                review_id=review.id,
                evidence_type="review_summary",
                source="securepr",
                content=body,
            )
        )
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception(
                "Review persistence failed before GitHub publication: "
                "repository=%s pr=%s review_id=%s",
                repo.full_name,
                pr.github_pr_number,
                review.id,
            )
            raise
        _queue_review_status_update(db, repo, pr, review.head_sha)
        db.commit()
        _log_analysis_timing("total", started_clock, repo, pr)
        return review

    event_map = {
        "APPROVED": "APPROVE",
        "BLOCKED": "REQUEST_CHANGES",
        "MANUAL_REVIEW": "COMMENT",
        "REVIEW_UNAVAILABLE": "COMMENT",
        "WAITING_FOR_AI": "COMMENT",
    }
    github_event = event_map[decision.status.value]
    publication_type = f"FINAL_{decision.status.value}_{review.id}"
    marker = (
        f"<!-- securepr-review:{pr.id}:{review.head_sha}:"
        f"{decision.status.value}:{review.id} -->"
    )
    body = f"{body}\n\n{marker}"
    review.github_review_state = github_event
    previous_review = (
        db.query(m.Review).filter(m.Review.id == previous_review_id).first()
        if previous_review_id
        else db.query(m.Review)
        .filter(
            m.Review.pull_request_id == pr.id,
            m.Review.head_sha != review.head_sha,
            m.Review.github_review_state.in_(("APPROVE", "REQUEST_CHANGES")),
            m.Review.github_action_confirmed == True,  # noqa: E712
        )
        .order_by(m.Review.created_at.desc())
        .first()
    )
    if previous_review and previous_review.github_review_state in (
        "APPROVE",
        "REQUEST_CHANGES",
    ):
        dismissal_error = _dismiss_review_approval(
            db, repo, pr, installation_id, previous_review
        )
        if dismissal_error:
            decision.reasons.append(dismissal_error)
    final_publication = _publication(
        db, repo, pr, review.head_sha, publication_type, marker
    )
    db.add(
        m.Evidence(
            review_id=review.id,
            evidence_type="github_review_body",
            source="securepr",
            content=body,
        )
    )
    try:
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception(
            "Review persistence failed before GitHub publication: "
            "repository=%s pr=%s review_id=%s",
            repo.full_name,
            pr.github_pr_number,
            review.id,
        )
        raise
    db.refresh(review)
    review_id = review.id
    review_head_sha = review.head_sha
    publication_is_published = final_publication.state == "PUBLISHED"
    db.commit()
    if publication_is_published:
        review.github_action_confirmed = True
        db.commit()
    else:
        try:
            live_pr = github_client.get_pull_request(
                installation_id, owner, repo_name, pr_number
            )
            if (
                live_pr.get("state") != "open"
                or live_pr.get("head_sha") != review_head_sha
            ):
                latest_sha = live_pr.get("head_sha")
                if (
                    isinstance(latest_sha, str)
                    and latest_sha
                    and latest_sha != pr.head_sha
                ):
                    _queue_current_revision(db, repo, pr, installation_id, latest_sha)
                else:
                    db.commit()
                return None
            if not _confirm_pr_revision_in_database(db, pr, review_head_sha):
                return None
            result = github_client.post_review(
                installation_id,
                owner,
                repo_name,
                pr_number,
                body,
                github_event,
                idempotency_marker=marker,
                commit_id=review_head_sha,
            )
            db.add(
                m.GithubAction(
                    review_id=review_id,
                    action_type="submit_review",
                    request_payload={"event": github_event},
                    response_status=result.get("status_code"),
                    response_payload=result.get("payload")
                    if isinstance(result.get("payload"), dict)
                    else None,
                    succeeded=result.get("succeeded", False),
                )
            )
            review.github_action_confirmed = result.get("succeeded", False)
            review.github_review_state = github_event
            _mark_publication_published(db, final_publication, result)
            db.commit()
            if not review.github_action_confirmed:
                raise RuntimeError(
                    f"GitHub rejected review publication for review {review.id}."
                )
            if review.github_action_confirmed:
                _queue_late_ci_failures(db, repo, pr, review)
        except Exception:
            logger.exception("Failed to publish AI review: review_id=%s", review.id)
            db.add(
                m.GithubAction(
                    review_id=review.id, action_type="submit_review", succeeded=False
                )
            )
            db.commit()
            raise

    if decision.status == m.DecisionStatus.APPROVED:
        db.commit()
        merge_result = auto_merge.attempt(
            db=db,
            repo=repo,
            pr=pr,
            review=review,
            installation_id=installation_id,
            findings=findings,
        )
        review.auto_merge_status = merge_result.status
        review.auto_merge_policy_revision = getattr(
            merge_result, "policy_revision", None
        )
        if merge_result.status not in (
            "DASHBOARD_CONFIGURATION_MISSING",
            "DISABLED_DASHBOARD_CONFIGURATION",
        ):
            review.auto_merge_attempted_at = datetime.utcnow()
        if merge_result.status == "MERGED":
            review.auto_merged_at = datetime.utcnow()
            review.auto_merge_failure_reason = None
        elif merge_result.status != "AUTO_MERGE_ENABLED":
            review.auto_merge_failure_reason = merge_result.reason
        db.add(
            m.GithubAction(
                review_id=review.id,
                action_type="request_github_auto_merge",
                request_payload={"method": settings.AUTO_MERGE_METHOD},
                response_status=merge_result.response_status,
                response_payload=merge_result.response_payload,
                succeeded=merge_result.status in ("AUTO_MERGE_ENABLED", "MERGED"),
            )
        )
        db.add(
            m.Evidence(
                review_id=review.id,
                evidence_type="auto_merge",
                source="securepr",
                content=json.dumps(
                    {
                        "status": merge_result.status,
                        "reason": merge_result.reason,
                        "merge_commit_sha": merge_result.merge_commit_sha,
                    }
                ),
            )
        )
        db.commit()
        if merge_result.status in {
            "CI_FAILED",
            "CI_PENDING",
            "CI_VERIFICATION_UNAVAILABLE",
        }:
            revision = _ci_revision(db, repo, pr, review.head_sha)
            if merge_result.ci_checks is not None:
                _upsert_ci_execution_records(
                    db, revision, merge_result.ci_checks, review.head_sha
                )
                revision.required_checks = merge_result.required_checks or []
                revision.error = None
                revision.discovery_complete = True
                revision.last_verified_at = datetime.utcnow()
            else:
                revision.error = merge_result.reason
                revision.discovery_complete = False

            ci_records = _ci_records(db, revision)
            ci_evaluation = ci_status.evaluate_ci_status(
                ci_records,
                fetch_error=revision.error,
                discovery_complete=revision.discovery_complete,
                required_checks=revision.required_checks,
            )
            revision.state = ci_evaluation.state
            revision.updated_at = datetime.utcnow()
            review.ci_verified_at = (
                datetime.utcnow()
                if ci_evaluation.state in {"PASSED", "FAILED"}
                and revision.discovery_complete
                else None
            )
            old_review_state = review.github_review_state
            dismissal_error = None
            if old_review_state == "APPROVE":
                dismissal_error = _dismiss_review_approval(
                    db, repo, pr, installation_id, review
                )
                if not dismissal_error:
                    review.github_review_state = "DISMISSED"
                    review.github_action_confirmed = False

            if ci_evaluation.state == "FAILED":
                decision.status = m.DecisionStatus.BLOCKED
                decision.reasons = [
                    "Approval is blocked because CI failed during final GitHub verification. "
                    f"AI risk score {assessment.overall_score}/100 "
                    f"({assessment.category}) is informational only."
                ]
                failed_checks = [
                    record
                    for record in ci_records
                    if ci_status.classify_execution(
                        record.get("status"), record.get("conclusion")
                    )
                    == "failed"
                ]
                for check in failed_checks:
                    reason = (
                        f"Failed check: {check.get('name') or check.get('context') or 'Unnamed check'} "
                        f"(provider: {check.get('provider') or 'Unknown provider'}; "
                        f"{check.get('conclusion') or 'failed'})"
                    )
                    if check.get("url") or check.get("html_url"):
                        reason += f" — {check.get('url') or check.get('html_url')}"
                    decision.reasons.append(reason)
                if dismissal_error:
                    decision.reasons.append(dismissal_error)
                    dismissal_finding = {
                        "dimension": "verification",
                        "severity": "HIGH",
                        "title": "Existing SecurePR approval could not be dismissed",
                        "description": dismissal_error,
                        "file_path": None,
                        "is_mandatory_blocker": True,
                    }
                    findings.append(dismissal_finding)
                    db.add(m.Finding(review_id=review.id, **dismissal_finding))
                review.decision = decision.status
                review.lifecycle_state = "BLOCKED"
                review.decision_reasons = decision.reasons
                review.github_review_state = "REQUEST_CHANGES"
                review.github_action_confirmed = False
                updated_body = format_comment(
                    decision=decision.status.value,
                    reasons=decision.reasons,
                    overall_score=assessment.overall_score,
                    category=assessment.category,
                    dimension_scores=blended,
                    findings=findings,
                    changed_files=changed_files,
                    circuit_breaker_tripped=cb_tripped,
                    circuit_breaker_reason=cb_reason,
                    impact_items=impact_items,
                    policy_name=policy_eval.policy_name,
                    policy_version=policy_eval.policy_version,
                    dashboard_url=dashboard_url,
                    ci_checks=[
                        record
                        for record in ci_records
                        if record["kind"] != "check_suite"
                    ],
                    ci_state="FAILED",
                    analysis_error=changed_files_error or ai_analysis_error,
                )
                corrective_marker = (
                    f"<!-- securepr-review:{pr.id}:{review.head_sha}:"
                    f"BLOCKED:{review.id} -->"
                )
                review_body = (
                    db.query(m.Evidence)
                    .filter(
                        m.Evidence.review_id == review.id,
                        m.Evidence.evidence_type == "github_review_body",
                    )
                    .first()
                )
                if review_body:
                    review_body.content = f"{updated_body}\n\n{corrective_marker}"
                db.add(
                    m.Evidence(
                        review_id=review.id,
                        evidence_type="ci_result_reconciliation",
                        source="securepr_ci_monitor",
                        content=json.dumps(
                            {
                                "state": ci_evaluation.state,
                                "sha": review.head_sha,
                                "checks": ci_records,
                                "error": ci_evaluation.error,
                            }
                        ),
                    )
                )
                db.commit()
                _publish_existing_review(db, repo, pr, installation_id, review)
            else:
                decision.status = m.DecisionStatus.WAITING_FOR_CI
                decision.reasons = [
                    (
                        "Approval is waiting for CI verification after GitHub reported "
                        f"{ci_evaluation.state.lower()}."
                    )
                ]
                if dismissal_error:
                    decision.reasons.append(dismissal_error)
                elif old_review_state == "APPROVE":
                    decision.reasons.append(
                        "The prior SecurePR approval was dismissed."
                    )
                review.decision = decision.status
                review.lifecycle_state = "WAITING_FOR_CI"
                review.decision_reasons = decision.reasons
                ci_evidence = (
                    db.query(m.Evidence)
                    .filter(
                        m.Evidence.review_id == review.id,
                        m.Evidence.evidence_type == "ci_result",
                    )
                    .first()
                )
                if ci_evidence:
                    ci_evidence.content = json.dumps(
                        {
                            "state": ci_evaluation.state,
                            "sha": review.head_sha,
                            "checks": ci_records,
                            "error": ci_evaluation.error,
                        }
                    )
                db.commit()
                _queue_review_status_update(db, repo, pr, review.head_sha)
                db.commit()
                _log_analysis_timing("total", started_clock, repo, pr)
                return review

    completion_pr_id = cast(str, pr.id)
    completion_pr_number = cast(int, pr.github_pr_number)
    completion_review_id = review.id
    completion_sha = review.head_sha
    completion_owner, repo_name = repo.full_name.split("/", 1)
    completion_marker = (
        f"<!-- securepr-complete:{completion_pr_id}:{completion_sha} -->"
    )
    completion_publication = _publication(
        db, repo, pr, completion_sha, "COMPLETION", completion_marker
    )
    if decision.status == m.DecisionStatus.REVIEW_UNAVAILABLE:
        completion_summary = (
            f"SecurePR received PR #{completion_pr_number}, but AI analysis was **NOT COMPLETED**. "
            "Review decision: **UNAVAILABLE**. Risk score: **Not Evaluated**. "
            f"CI status: **{ci_evaluation.state.replace('_', ' ').title()}**. "
            "**No AI approval decision has been made.** "
        )
    else:
        completion_summary = (
            f"SecurePR review complete for PR #{completion_pr_number}: "
            f"**{decision.status.value}** "
            f"(risk {assessment.overall_score}/100, {assessment.category}). "
            f"{'**Auto-merge:** ' + review.auto_merge_status + ' — ' + (review.auto_merge_failure_reason or 'GitHub confirmed the request.') + '. ' if review.auto_merge_status else ''}"
        )
    completion_message = (
        f"### SecurePR Review Complete\n\n"
        f"{completion_summary}"
        f"[View detailed review]({settings.DASHBOARD_ORIGIN}/#/pr/{completion_pr_id}) "
        f"\n\n{completion_marker}"
    )
    db.commit()
    latest_pr = github_client.get_pull_request(
        installation_id, completion_owner, repo_name, completion_pr_number
    )
    if latest_pr.get("state") != "open" or latest_pr.get("head_sha") != completion_sha:
        latest_sha = latest_pr.get("head_sha")
        if isinstance(latest_sha, str) and latest_sha and latest_sha != pr.head_sha:
            _queue_current_revision(db, repo, pr, installation_id, latest_sha)
        return review
    try:
        result = github_client.post_issue_comment(
            installation_id,
            completion_owner,
            repo_name,
            completion_pr_number,
            completion_message,
            idempotency_marker=completion_marker,
        )
        db.add(
            m.GithubAction(
                review_id=completion_review_id,
                action_type="post_completion_notification",
                request_payload={"marker": completion_marker},
                response_status=result.get("status_code"),
                response_payload=result.get("payload")
                if isinstance(result.get("payload"), dict)
                else None,
                succeeded=result.get("succeeded", False),
            )
        )
        _mark_publication_published(db, completion_publication, result)
        db.add(
            m.Notification(
                review_id=completion_review_id,
                recipient=completion_owner,
                channel="github",
                subject=f"PR #{completion_pr_number} {decision.status.value}",
                body=completion_message,
                delivered=result.get("succeeded", False),
            )
        )
        db.commit()
        if not result.get("succeeded", False):
            raise RuntimeError(
                f"GitHub rejected completion notification for review {review.id}."
            )
    except Exception:
        logger.exception("Failed to notify repository owner: review_id=%s", review.id)
        raise

    elapsed = time.perf_counter() - started_clock
    _queue_review_status_update(db, repo, pr, review.head_sha)
    db.commit()
    if elapsed >= settings.REVIEW_RESULT_DEADLINE_SECONDS:
        logger.warning(
            "AI review exceeded configured result deadline: elapsed_seconds=%.2f deadline_seconds=%s "
            "repository=%s pr=%s sha=%s review_id=%s",
            elapsed,
            settings.REVIEW_RESULT_DEADLINE_SECONDS,
            repo.full_name,
            pr.github_pr_number,
            pr.head_sha,
            review.id,
        )
    _log_analysis_timing("total", started_clock, repo, pr)
    return review


def process_review_job(db: Session, payload: dict) -> None:
    repo = (
        db.query(m.Repository)
        .filter(m.Repository.id == payload["repository_id"])
        .first()
    )
    pr = (
        db.query(m.PullRequest)
        .filter(m.PullRequest.id == payload["pull_request_id"])
        .first()
    )
    if not repo or not pr:
        raise ValueError("Review job references a missing repository or pull request.")
    owner, repo_name = repo.full_name.split("/", 1)
    pr_number = cast(int, pr.github_pr_number)
    db.commit()
    live_pr = github_client.get_pull_request(
        payload["installation_id"], owner, repo_name, pr_number
    )
    if live_pr.get("state") != "open":
        pr.state = live_pr.get("state") or pr.state
        db.commit()
        return
    live_sha = live_pr.get("head_sha")
    if not isinstance(live_sha, str) or not live_sha:
        raise ValueError("GitHub pull-request response is missing its head SHA.")
    if pr.head_sha != live_sha:
        logger.info(
            "Reconciling PR head before review job: repository=%s pr=%s stored_sha=%s live_sha=%s",
            repo.full_name,
            pr.github_pr_number,
            pr.head_sha,
            live_sha,
        )
        pr.head_sha = live_sha
        pr.ai_state = "AI_PENDING"
        pr.ai_error_code = None
        pr.ai_retryable_at = None
        pr.ai_attempt_id = None
        pr.state = "open"
        _ci_revision(db, repo, pr, live_sha)
        db.commit()
        enqueue_job(
            db,
            "CI_REFRESH",
            f"ci-initial:{repo.id}:{pr.id}:{live_sha}",
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "installation_id": payload["installation_id"],
                "head_sha": live_sha,
                "event_name": "pull_request",
                "reason": "reconciled",
            },
        )
        enqueue_job(
            db,
            "REVIEW_STATUS",
            f"review-status-initial:{pr.id}:{live_sha}",
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "installation_id": payload["installation_id"],
                "head_sha": live_sha,
            },
        )
        db.commit()
    run_analysis(
        db,
        repo,
        pr,
        payload["installation_id"],
        is_reevaluation=bool(payload.get("is_reevaluation", False)),
        previous_review_id=payload.get("previous_review_id"),
        expected_head_sha=live_sha,
        trigger="durable_review_job",
        job_id=payload.get("_job_id"),
        retry_count=payload.get("_retry_count", 0),
    )


def _fetch_ci_checks(
    installation_id: str,
    owner: str,
    repo_name: str,
    head_sha: str,
    base_branch: str,
):
    import concurrent.futures

    checks, errors = [], []
    sources = (
        ("check_runs", github_client.get_check_runs),
        ("commit_status", github_client.get_commit_statuses),
    )
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=3, thread_name_prefix="github-ci"
    )
    futures = {
        name: pool.submit(getter, installation_id, owner, repo_name, head_sha)
        for name, getter in sources
    }
    futures["required_checks"] = pool.submit(
        github_client.get_required_status_checks,
        installation_id,
        owner,
        repo_name,
        base_branch,
    )
    required_checks = []
    deadline = time.monotonic() + 10
    try:
        for name, future in futures.items():
            try:
                result = future.result(timeout=max(0.01, deadline - time.monotonic()))
                if name == "required_checks":
                    required_checks.extend(result)
                else:
                    checks.extend(result)
            except CI_FETCH_ERRORS as exc:
                errors.append(f"{name}: {exc}")
        return checks, required_checks, errors
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _upsert_ci_execution_records(
    db: Session,
    revision: m.CIRevision,
    discovered: list[dict],
    head_sha: str,
    *,
    remove_missing: bool = True,
) -> None:
    discovered_keys = set()
    for item in discovered:
        if item.get("sha") not in (None, head_sha):
            continue
        kind = item.get("kind") or "check_run"
        identity = str(item.get("identity") or item.get("id") or "")
        if not identity:
            identity = hashlib.sha256(
                json.dumps(item, sort_keys=True).encode("utf-8")
            ).hexdigest()
        discovered_keys.add((kind, identity))
        row = (
            db.query(m.CIExecution)
            .filter(
                m.CIExecution.ci_revision_id == revision.id,
                m.CIExecution.kind == kind,
                m.CIExecution.identity == identity,
            )
            .first()
        )
        if row is None:
            row = m.CIExecution(
                ci_revision_id=revision.id,
                kind=kind,
                identity=identity,
                head_sha=head_sha,
            )
            db.add(row)
        row.provider = item.get("provider")
        row.app_id = str(item.get("app_id")) if item.get("app_id") is not None else None
        row.context = item.get("context")
        row.name = item.get("name")
        row.status = item.get("status")
        row.conclusion = item.get("conclusion")
        row.external_url = item.get("url") or item.get("html_url")
    if remove_missing:
        existing = (
            db.query(m.CIExecution)
            .filter(m.CIExecution.ci_revision_id == revision.id)
            .all()
        )
        for row in existing:
            if (row.kind, row.identity) not in discovered_keys:
                db.delete(row)


def process_ci_refresh_job(db: Session, payload: dict) -> None:
    repo = (
        db.query(m.Repository)
        .filter(m.Repository.id == payload["repository_id"])
        .first()
    )
    pr = (
        db.query(m.PullRequest)
        .filter(m.PullRequest.id == payload["pull_request_id"])
        .first()
    )
    if not repo or not pr or pr.head_sha != payload["head_sha"] or pr.state != "open":
        return
    revision = _ci_revision(db, repo, pr, payload["head_sha"])
    db.commit()
    try:
        owner, repo_name = repo.full_name.split("/", 1)
        checks, required_checks, errors = _fetch_ci_checks(
            payload["installation_id"],
            owner,
            repo_name,
            payload["head_sha"],
            pr.base_branch,
        )
    except CI_FETCH_ERRORS as exc:
        checks, required_checks, errors = [], [], [str(exc)]

    db.expire_all()
    current_pr = db.query(m.PullRequest).filter(m.PullRequest.id == pr.id).first()
    if (
        not current_pr
        or current_pr.head_sha != payload["head_sha"]
        or current_pr.state != "open"
    ):
        return

    if errors:
        _upsert_ci_execution_records(
            db, revision, checks, payload["head_sha"], remove_missing=False
        )
        revision.error = "; ".join(errors)[:4000]
        revision.retry_count += 1
        revision.discovery_complete = False
        existing_records = _ci_records(db, revision)
        error_evaluation = ci_status.evaluate_ci_status(
            existing_records,
            fetch_error=revision.error,
            discovery_complete=False,
            required_checks=revision.required_checks,
        )
        revision.state = error_evaluation.state
        db.commit()
        _queue_review_reconciliation(db, repo, pr, revision)
        _queue_review_status_update(db, repo, pr, payload["head_sha"])
        db.commit()
        _queue_ci_notifications(db, repo, pr, revision)
        retry_backoff_cap = max(settings.CI_API_MAX_RETRIES, 1)
        delay = min(
            5 * (2 ** min(revision.retry_count - 1, retry_backoff_cap - 1)),
            120,
        )
        enqueue_job(
            db,
            "CI_REFRESH",
            f"ci-retry:{revision.id}:{revision.retry_count}",
            {**payload, "reason": "retry"},
            datetime.utcnow() + timedelta(seconds=delay),
        )
        db.commit()
        return

    revision.error = None
    revision.required_checks = required_checks
    revision.discovery_complete = True
    revision.last_verified_at = datetime.utcnow()
    discovered = checks
    _upsert_ci_execution_records(db, revision, discovered, payload["head_sha"])
    db.flush()
    records = _ci_records(db, revision)
    evaluated = ci_status.evaluate_ci_status(
        records,
        required_checks=revision.required_checks,
        discovery_complete=revision.discovery_complete,
    )
    revision.state = evaluated.state
    revision.updated_at = datetime.utcnow()
    db.commit()
    _queue_review_reconciliation(db, repo, pr, revision)
    db.commit()
    _queue_review_status_update(db, repo, pr, payload["head_sha"])
    db.commit()

    if evaluated.state in {"IN_PROGRESS", "UNKNOWN", "NOT_CONFIGURED", "NON_SUCCESS"}:
        revision.poll_count += 1
        poll_backoff_cap = max(settings.CI_FALLBACK_MAX_POLLS, 1)
        delay = min(
            settings.CI_FALLBACK_POLL_INTERVAL_SECONDS
            * (2 ** min(revision.poll_count - 1, poll_backoff_cap - 1, 5)),
            300,
        )
        enqueue_job(
            db,
            "CI_REFRESH",
            f"ci-poll:{revision.id}:{revision.poll_count}",
            {**payload, "reason": "poll"},
            datetime.utcnow() + timedelta(seconds=delay),
        )
        db.commit()

    _queue_ci_notifications(db, repo, pr, revision)


def process_review_status_job(db: Session, payload: dict) -> None:
    repo = (
        db.query(m.Repository)
        .filter(m.Repository.id == payload["repository_id"])
        .first()
    )
    pr = (
        db.query(m.PullRequest)
        .filter(m.PullRequest.id == payload["pull_request_id"])
        .first()
    )
    if not repo or not pr:
        raise ValueError(
            "Review status job references a missing repository or pull request."
        )
    with _revision_finalization_lock(
        db,
        cast(str, repo.id),
        cast(int, pr.github_pr_number),
        head_sha=payload.get("head_sha"),
        operation="review-status-publication",
        job_id=payload.get("_job_id"),
        retry_count=payload.get("_retry_count", 0),
    ):
        db.commit()
        db.expire_all()
        repo = (
            db.query(m.Repository)
            .filter(m.Repository.id == payload["repository_id"])
            .first()
        )
        pr = (
            db.query(m.PullRequest)
            .filter(m.PullRequest.id == payload["pull_request_id"])
            .first()
        )
        if not repo or not pr:
            return
        if pr.head_sha != payload["head_sha"]:
            logger.info(
                "Ignoring stale review-status job: repository=%s pr=%s "
                "job_sha=%s current_sha=%s job_id=%s",
                repo.full_name,
                pr.github_pr_number if pr else "unknown",
                payload["head_sha"],
                pr.head_sha if pr else "unknown",
                payload.get("_job_id", "unknown"),
            )
            return
        _publish_review_status_comment(db, payload, repo, pr)


def process_auto_merge_cancel_job(db: Session, payload: dict) -> None:
    repo = (
        db.query(m.Repository)
        .filter(m.Repository.id == payload["repository_id"])
        .first()
    )
    pr = (
        db.query(m.PullRequest)
        .filter(m.PullRequest.id == payload["pull_request_id"])
        .first()
    )
    if not repo or not pr:
        raise ValueError(
            "Auto-merge cancellation job references a missing repository or pull request."
        )
    with _revision_finalization_lock(
        db,
        cast(str, repo.id),
        cast(int, pr.github_pr_number),
        head_sha=cast(str | None, pr.head_sha),
        operation="auto-merge-cancellation",
        job_id=payload.get("_job_id"),
        retry_count=payload.get("_retry_count", 0),
    ):
        db.commit()
        db.expire_all()
        repo = (
            db.query(m.Repository)
            .filter(m.Repository.id == payload["repository_id"])
            .first()
        )
        pr = (
            db.query(m.PullRequest)
            .filter(m.PullRequest.id == payload["pull_request_id"])
            .first()
        )
        if not repo or not pr:
            return
        eligibility = auto_merge.saved_eligibility(db, repo, pr)
        if (
            eligibility is None
            or eligibility.policy_revision != payload["policy_revision"]
        ):
            logger.info(
                "Ignoring obsolete auto-merge cancellation: repository=%s pr=%s "
                "job_id=%s target_policy_revision=%s current_policy_revision=%s",
                repo.full_name,
                pr.github_pr_number,
                payload.get("_job_id", "unknown"),
                payload["policy_revision"],
                eligibility.policy_revision if eligibility else "missing",
            )
            return
        current_review = (
            db.query(m.Review)
            .filter(
                m.Review.pull_request_id == pr.id,
                m.Review.head_sha == pr.head_sha,
                m.Review.is_ci_wait_placeholder == False,
            )
            .order_by(m.Review.created_at.desc())
            .first()
        )
        still_authorized = bool(
            eligibility.is_enabled
            and 0 <= eligibility.max_risk_threshold <= 100
            and current_review
            and current_review.ai_state == "AI_COMPLETED"
            and current_review.decision == m.DecisionStatus.APPROVED
            and current_review.overall_score is not None
            and current_review.overall_score <= eligibility.max_risk_threshold
        )
        if still_authorized:
            return
        owner, repo_name = repo.full_name.split("/", 1)
        cancelled = auto_merge.cancel_pending_auto_merge(
            installation_id=cast(str, repo.installation_id),
            owner=owner,
            repo_name=repo_name,
            pr_number=cast(int, pr.github_pr_number),
            db=db,
            pull_request_id=cast(str, pr.id),
            new_head_sha=cast(str | None, pr.head_sha),
            cancellation_status="CANCELLED_DASHBOARD_CONFIGURATION",
            cancellation_reason=(
                "The saved dashboard configuration no longer authorizes "
                "this auto-merge request."
            ),
            stale_only=False,
        )
        db.add(
            m.AuditLog(
                actor="securepr",
                action="reconcile_github_auto_merge_policy",
                target_type="pull_request",
                target_id=pr.id,
                details_json={
                    "repository": repo.full_name,
                    "pr_number": pr.github_pr_number,
                    "head_sha": pr.head_sha,
                    "policy_revision": eligibility.policy_revision,
                    "job_id": payload.get("_job_id"),
                    "cancellation_result": "cancelled" if cancelled else "no_request",
                },
            )
        )
        db.commit()


def _publish_review_status_comment(
    db: Session, payload: dict, repo: m.Repository, pr: m.PullRequest
) -> None:
    if pr.head_sha != payload["head_sha"] or pr.state != "open":
        return
    revision = (
        db.query(m.CIRevision)
        .filter(
            m.CIRevision.repository_id == repo.id,
            m.CIRevision.pull_request_id == pr.id,
            m.CIRevision.head_sha == payload["head_sha"],
        )
        .first()
    )
    records = _ci_records(db, revision) if revision else []
    ci_summary = ci_status.evaluate_ci_status(
        records,
        fetch_error=revision.error if revision else None,
        discovery_complete=bool(revision and revision.discovery_complete),
        required_checks=revision.required_checks if revision else [],
    )
    executions = [record for record in records if record["kind"] != "check_suite"]
    review = (
        db.query(m.Review)
        .filter(
            m.Review.pull_request_id == pr.id,
            m.Review.head_sha == payload["head_sha"],
            m.Review.is_ci_wait_placeholder == False,  # noqa: E712
        )
        .order_by(m.Review.created_at.desc())
        .first()
    )
    findings = (
        [
            {
                "severity": finding.severity,
                "title": finding.title,
                "description": finding.description,
                "file_path": finding.file_path,
                "line_reference": finding.line_reference,
            }
            for finding in db.query(m.Finding)
            .filter(m.Finding.review_id == review.id)
            .all()
        ]
        if review
        else []
    )
    marker = f"<!-- securepr-status:{pr.id}:{payload['head_sha']} -->"
    body = format_review_status_comment(
        pr_number=pr.github_pr_number,
        head_sha=payload["head_sha"],
        ci_state=ci_summary.state,
        ci_checks=executions,
        ci_error=ci_summary.error,
        review=review,
        findings=findings,
        marker=marker,
    )
    owner, repo_name = repo.full_name.split("/", 1)
    pr_number = pr.github_pr_number
    waiting_publication = _publication(
        db, repo, pr, payload["head_sha"], "WAITING", marker
    )
    db.commit()
    current_revision, latest_sha = _verify_latest_pr_revision(
        db, repo, pr, payload["installation_id"], payload["head_sha"]
    )
    if not current_revision:
        if latest_sha and latest_sha != payload["head_sha"]:
            _queue_current_revision(
                db, repo, pr, payload["installation_id"], latest_sha
            )
        return
    result = github_client.post_issue_comment(
        payload["installation_id"],
        owner,
        repo_name,
        pr_number,
        body,
        idempotency_marker=marker,
    )
    _mark_publication_published(db, waiting_publication, result)
    db.add(
        m.GithubAction(
            review_id=review.id if review else None,
            action_type="update_review_status_comment",
            request_payload={"head_sha": payload["head_sha"], "marker": marker},
            response_status=result.get("status_code"),
            response_payload=result.get("payload")
            if isinstance(result.get("payload"), dict)
            else None,
            succeeded=result.get("succeeded", False),
        )
    )
    db.commit()
    if not result.get("succeeded", False):
        raise RuntimeError(
            f"GitHub rejected review status publication for PR #{pr.github_pr_number}."
        )


def process_ci_notification_job(db: Session, payload: dict) -> None:
    # Kept as a compatibility no-op: CI failure updates are not separate bot messages.
    return
