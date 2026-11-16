"""
Analysis Pipeline Orchestrator.

Wires together: GitHub fetch -> risk engine -> policy engine -> circuit
breaker -> impact simulator -> decision engine -> persistence -> GitHub
comment/review publish -> notifications.

CI discovery persists a lightweight WAITING_FOR_CI placeholder while GitHub
checks are running or the discovered result set is stabilizing. Check-run,
check-suite, and status webhooks trigger the same authoritative evaluator as
the fallback poller. A bounded timeout routes unavailable or indefinitely
pending CI to manual review. Both paths share a per-PR database lock, so only
one can publish the final result.

Every FINAL review run is persisted as a new, immutable Review row (see
models.Review). Re-evaluating a PR NEVER mutates or deletes a prior final
review; it creates a new row with is_reevaluation=True and
superseded_review_id pointing at the previous one, so decision replay always
has the original evidence available.
"""
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_session_factory
from app.integrations.gemini_client import gemini_client
from app.integrations.github_client import github_client
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
from app.services.comment_formatter import (
    format_auto_merge_comment,
    format_comment,
    format_waiting_comment,
)

logger = logging.getLogger("securepr.pipeline")
settings = get_settings()

# Local poller cancellation avoids keeping a fallback thread asleep after the
# final decision has been published. The revision lock below is separate and
# provides cross-worker serialization for review creation and publication.
_active_ci_waits: dict[tuple[str, str], threading.Event] = {}
_active_ci_waits_lock = threading.Lock()

_fallback_revision_locks: dict[tuple[str, int], threading.RLock] = {}
_fallback_revision_locks_guard = threading.Lock()


def _log_analysis_timing(
    stage: str,
    started_at: float,
    trigger: str,
    repo: m.Repository,
    pr: m.PullRequest,
) -> None:
    duration_ms = round((time.perf_counter() - started_at) * 1000)
    logger.info(
        "SecurePR analysis timing: stage=%s duration_ms=%s trigger=%s repository=%s "
        "pr_number=%s head_sha=%s",
        stage,
        duration_ms,
        trigger,
        repo.full_name,
        pr.github_pr_number,
        pr.head_sha,
        extra={
            "timing_event": "analysis",
            "stage": stage,
            "duration_ms": duration_ms,
            "trigger": trigger,
            "repository": repo.full_name,
            "pr_number": pr.github_pr_number,
            "head_sha": pr.head_sha,
        },
    )


@contextmanager
def _revision_finalization_lock(
    db: Session, repository_id: str, pr_number: int
) -> Iterator[None]:
    """Serialize analysis and publication for one PR across app workers.

    SQL Server and PostgreSQL use session-owned database locks on a dedicated
    connection, so commits performed by the review pipeline do not release them.
    SQLite uses a process lock plus the reviews table's unique constraint.
    """
    lock_key = (repository_id, pr_number)
    dialect = db.get_bind().dialect.name
    if dialect not in ("mssql", "postgresql"):
        with _fallback_revision_locks_guard:
            local_lock = _fallback_revision_locks.setdefault(
                lock_key, threading.RLock()
            )
        with local_lock:
            yield
        return

    resource_id = hashlib.sha256(
        f"{repository_id}:{pr_number}".encode("utf-8")
    ).hexdigest()
    resource = f"securepr:review:{resource_id}"
    connection = db.get_bind().connect()
    acquired = False
    try:
        if dialect == "postgresql":
            resource_key = int.from_bytes(
                hashlib.sha256(resource.encode("utf-8")).digest()[:8],
                byteorder="big",
                signed=True,
            )
            connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            connection.execute(
                text("SELECT pg_advisory_lock(:resource_key)"),
                {"resource_key": resource_key},
            )
        else:
            result = connection.execute(
                text(
                    """
                    DECLARE @lock_result int;
                    EXEC @lock_result = sys.sp_getapplock
                        @Resource = :resource,
                        @LockMode = 'Exclusive',
                        @LockOwner = 'Session',
                        @LockTimeout = 30000;
                    SELECT @lock_result;
                    """
                ),
                {"resource": resource},
            ).scalar()
            if result is None or result < 0:
                raise RuntimeError(
                    f"Could not acquire PR review lock (sp_getapplock={result})"
                )
        acquired = True
        yield
    finally:
        if acquired:
            try:
                if dialect == "postgresql":
                    result = connection.execute(
                        text("SELECT pg_advisory_unlock(:resource_key)"),
                        {"resource_key": resource_key},
                    ).scalar()
                    if not result:
                        logger.error(
                            "PR review lock release failed: repository_id=%s pr_number=%s",
                            repository_id,
                            pr_number,
                        )
                else:
                    result = connection.execute(
                        text(
                            """
                            DECLARE @lock_result int;
                            EXEC @lock_result = sys.sp_releaseapplock
                                @Resource = :resource,
                                @LockOwner = 'Session';
                            SELECT @lock_result;
                            """
                        ),
                        {"resource": resource},
                    ).scalar()
                    if result is None or result < 0:
                        logger.error(
                            "PR review lock release failed: repository_id=%s pr_number=%s result=%s",
                            repository_id,
                            pr_number,
                            result,
                        )
            except Exception:
                logger.exception(
                    "PR review lock release raised: repository_id=%s pr_number=%s",
                    repository_id,
                    pr_number,
                )
        connection.close()


def _cancel_ci_wait_poller(pull_request_id: str, head_sha: str) -> None:
    key = (pull_request_id, head_sha)
    with _active_ci_waits_lock:
        stop_event = _active_ci_waits.get(key)
    if stop_event:
        stop_event.set()


def _fetch_ci_checks(
    installation_id: str, owner: str, repo_name: str, head_sha: str
) -> tuple[list[dict], list[dict], list[str]]:
    """Read GitHub CI representations for one immutable commit.

    GitHub Actions uses check runs, while several CI vendors still publish
    legacy commit statuses. Suites are discovery signals, not substitutes for
    their individual check runs. Any API failure remains visible to evaluation.
    """
    checks: list[dict] = []
    suites: list[dict] = []
    fetch_errors: list[str] = []
    getters = (
        ("check runs", github_client.get_check_runs),
        ("commit statuses", github_client.get_commit_statuses),
        ("check suites", github_client.get_check_suites),
    )
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="github-ci") as executor:
        futures = [
            (source, executor.submit(getter, installation_id, owner, repo_name, head_sha))
            for source, getter in getters
        ]
        for source, future in futures:
            try:
                result = future.result()
                if source == "check suites":
                    suites.extend(result)
                else:
                    checks.extend(result)
            except Exception:
                fetch_errors.append(source)
                logger.exception("Failed to fetch GitHub %s for head_sha=%s", source, head_sha)
    return checks, suites, fetch_errors


def _read_ci_result(db: Session, review_id: str | None) -> dict | None:
    if not review_id:
        return None
    evidence = (
        db.query(m.Evidence)
        .filter(m.Evidence.review_id == review_id, m.Evidence.evidence_type == "ci_result")
        .first()
    )
    if not evidence or not evidence.content:
        return None
    try:
        value = json.loads(evidence.content)
    except (TypeError, ValueError):
        logger.warning("Stored CI evidence is malformed: review_id=%s", review_id)
        return None
    if isinstance(value, list):
        return {"checks": value, "check_suites": []}
    return value if isinstance(value, dict) else None


def _store_ci_result(db: Session, review_id: str, result: dict) -> None:
    evidence = (
        db.query(m.Evidence)
        .filter(m.Evidence.review_id == review_id, m.Evidence.evidence_type == "ci_result")
        .first()
    )
    content = json.dumps(result)
    if evidence:
        evidence.source = "github"
        evidence.content = content
    else:
        db.add(m.Evidence(
            review_id=review_id,
            evidence_type="ci_result",
            source="github",
            content=content,
        ))


def _ci_snapshot(
    db: Session,
    placeholder: m.Review | None,
    checks: list[dict],
    suites: list[dict],
    head_sha: str,
    fetch_errors: list[str],
    required_checks_pending: list[str],
    force_final: bool,
) -> tuple[dict, ci_status.CIStatus]:
    now = datetime.utcnow()
    previous = _read_ci_result(db, placeholder.id if placeholder else None) or {}
    signature = ci_status.snapshot_signature(checks, suites)
    previous_signature = previous.get("signature")
    try:
        last_change_at = datetime.fromisoformat(
            str(previous.get("last_change_at", ""))
        )
        if last_change_at.tzinfo is not None:
            last_change_at = last_change_at.replace(tzinfo=None)
    except ValueError:
        last_change_at = now
    if signature != previous_signature:
        last_change_at = now

    started_at = placeholder.ci_wait_started_at if placeholder else None
    if started_at is None:
        try:
            started_at = datetime.fromisoformat(
                str(previous.get("started_at", ""))
            )
            if started_at.tzinfo is not None:
                started_at = started_at.replace(tzinfo=None)
        except ValueError:
            started_at = now

    elapsed = max(0.0, (now - started_at).total_seconds())
    quiet_seconds = max(0.0, (now - last_change_at).total_seconds())
    timed_out = force_final or elapsed >= settings.CI_WAIT_TIMEOUT_SECONDS
    error = (
        "GitHub CI APIs were unavailable: " + ", ".join(fetch_errors)
        if fetch_errors
        else None
    )
    status = ci_status.evaluate_ci_status(
        checks,
        suites,
        required_checks_pending=required_checks_pending,
        fetch_error=error,
        quiet_seconds=quiet_seconds,
        stabilization_seconds=settings.CI_DISCOVERY_STABILIZATION_SECONDS,
        timed_out=timed_out,
    )
    result = {
        "state": status.state,
        "sha": head_sha,
        "checks": checks,
        "check_suites": suites,
        "signature": signature,
        "started_at": started_at.isoformat(),
        "last_change_at": last_change_at.isoformat(),
        "updated_at": now.isoformat(),
        "error": status.error,
    }
    return result, status


def _persist_ci_wait(db: Session, repo: m.Repository, pr: m.PullRequest, installation_id: str,
                     owner: str, repo_name: str, policy_id: str, policy_eval, existing_placeholder,
                     changed_files: list[dict] | None = None, changed_files_error: bool = False,
                     extra_reason: str | None = None, pending_checks: list[str] | None = None,
                     ci_result: dict | None = None) -> m.Review:
    """Persist one idempotent, explicitly non-final CI waiting state."""
    pending = pending_checks if pending_checks is not None else policy_eval.mandatory_checks_pending
    reasons = list(policy_eval.warnings)
    reasons.append(
        f"Waiting for CI: {', '.join(pending)}. "
        "SecurePR will finalize after the discovered checks finish and the results stabilize."
    )
    if extra_reason:
        reasons.append(extra_reason)
    if existing_placeholder:
        existing_placeholder.ci_wait_pending_checks = pending
        existing_placeholder.decision_reasons = reasons
        if ci_result:
            _store_ci_result(db, existing_placeholder.id, ci_result)
        db.commit()
        return existing_placeholder

    review = m.Review(
        pull_request_id=pr.id, policy_id=policy_id, head_sha=pr.head_sha,
        analysis_started_at=datetime.utcnow(), is_ci_wait_placeholder=True,
        ci_wait_started_at=datetime.utcnow(), ci_wait_pending_checks=pending,
        decision=m.DecisionStatus.WAITING_FOR_CI, decision_reasons=reasons,
        risk_category="NOT_EVALUATED",
    )
    db.add(review)
    db.flush()
    for f in changed_files or []:
        db.add(m.ChangedFile(review_id=review.id, file_path=f["file_path"], change_type=f.get("change_type"),
                              additions=f.get("additions", 0), deletions=f.get("deletions", 0),
                              is_blocked_path=False, patch_excerpt=f.get("patch_excerpt")))
    db.add(m.Evidence(review_id=review.id, evidence_type="ci_wait", source="securepr",
                      content=f"Waiting for: {', '.join(pending)}"))
    if ci_result:
        _store_ci_result(db, review.id, ci_result)
    if changed_files_error:
        db.add(m.Evidence(
            review_id=review.id,
            evidence_type="ci_wait_file_fetch",
            source="github",
            content="Changed-file list is unavailable; no file-count claim was made.",
        ))
    db.commit()
    db.refresh(review)

    if settings.POST_CI_WAITING_COMMENT:
        try:
            body = format_waiting_comment(
                pending_checks=pending,
                changed_files_count=None if changed_files_error else len(changed_files or []),
                changed_files_unavailable=changed_files_error,
                policy_name=policy_eval.policy_name, policy_version=policy_eval.policy_version,
                dashboard_url=f"{settings.DASHBOARD_ORIGIN}/#/pr/{pr.id}",
                ci_state=ci_result.get("state") if ci_result else None,
            )
            result = github_client.post_review(installation_id, owner, repo_name, pr.github_pr_number, body, "COMMENT")
            db.add(m.GithubAction(review_id=review.id, action_type="post_waiting_comment",
                                   response_status=result.get("status_code"), succeeded=result.get("succeeded", False)))
            db.commit()
        except Exception:
            logger.exception("Failed to post CI-wait comment: review_id=%s", review.id)
    return review


def _finalize_unverified_ci_wait(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    owner: str,
    repo_name: str,
    policy_id: str,
    policy_eval,
    existing_placeholder,
    changed_files: list[dict],
    ci_result: dict,
    reason: str,
) -> m.Review:
    """Close a timed-out wait locally when GitHub revision verification fails."""
    ci_result["state"] = "TIMEOUT"
    ci_result["error"] = reason
    review = _persist_ci_wait(
        db,
        repo,
        pr,
        installation_id,
        owner,
        repo_name,
        policy_id,
        policy_eval,
        existing_placeholder,
        changed_files=changed_files,
        pending_checks=["Final GitHub verification unavailable"],
        ci_result=ci_result,
        extra_reason=reason,
    )
    review.is_ci_wait_placeholder = False
    review.ci_wait_pending_checks = []
    review.decision = m.DecisionStatus.MANUAL_REVIEW
    review.decision_reasons = [
        reason,
        "No GitHub review was published because the current PR revision could not be verified.",
    ]
    review.analysis_completed_at = datetime.utcnow()
    _store_ci_result(db, review.id, ci_result)
    db.commit()
    db.refresh(review)
    return review


def run_background_analysis(
    repository_id: str,
    pull_request_id: str,
    installation_id: str,
    expected_head_sha: str | None = None,
) -> None:
    """Run webhook-triggered analysis with a task-owned session and reloaded ORM rows.

    FastAPI request sessions and ORM instances are not safe to retain after a
    response has been sent, so webhook background tasks cross this boundary
    using IDs only.
    """
    logger.info("Background analysis started: repository_id=%s pull_request_id=%s installation_id=%s",
                repository_id, pull_request_id, installation_id)
    db = get_session_factory()()
    try:
        repo = db.query(m.Repository).filter(m.Repository.id == repository_id).first()
        pr = db.query(m.PullRequest).filter(m.PullRequest.id == pull_request_id).first()
        if not repo or not pr:
            logger.error("Background analysis aborted because persisted records were not found: "
                         "repository_id=%s repository_found=%s pull_request_id=%s pull_request_found=%s",
                         repository_id, bool(repo), pull_request_id, bool(pr))
            return
        if pr.repository_id != repo.id:
            logger.error("Background analysis aborted because PR does not belong to repository: "
                         "repository_id=%s pull_request_id=%s actual_repository_id=%s",
                         repository_id, pull_request_id, pr.repository_id)
            return

        review = run_analysis(
            db,
            repo,
            pr,
            installation_id,
            expected_head_sha=expected_head_sha,
            trigger="pull_request",
        )

        if review is None:
            return
        if review.decision == m.DecisionStatus.WAITING_FOR_CI:
            _spawn_ci_wait_poller(
                repository_id, pull_request_id, installation_id, review.head_sha
            )
        else:
            _cancel_ci_wait_poller(pull_request_id, review.head_sha)
    except Exception:
        db.rollback()
        logger.exception("Background analysis failed: repository_id=%s pull_request_id=%s installation_id=%s",
                         repository_id, pull_request_id, installation_id)
    finally:
        db.close()
        logger.info("Background analysis database session closed: repository_id=%s pull_request_id=%s",
                    repository_id, pull_request_id)


def handle_check_event_finalization(
    repository_id: str,
    head_sha: str,
    installation_id: str,
    event_name: str = "check_event",
) -> None:
    """Revalidate a completed CI event against every currently matching PR.

    The check does not require a waiting row: an event can race the initial
    PR-analysis task before that row is committed. In either case run_analysis
    re-fetches authoritative checks for the exact current SHA.
    """
    db = get_session_factory()()
    try:
        prs = db.query(m.PullRequest).filter(
            m.PullRequest.repository_id == repository_id, m.PullRequest.head_sha == head_sha,
            m.PullRequest.state == "open",
        ).all()
        if not prs:
            logger.info(
                "CI finalization ignored: trigger=webhook event_type=%s repository_id=%s "
                "pr_number=none head_sha=%s matched_current_revision=false reason=no_open_pr_at_sha",
                event_name,
                repository_id,
                head_sha,
                extra={
                    "finalization_trigger": "webhook",
                    "event_type": event_name,
                    "repository_id": repository_id,
                    "pr_number": None,
                    "head_sha": head_sha,
                    "matched_current_revision": False,
                    "ignore_reason": "no_open_pr_at_sha",
                },
            )
            return
        repo = db.query(m.Repository).filter(m.Repository.id == repository_id).first()
        if not repo:
            logger.warning(
                "CI finalization ignored: trigger=webhook event_type=%s repository_id=%s "
                "head_sha=%s matched_current_revision=false reason=repository_not_found",
                event_name,
                repository_id,
                head_sha,
            )
            return
        for pr in prs:
            logger.info(
                "CI event matched current PR revision: trigger=webhook event_type=%s "
                "repository=%s pr_number=%s head_sha=%s matched_current_revision=true",
                event_name,
                repo.full_name,
                pr.github_pr_number,
                head_sha,
                extra={
                    "finalization_trigger": "webhook",
                    "event_type": event_name,
                    "repository": repo.full_name,
                    "pr_number": pr.github_pr_number,
                    "head_sha": head_sha,
                    "matched_current_revision": True,
                },
            )
            review = run_analysis(
                db,
                repo,
                pr,
                installation_id,
                expected_head_sha=head_sha,
                trigger="webhook",
                event_name=event_name,
            )
            if review is None:
                continue
            if review.decision == m.DecisionStatus.WAITING_FOR_CI:
                _spawn_ci_wait_poller(
                    repository_id, pr.id, installation_id, review.head_sha
                )
            else:
                _cancel_ci_wait_poller(pr.id, head_sha)
    except Exception:
        db.rollback()
        logger.exception("Check-event finalization failed: repository_id=%s head_sha=%s", repository_id, head_sha)
    finally:
        db.close()


def _spawn_ci_wait_poller(repository_id: str, pull_request_id: str, installation_id: str, head_sha: str) -> None:
    key = (pull_request_id, head_sha)
    with _active_ci_waits_lock:
        if key in _active_ci_waits:
            return
        stop_event = threading.Event()
        _active_ci_waits[key] = stop_event
    db = get_session_factory()()
    try:
        final_review = db.query(m.Review).filter(
            m.Review.pull_request_id == pull_request_id,
            m.Review.head_sha == head_sha,
            m.Review.is_ci_wait_placeholder == False,  # noqa: E712
            m.Review.is_reevaluation == False,  # noqa: E712
            m.Review.decision != m.DecisionStatus.WAITING_FOR_CI,
        ).first()
        if final_review:
            stop_event.set()
    except Exception:
        logger.exception(
            "Could not check existing final review before starting fallback poller: "
            "pull_request_id=%s head_sha=%s",
            pull_request_id,
            head_sha,
        )
    finally:
        db.close()
    if stop_event.is_set():
        with _active_ci_waits_lock:
            if _active_ci_waits.get(key) is stop_event:
                _active_ci_waits.pop(key, None)
        logger.info(
            "CI-wait polling not started (revision already finalized): "
            "pull_request_id=%s head_sha=%s",
            pull_request_id,
            head_sha,
        )
        return
    thread = threading.Thread(
        target=_ci_wait_poll_loop,
        args=(repository_id, pull_request_id, installation_id, head_sha, stop_event),
        daemon=True, name=f"ci-wait-{pull_request_id[:8]}",
    )
    thread.start()
    logger.info("CI-wait polling started: pull_request_id=%s head_sha=%s interval=%ss timeout=%ss",
                pull_request_id, head_sha, settings.CI_WAIT_POLL_INTERVAL_SECONDS, settings.CI_WAIT_TIMEOUT_SECONDS)


def _ci_wait_poll_loop(
    repository_id: str,
    pull_request_id: str,
    installation_id: str,
    head_sha: str,
    stop_event: threading.Event | None = None,
) -> None:
    """Polls until CI stabilizes or the bounded timeout forces manual review."""
    session_factory = get_session_factory()
    stop_event = stop_event or threading.Event()
    deadline = time.monotonic() + settings.CI_WAIT_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            if stop_event.wait(settings.CI_WAIT_POLL_INTERVAL_SECONDS):
                logger.info(
                    "CI-wait polling stopped by finalization: trigger=polling "
                    "pull_request_id=%s head_sha=%s",
                    pull_request_id,
                    head_sha,
                )
                return
            db = session_factory()
            try:
                pr = db.query(m.PullRequest).filter(m.PullRequest.id == pull_request_id).first()
                if not pr or pr.head_sha != head_sha or pr.state != "open":
                    logger.info(
                        "CI-wait polling stopped (PR moved on): trigger=polling "
                        "pull_request_id=%s head_sha=%s",
                        pull_request_id,
                        head_sha,
                    )
                    return
                latest_final = db.query(m.Review).filter(
                    m.Review.pull_request_id == pr.id,
                    m.Review.head_sha == head_sha,
                    m.Review.is_ci_wait_placeholder == False,  # noqa: E712
                    m.Review.is_reevaluation == False,  # noqa: E712
                    m.Review.decision != m.DecisionStatus.WAITING_FOR_CI,
                ).first()
                if latest_final:
                    logger.info(
                        "CI-wait polling stopped (revision already finalized): "
                        "trigger=polling pull_request_id=%s head_sha=%s review_id=%s",
                        pull_request_id,
                        head_sha,
                        latest_final.id,
                    )
                    return
                repo = db.query(m.Repository).filter(m.Repository.id == repository_id).first()
                if not repo:
                    return
                review = run_analysis(
                    db,
                    repo,
                    pr,
                    installation_id,
                    expected_head_sha=head_sha,
                    trigger="polling",
                )
                if review is None or review.decision != m.DecisionStatus.WAITING_FOR_CI:
                    _cancel_ci_wait_poller(pull_request_id, head_sha)
                    return
            except Exception:
                db.rollback()
                logger.exception("CI-wait poll tick failed: pull_request_id=%s", pull_request_id)
            finally:
                db.close()

        # Timed out: force a final decision rather than waiting forever.
        logger.warning(
            "CI-wait timed out: trigger=polling_timeout elapsed_seconds=%s "
            "pull_request_id=%s head_sha=%s",
            settings.CI_WAIT_TIMEOUT_SECONDS,
            pull_request_id,
            head_sha,
        )
        db = session_factory()
        try:
            pr = db.query(m.PullRequest).filter(m.PullRequest.id == pull_request_id).first()
            repo = db.query(m.Repository).filter(m.Repository.id == repository_id).first() if pr else None
            if pr and repo and pr.head_sha == head_sha:
                latest = (db.query(m.Review).filter(m.Review.pull_request_id == pr.id)
                          .order_by(m.Review.created_at.desc()).first())
                if latest and latest.decision == m.DecisionStatus.WAITING_FOR_CI:
                    run_analysis(
                        db,
                        repo,
                        pr,
                        installation_id,
                        force_final=True,
                        expected_head_sha=head_sha,
                        trigger="polling_timeout",
                    )
        except Exception:
            db.rollback()
            logger.exception("CI-wait forced finalization failed: pull_request_id=%s", pull_request_id)
        finally:
            db.close()
    finally:
        with _active_ci_waits_lock:
            if _active_ci_waits.get((pull_request_id, head_sha)) is stop_event:
                _active_ci_waits.pop((pull_request_id, head_sha), None)


def run_analysis(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    is_reevaluation: bool = False,
    previous_review_id: str | None = None,
    force_final: bool = False,
    expected_head_sha: str | None = None,
    trigger: str = "analysis",
    event_name: str | None = None,
) -> m.Review | None:
    """Run one serialized lifecycle pass for the PR's current revision."""
    repository_id = repo.id
    pull_request_id = pr.id
    pr_number = pr.github_pr_number
    with _revision_finalization_lock(db, repository_id, pr_number):
        db.expire_all()
        current_pr = (
            db.query(m.PullRequest)
            .filter(m.PullRequest.id == pull_request_id)
            .first()
        )
        current_repo = (
            db.query(m.Repository)
            .filter(m.Repository.id == repository_id)
            .first()
        )
        if current_pr is None or current_repo is None:
            logger.warning(
                "Review lifecycle ignored: trigger=%s repository_id=%s pr_number=%s "
                "matched_current_revision=false reason=record_not_found",
                trigger,
                repository_id,
                pr_number,
            )
            return None

        if expected_head_sha and current_pr.head_sha != expected_head_sha:
            logger.info(
                "Review lifecycle ignored: trigger=%s repository=%s pr_number=%s "
                "event_type=%s event_head_sha=%s current_head_sha=%s matched_current_revision=false "
                "reason=stale_revision",
                trigger,
                current_repo.full_name,
                current_pr.github_pr_number,
                event_name or "none",
                expected_head_sha,
                current_pr.head_sha,
                extra={
                    "finalization_trigger": trigger,
                    "event_type": event_name,
                    "repository": current_repo.full_name,
                    "pr_number": current_pr.github_pr_number,
                    "head_sha": expected_head_sha,
                    "current_head_sha": current_pr.head_sha,
                    "matched_current_revision": False,
                    "ignore_reason": "stale_revision",
                },
            )
            return None

        if not is_reevaluation and previous_review_id is None:
            existing_final = (
                db.query(m.Review)
                .filter(
                    m.Review.pull_request_id == current_pr.id,
                    m.Review.head_sha == current_pr.head_sha,
                    m.Review.is_ci_wait_placeholder == False,  # noqa: E712
                    m.Review.is_reevaluation == False,  # noqa: E712
                    m.Review.decision != m.DecisionStatus.WAITING_FOR_CI,
                )
                .order_by(m.Review.created_at.desc())
                .first()
            )
            if existing_final:
                logger.info(
                    "Review lifecycle duplicate ignored: trigger=%s repository=%s "
                    "event_type=%s pr_number=%s head_sha=%s review_id=%s reason=already_finalized",
                    trigger,
                    current_repo.full_name,
                    event_name or "none",
                    current_pr.github_pr_number,
                    current_pr.head_sha,
                    existing_final.id,
                    extra={
                        "finalization_trigger": trigger,
                        "event_type": event_name,
                        "repository": current_repo.full_name,
                        "pr_number": current_pr.github_pr_number,
                        "head_sha": current_pr.head_sha,
                        "duplicate_ignored": True,
                    },
                )
                _cancel_ci_wait_poller(current_pr.id, current_pr.head_sha)
                return existing_final

        logger.info(
            "Review lifecycle acquired revision lock: trigger=%s repository=%s "
            "event_type=%s pr_number=%s head_sha=%s matched_current_revision=true",
            trigger,
            current_repo.full_name,
            event_name or "none",
            current_pr.github_pr_number,
            current_pr.head_sha,
            extra={
                "finalization_trigger": trigger,
                "event_type": event_name,
                "repository": current_repo.full_name,
                "pr_number": current_pr.github_pr_number,
                "head_sha": current_pr.head_sha,
                "matched_current_revision": True,
            },
        )
        review = _run_analysis_locked(
            db,
            current_repo,
            current_pr,
            installation_id,
            is_reevaluation=is_reevaluation,
            previous_review_id=previous_review_id,
            force_final=force_final,
            trigger=trigger,
            event_name=event_name,
        )
        if review is None or review.decision != m.DecisionStatus.WAITING_FOR_CI:
            _cancel_ci_wait_poller(current_pr.id, current_pr.head_sha)
        return review


def _run_analysis_locked(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    is_reevaluation: bool = False,
    previous_review_id: str | None = None,
    force_final: bool = False,
    trigger: str = "analysis",
    event_name: str | None = None,
) -> m.Review | None:
    """
    Runs one analysis pass. Returns either:
      - a WAITING_FOR_CI placeholder review (mandatory checks still running
        and force_final is False), or
      - a final review (APPROVED / BLOCKED / MANUAL_REVIEW).

    Idempotency: if a WAITING_FOR_CI placeholder already exists for this
    exact head commit, this call reuses it instead of creating a duplicate
    and does not re-post the "waiting" comment -- callers (the poller and the
    webhook fast path) can call this repeatedly without spamming the PR.
    """
    owner, repo_name = repo.full_name.split("/", 1)
    pull_request_fetch_error = None
    try:
        live_pr = github_client.get_pull_request(
            installation_id, owner, repo_name, pr.github_pr_number
        )
        live_sha = live_pr.get("head_sha")
        if live_pr.get("state") != "open":
            pr.state = live_pr.get("state") or pr.state
            db.commit()
            return None
        if not isinstance(live_sha, str) or not live_sha:
            raise ValueError("GitHub pull-request response is missing its head SHA.")
        if live_sha != pr.head_sha:
            logger.info(
                "PR head refreshed before CI evaluation: repository=%s pr_number=%s "
                "stored_head_sha=%s live_head_sha=%s",
                repo.full_name,
                pr.github_pr_number,
                pr.head_sha,
                live_sha,
            )
            pr.head_sha = live_sha
            db.commit()
    except Exception:
        pull_request_fetch_error = "pull_request"
        logger.exception(
            "Failed to verify live PR revision before CI evaluation: repository=%s pr_number=%s",
            repo.full_name,
            pr.github_pr_number,
        )

    # A retry of an already-finalized delivery must not issue another GitHub
    # review. Re-evaluation callers explicitly opt out of this check.
    existing_final = (
        db.query(m.Review)
        .filter(m.Review.pull_request_id == pr.id, m.Review.head_sha == pr.head_sha,
                m.Review.decision != m.DecisionStatus.WAITING_FOR_CI)
        .order_by(m.Review.created_at.desc())
        .first()
    )
    if existing_final and not is_reevaluation and previous_review_id is None:
        logger.info("Analysis already finalized for this revision; skipping duplicate: review_id=%s", existing_final.id)
        return existing_final

    analysis_started_at = time.perf_counter()
    started = datetime.utcnow()

    existing_placeholder = (
        db.query(m.Review)
        .filter(m.Review.pull_request_id == pr.id, m.Review.head_sha == pr.head_sha,
                m.Review.is_ci_wait_placeholder == True, m.Review.decision == m.DecisionStatus.WAITING_FOR_CI)  # noqa: E712
        .order_by(m.Review.created_at.desc())
        .first()
    )

    policy_doc = policy_engine.load_repo_policy(repo.full_name)
    policy_id = _get_or_create_policy_row(db, repo, policy_doc)

    # --- 1. Discover the checks attached to the live PR head. A transient
    # GitHub failure is represented explicitly and never treated as success.
    ci_fetch_started_at = time.perf_counter()
    ci_checks, ci_suites, ci_fetch_errors = _fetch_ci_checks(
        installation_id, owner, repo_name, pr.head_sha
    )
    if pull_request_fetch_error:
        ci_fetch_errors.append(pull_request_fetch_error)
    _log_analysis_timing("ci_fetch", ci_fetch_started_at, trigger, repo, pr)
    logger.info(
        "CI state re-fetched: trigger=%s event_type=%s repository=%s pr_number=%s "
        "head_sha=%s checks=%s check_suites=%s fetch_errors=%s",
        trigger,
        event_name or "none",
        repo.full_name,
        pr.github_pr_number,
        pr.head_sha,
        [
            {
                "identity": check.get("identity"),
                "name": check.get("name"),
                "provider": check.get("provider"),
                "status": check.get("status"),
                "conclusion": check.get("conclusion"),
            }
            for check in ci_checks
        ],
        [
            {"identity": suite.get("identity"), "status": suite.get("status"),
             "conclusion": suite.get("conclusion")}
            for suite in ci_suites
        ],
        ci_fetch_errors,
        extra={
            "finalization_trigger": trigger,
            "event_type": event_name,
            "repository": repo.full_name,
            "pr_number": pr.github_pr_number,
            "head_sha": pr.head_sha,
            "required_check_states": [
                {
                    "identity": check.get("identity"),
                    "name": check.get("name"),
                    "status": check.get("status"),
                    "conclusion": check.get("conclusion"),
                }
                for check in ci_checks
            ],
        },
    )
    ci_gate = policy_engine.evaluate_policy(policy_doc, [], ci_checks, pr.author_login)
    ci_result, ci_evaluation = _ci_snapshot(
        db,
        existing_placeholder,
        ci_checks,
        ci_suites,
        pr.head_sha,
        ci_fetch_errors,
        ci_gate.mandatory_checks_pending,
        force_final,
    )
    no_ci_detected = ci_evaluation.state in ("NO_CI_CONFIGURED", "NO_CHECKS_FOUND")
    ci_wait_pending = list(ci_evaluation.pending_checks)
    if ci_gate.mandatory_checks_pending:
        ci_wait_pending = sorted(set(ci_wait_pending + ci_gate.mandatory_checks_pending))
    should_wait_for_policy = bool(
        ci_gate.mandatory_checks_pending and not no_ci_detected and not force_final
    )
    should_wait_for_ci = not ci_evaluation.finalized and not force_final
    if should_wait_for_ci or should_wait_for_policy:
        logger.info(
            "CI discovery is not stable: trigger=%s event_type=%s repository=%s pr_number=%s "
            "head_sha=%s ci_state=%s pending_checks=%s",
            trigger,
            event_name or "none",
            repo.full_name,
            pr.github_pr_number,
            pr.head_sha,
            ci_evaluation.state,
            ci_wait_pending,
            extra={
                "finalization_trigger": trigger,
                "event_type": event_name,
                "repository": repo.full_name,
                "pr_number": pr.github_pr_number,
                "head_sha": pr.head_sha,
                "ci_state": ci_evaluation.state,
                "pending_checks": ci_wait_pending,
            },
        )
        waiting_changed_files = None
        waiting_changed_files_error = False
        if not existing_placeholder:
            waiting_files_started_at = time.perf_counter()
            try:
                waiting_changed_files = github_client.get_pr_files(
                    installation_id, owner, repo_name, pr.github_pr_number
                )
            except Exception:
                waiting_changed_files_error = True
                logger.exception(
                    "GitHub PR-file fetch failed while creating CI wait: "
                    "repository_id=%s pull_request_id=%s",
                    repo.id,
                    pr.id,
                )
            _log_analysis_timing("waiting_file_fetch", waiting_files_started_at, trigger, repo, pr)

        wait_reasons = []
        if ci_fetch_errors:
            wait_reasons.append(
                "SecurePR could not fully read GitHub CI yet; it will retry rather than make a final "
                "decision from incomplete CI information."
            )
        if waiting_changed_files_error:
            wait_reasons.append(
                "SecurePR could not retrieve the changed-file list; no changed-file count is available yet."
            )
        review = _persist_ci_wait(
            db, repo, pr, installation_id, owner, repo_name, policy_id, ci_gate, existing_placeholder,
            changed_files=waiting_changed_files,
            changed_files_error=waiting_changed_files_error,
            extra_reason="\n".join(wait_reasons) or None,
            pending_checks=ci_wait_pending,
            ci_result=ci_result,
        )
        _log_analysis_timing("total", analysis_started_at, trigger, repo, pr)
        return review

    # The discovered checks are terminal and stable, or the bounded timeout /
    # no-CI path will force a manual review before any approval can be published.
    analysis_complete = True
    changed_files_error: str | None = None
    changed_files: list[dict] = []
    files_fetch_started_at = time.perf_counter()
    try:
        changed_files = github_client.get_pr_files(installation_id, owner, repo_name, pr.github_pr_number)
    except Exception:
        logger.exception("GitHub PR-file fetch failed: repository_id=%s pull_request_id=%s", repo.id, pr.id)
        analysis_complete = False
        changed_files_error = (
            "GitHub could not provide this pull request's changed files. "
            "No risk assessment or auto-merge was performed."
        )
    _log_analysis_timing("changed_files_fetch", files_fetch_started_at, trigger, repo, pr)

    # --- 2. Policy evaluation (also classifies mandatory checks as
    #        succeeded/failed/pending -- see policy_engine.evaluate_policy) ---
    policy_eval = policy_engine.evaluate_policy(policy_doc, changed_files, ci_checks, pr.author_login)

    # --- 3. Risk scoring (deterministic + optional AI) --------------------------
    scoring_started_at = time.perf_counter()
    ai_scores = None
    if analysis_complete:
        heuristic_scores = risk_engine.compute_deterministic_scores(changed_files, ci_checks)
        heuristic_scores["policy_compliance"] = risk_engine.compute_policy_compliance_score(policy_eval.violations)
        gemini_started_at = time.perf_counter()
        try:
            ai_scores = gemini_client.score_dimensions(changed_files)
        except Exception:
            logger.exception("Gemini scoring raised unexpectedly; continuing with heuristic-only scores")
        _log_analysis_timing("gemini_scoring", gemini_started_at, trigger, repo, pr)
        blended = risk_engine.blend_with_ai(heuristic_scores, ai_scores)
        assessment = risk_engine.compute_overall(blended)
    else:
        rationale = "Not evaluated because GitHub did not provide the changed-file list."
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
            scores=blended,
            overall_score=0,
            category="NOT_EVALUATED",
        )
    _log_analysis_timing("risk_scoring", scoring_started_at, trigger, repo, pr)

    # --- 4. Findings --------------------------------------------------------
    findings: list[dict] = []
    for path in policy_eval.blocked_paths_touched:
        findings.append({"dimension": "policy_compliance", "severity": "CRITICAL",
                          "title": "Blocked path modified", "description": f"'{path}' matches a blocked-path rule.",
                          "file_path": path, "is_mandatory_blocker": True})
    for chk in policy_eval.mandatory_checks_failed:
        findings.append({"dimension": "verification", "severity": "HIGH",
                          "title": "Mandatory CI check failed", "description": f"'{chk}' completed without succeeding.",
                          "file_path": None, "is_mandatory_blocker": True})
    for dim, s in blended.items():
        if s.score >= 70:
            findings.append({"dimension": dim, "severity": "HIGH", "title": f"High {dim.replace('_', ' ')} risk",
                              "description": s.rationale, "file_path": None, "is_mandatory_blocker": False})

    # --- 5. Impact Simulator --------------------------------------------------
    impact_items = impact_simulator.simulate(changed_files) if analysis_complete else []

    # --- 6. Circuit breaker ----------------------------------------------------
    cb_tripped, cb_reason = circuit_breaker.is_tripped(db, repo.id, pr.author_login)

    # --- 7. Eligibility ----------------------------------------------------
    author = db.query(m.User).filter(m.User.github_login == pr.author_login).first()
    eligibility = None
    if author:
        eligibility = db.query(m.AutoApprovalEligibility).filter(
            m.AutoApprovalEligibility.repository_id == repo.id,
            m.AutoApprovalEligibility.user_id == author.id,
        ).first()
    author_eligible = bool(eligibility and eligibility.is_enabled)
    author_threshold = eligibility.max_risk_threshold if eligibility else None

    # --- 8. Decision (deterministic) --------------------------------------------
    ci_timed_out = ci_evaluation.state == "TIMEOUT"
    pending_for_decision = (
        []
        if force_final or ci_timed_out or no_ci_detected
        else policy_eval.mandatory_checks_pending
    )
    decision = decision_engine.decide(decision_engine.DecisionInput(
        overall_score=assessment.overall_score, risk_category=assessment.category, findings=findings,
        policy_violations=policy_eval.violations, policy_warnings=policy_eval.warnings,
        requires_human_review_by_policy=policy_eval.requires_human_review,
        circuit_breaker_tripped=cb_tripped, circuit_breaker_reason=cb_reason,
        author_eligible=author_eligible, author_risk_threshold=author_threshold,
        analysis_complete=analysis_complete, mandatory_checks_pending=pending_for_decision,
    ))
    if changed_files_error:
        decision.reasons = [changed_files_error]
    if ci_timed_out and decision.status not in (m.DecisionStatus.BLOCKED,):
        # Timed out waiting for CI -- never silently approve on an incomplete
        # verification picture; route to manual review and say why.
        decision = decision_engine.Decision(
            m.DecisionStatus.MANUAL_REVIEW,
            [
                (
                    f"Timed out after {settings.CI_WAIT_TIMEOUT_SECONDS}s waiting for GitHub CI "
                    "to become available and stable; routed to manual review rather than approving "
                    "without a final verification result."
                )
            ] + decision.reasons,
        )
    elif no_ci_detected and decision.status not in (m.DecisionStatus.BLOCKED,):
        state_text = (
            "No CI checks were detected for this commit."
            if ci_evaluation.state == "NO_CI_CONFIGURED"
            else "GitHub reported check suites but no check runs or commit statuses were found."
        )
        decision = decision_engine.Decision(
            m.DecisionStatus.MANUAL_REVIEW,
            [f"{state_text} SecurePR did not treat the absence of checks as successful CI."] +
            decision.reasons,
        )

    if decision.status != m.DecisionStatus.WAITING_FOR_CI:
        try:
            latest_pr = github_client.get_pull_request(
                installation_id, owner, repo_name, pr.github_pr_number
            )
        except Exception:
            logger.exception(
                "Could not verify the live PR revision before finalization: "
                "repository=%s pr_number=%s head_sha=%s",
                repo.full_name,
                pr.github_pr_number,
                pr.head_sha,
            )
            reason = "GitHub could not verify the PR revision before finalization."
            if force_final or ci_timed_out:
                review = _finalize_unverified_ci_wait(
                    db, repo, pr, installation_id, owner, repo_name, policy_id,
                    policy_eval, existing_placeholder, changed_files, ci_result, reason,
                )
            else:
                ci_result["state"] = "ERROR"
                ci_result["error"] = reason
                review = _persist_ci_wait(
                    db,
                    repo,
                    pr,
                    installation_id,
                    owner,
                    repo_name,
                    policy_id,
                    policy_eval,
                    existing_placeholder,
                    changed_files=changed_files,
                    pending_checks=["GitHub PR revision unavailable"],
                    ci_result=ci_result,
                    extra_reason=reason,
                )
            _log_analysis_timing("total", analysis_started_at, trigger, repo, pr)
            return review

        latest_sha = latest_pr.get("head_sha")
        if latest_pr.get("state") != "open":
            pr.state = latest_pr.get("state") or pr.state
            db.commit()
            return None
        if not isinstance(latest_sha, str) or not latest_sha:
            logger.error(
                "GitHub PR response did not contain a head SHA before finalization: "
                "repository=%s pr_number=%s",
                repo.full_name,
                pr.github_pr_number,
            )
            reason = "GitHub could not verify the PR revision before finalization."
            if force_final or ci_timed_out:
                review = _finalize_unverified_ci_wait(
                    db, repo, pr, installation_id, owner, repo_name, policy_id,
                    policy_eval, existing_placeholder, changed_files, ci_result, reason,
                )
            else:
                ci_result["state"] = "ERROR"
                ci_result["error"] = reason
                review = _persist_ci_wait(
                    db,
                    repo,
                    pr,
                    installation_id,
                    owner,
                    repo_name,
                    policy_id,
                    policy_eval,
                    existing_placeholder,
                    changed_files=changed_files,
                    pending_checks=["GitHub PR revision unavailable"],
                    ci_result=ci_result,
                    extra_reason=reason,
                )
            _log_analysis_timing("total", analysis_started_at, trigger, repo, pr)
            return review
        if latest_sha != pr.head_sha:
            logger.info(
                "Finalization stopped because the PR head changed during analysis: "
                "repository=%s pr_number=%s reviewed_head_sha=%s current_head_sha=%s",
                repo.full_name,
                pr.github_pr_number,
                pr.head_sha,
                latest_sha,
            )
            pr.head_sha = latest_sha
            db.commit()
            return None

        final_ci_checks, final_ci_suites, final_ci_errors = _fetch_ci_checks(
            installation_id, owner, repo_name, latest_sha
        )
        final_ci_gate = policy_engine.evaluate_policy(
            policy_doc, [], final_ci_checks, pr.author_login
        )
        final_ci_result, final_ci_evaluation = _ci_snapshot(
            db,
            existing_placeholder,
            final_ci_checks,
            final_ci_suites,
            latest_sha,
            final_ci_errors,
            final_ci_gate.mandatory_checks_pending,
            force_final,
        )
        final_signature_changed = (
            final_ci_result["signature"] != ci_result.get("signature")
        )
        if final_signature_changed and not (force_final or ci_timed_out):
            final_ci_result["state"] = "DISCOVERING"
            final_ci_result["last_change_at"] = datetime.utcnow().isoformat()
            final_ci_evaluation = ci_status.CIStatus(
                "DISCOVERING",
                False,
                ("Waiting for newly discovered CI results to stabilize",),
            )
        if not final_ci_evaluation.finalized and not (
            force_final or ci_timed_out
        ):
            final_pending = list(final_ci_evaluation.pending_checks)
            if not final_pending:
                final_pending = ["Waiting for CI checks to stabilize"]
            ci_result = final_ci_result
            review = _persist_ci_wait(
                db,
                repo,
                pr,
                installation_id,
                owner,
                repo_name,
                policy_id,
                final_ci_gate,
                existing_placeholder,
                changed_files=changed_files,
                pending_checks=final_pending,
                ci_result=final_ci_result,
                extra_reason=final_ci_evaluation.error,
            )
            _log_analysis_timing("total", analysis_started_at, trigger, repo, pr)
            return review

        ci_checks = final_ci_checks
        ci_suites = final_ci_suites
        ci_result = final_ci_result
        ci_evaluation = final_ci_evaluation
        if ci_evaluation.state == "TIMEOUT":
            ci_timed_out = True
            policy_eval = final_ci_gate
            if decision.status != m.DecisionStatus.BLOCKED:
                decision = decision_engine.Decision(
                    m.DecisionStatus.MANUAL_REVIEW,
                    [
                        (
                            f"Timed out after {settings.CI_WAIT_TIMEOUT_SECONDS}s waiting for GitHub CI "
                            "to become available and stable; routed to manual review rather than approving "
                            "without a final verification result."
                        )
                    ] + decision.reasons,
                )

    dashboard_url = f"{settings.DASHBOARD_ORIGIN}/#/pr/{pr.id}"

    # ===================================================================
    # Branch A: still waiting on mandatory CI -- persist/reuse a
    # lightweight placeholder, post the short "waiting" comment once.
    # ===================================================================
    if decision.status == m.DecisionStatus.WAITING_FOR_CI:
        if existing_placeholder:
            existing_placeholder.ci_wait_pending_checks = ci_wait_pending
            _store_ci_result(db, existing_placeholder.id, ci_result)
            db.commit()
            logger.info("CI-wait placeholder reused (still pending): review_id=%s pending=%s",
                        existing_placeholder.id, policy_eval.mandatory_checks_pending)
            _log_analysis_timing("total", analysis_started_at, trigger, repo, pr)
            return existing_placeholder

        review = m.Review(
            pull_request_id=pr.id, policy_id=policy_id, head_sha=pr.head_sha, is_reevaluation=is_reevaluation,
            superseded_review_id=previous_review_id, analysis_started_at=started,
            is_ci_wait_placeholder=True, ci_wait_started_at=started,
            ci_wait_pending_checks=ci_wait_pending,
            decision=m.DecisionStatus.WAITING_FOR_CI, decision_reasons=decision.reasons,
            risk_category="NOT_EVALUATED", circuit_breaker_active=cb_tripped,
        )
        db.add(review)
        db.flush()
        for f in changed_files:
            db.add(m.ChangedFile(review_id=review.id, file_path=f["file_path"], change_type=f.get("change_type"),
                                  additions=f.get("additions", 0), deletions=f.get("deletions", 0),
                                  is_blocked_path=f["file_path"] in policy_eval.blocked_paths_touched,
                                  patch_excerpt=f.get("patch_excerpt")))
        db.add(m.Evidence(review_id=review.id, evidence_type="ci_wait", source="securepr",
                           content=f"Waiting for: {', '.join(policy_eval.mandatory_checks_pending)}"))
        _store_ci_result(db, review.id, ci_result)
        db.commit()
        db.refresh(review)
        logger.info(
            "CI-wait placeholder created: trigger=%s event_type=%s repository=%s pr_number=%s "
            "head_sha=%s review_id=%s pending_checks=%s",
            trigger,
            event_name or "none",
            repo.full_name,
            pr.github_pr_number,
            pr.head_sha,
            review.id,
            policy_eval.mandatory_checks_pending,
            extra={
                "finalization_trigger": trigger,
                "event_type": event_name,
                "repository": repo.full_name,
                "pr_number": pr.github_pr_number,
                "head_sha": pr.head_sha,
                "pending_checks": policy_eval.mandatory_checks_pending,
            },
        )

        try:
            body = format_waiting_comment(
                    pending_checks=ci_wait_pending, changed_files_count=len(changed_files),
                policy_name=policy_eval.policy_name, policy_version=policy_eval.policy_version,
                dashboard_url=dashboard_url,
                    ci_state=ci_result.get("state"),
            )
            result = github_client.post_review(installation_id, owner, repo_name, pr.github_pr_number, body, "COMMENT")
            db.add(m.GithubAction(review_id=review.id, action_type="post_waiting_comment",
                                   response_status=result.get("status_code"), succeeded=result.get("succeeded", False)))
            db.commit()
        except Exception:
            logger.exception("Failed to post CI-wait comment: review_id=%s", review.id)
        _log_analysis_timing("total", analysis_started_at, trigger, repo, pr)
        return review

    # ===================================================================
    # Branch B: final decision -- persist the full review, post the full
    # comment, notify the owner.
    # ===================================================================
    final_previous_review_id = previous_review_id or (existing_placeholder.id if existing_placeholder else None)
    waited_for_ci = bool(existing_placeholder) or force_final

    review = m.Review(
        pull_request_id=pr.id, policy_id=policy_id, head_sha=pr.head_sha, is_reevaluation=is_reevaluation,
        superseded_review_id=final_previous_review_id, analysis_started_at=started,
    )
    db.add(review)
    db.flush()

    for f in findings:
        db.add(m.Finding(review_id=review.id, **f))
    for item in impact_items:
        db.add(m.ImpactPrediction(
            review_id=review.id, component_type=item.component_type, component_name=item.component_name,
            confidence=item.confidence, evidence_summary=item.evidence_summary,
            potential_consequence=item.potential_consequence,
        ))
    for f in changed_files:
        is_blocked = f["file_path"] in policy_eval.blocked_paths_touched
        db.add(m.ChangedFile(review_id=review.id, file_path=f["file_path"], change_type=f.get("change_type"),
                              additions=f.get("additions", 0), deletions=f.get("deletions", 0),
                              is_blocked_path=is_blocked, patch_excerpt=f.get("patch_excerpt")))
    for dim, s in blended.items():
        db.add(m.Evidence(review_id=review.id, evidence_type="dimension_rationale", source=s.source,
                           content=f"{dim}: {s.rationale}"))
    if ai_scores and ai_scores.get("summary"):
        db.add(m.Evidence(review_id=review.id, evidence_type="ai_rationale", source="gemini",
                           content=ai_scores["summary"]))
    _store_ci_result(db, review.id, ci_result)
    if not analysis_complete:
        db.add(m.Evidence(review_id=review.id, evidence_type="error", source="github_fetch",
                           content=changed_files_error or
                           "Failed to fetch PR files from GitHub; analysis treated as incomplete."))

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
    review.circuit_breaker_active = cb_tripped
    review.analysis_completed_at = datetime.utcnow()
    db.commit()
    db.refresh(review)
    logger.info(
        "Final review saved: trigger=%s event_type=%s repository=%s pr_number=%s head_sha=%s "
        "review_id=%s decision=%s waited_for_ci=%s",
        trigger,
        event_name or "none",
        repo.full_name,
        pr.github_pr_number,
        pr.head_sha,
        review.id,
        decision.status.value,
        waited_for_ci,
        extra={
            "finalization_trigger": trigger,
            "event_type": event_name,
            "repository": repo.full_name,
            "pr_number": pr.github_pr_number,
            "head_sha": pr.head_sha,
            "review_id": review.id,
            "decision": decision.status.value,
        },
    )

    # --- Publish GitHub comment / review ---------------------------------
    body = format_comment(
        decision=decision.status.value, reasons=decision.reasons, overall_score=assessment.overall_score,
        category=assessment.category, dimension_scores=blended, findings=findings, changed_files=changed_files,
        circuit_breaker_tripped=cb_tripped, circuit_breaker_reason=cb_reason, impact_items=impact_items,
        policy_name=policy_eval.policy_name, policy_version=policy_eval.policy_version, dashboard_url=dashboard_url,
        ci_checks=ci_checks, waited_for_ci=waited_for_ci,
        ci_state=ci_evaluation.state,
        analysis_error=changed_files_error,
    )

    event_map = {"APPROVED": "APPROVE", "BLOCKED": "REQUEST_CHANGES", "MANUAL_REVIEW": "COMMENT"}
    gh_event = event_map.get(decision.status.value, "COMMENT")

    github_publish_started_at = time.perf_counter()
    try:
        result = github_client.post_review(installation_id, owner, repo_name, pr.github_pr_number, body, gh_event)
        db.add(m.GithubAction(review_id=review.id, action_type="submit_review",
                               request_payload={"event": gh_event}, response_status=result["status_code"],
                               response_payload=result.get("payload") if isinstance(result.get("payload"), dict) else None,
                               succeeded=result["succeeded"]))
        review.github_action_confirmed = result["succeeded"]
        review.github_review_state = gh_event
        if decision.status == m.DecisionStatus.APPROVED and not result["succeeded"]:
            review.decision_reasons = review.decision_reasons + [
                "GitHub did not confirm the approval action; treat as unresolved and check manually."]
        db.commit()
        if result["succeeded"]:
            logger.info(
                "Final GitHub decision published: trigger=%s event_type=%s repository=%s pr_number=%s "
                "head_sha=%s review_id=%s status=%s",
                trigger,
                event_name or "none",
                repo.full_name,
                pr.github_pr_number,
                pr.head_sha,
                review.id,
                result.get("status_code"),
                extra={
                    "finalization_trigger": trigger,
                    "event_type": event_name,
                    "repository": repo.full_name,
                    "pr_number": pr.github_pr_number,
                    "head_sha": pr.head_sha,
                    "review_id": review.id,
                    "published": result["succeeded"],
                },
            )
        else:
            logger.error("GitHub review/comment failed: review_id=%s status=%s", review.id, result.get("status_code"))
    except Exception:
        logger.exception("GitHub review/comment failed with exception: review_id=%s", review.id)
        db.add(m.GithubAction(review_id=review.id, action_type="submit_review", succeeded=False))
        db.commit()
    _log_analysis_timing("github_publish", github_publish_started_at, trigger, repo, pr)

    # --- Auto-merge: separate, fail-closed gate after the final review ---
    # This never runs for WAITING_FOR_CI and re-fetches mutable GitHub state.
    auto_merge_started_at = time.perf_counter()
    if decision.status == m.DecisionStatus.APPROVED:
        merge_result = auto_merge.attempt(
            db=db, repo=repo, pr=pr, review=review, installation_id=installation_id,
            policy_doc=policy_doc, findings=findings, author_eligible=author_eligible,
            author_threshold=author_threshold,
        )
        review.auto_merge_status = merge_result.status
        if merge_result.status in ("MERGED", "FAILED"):
            review.auto_merge_attempted_at = datetime.utcnow()
        if merge_result.status == "MERGED":
            review.auto_merged_at = datetime.utcnow()
            review.merge_commit_sha = merge_result.merge_commit_sha
        elif merge_result.status not in ("DISABLED_GLOBAL", "DISABLED_REPOSITORY"):
            review.auto_merge_failure_reason = merge_result.reason
        db.add(m.Evidence(review_id=review.id, evidence_type="auto_merge", source="securepr",
                          content=json.dumps({"status": merge_result.status, "reason": merge_result.reason,
                                              "merge_commit_sha": merge_result.merge_commit_sha})))
        db.add(m.GithubAction(review_id=review.id, action_type="auto_merge",
                               request_payload={"expected_head_sha": review.head_sha},
                               response_status=merge_result.response_status,
                               response_payload=merge_result.response_payload,
                               succeeded=merge_result.status == "MERGED"))
        db.commit()

        try:
            merge_comment = format_auto_merge_comment(status=merge_result.status,
                                                       overall_score=assessment.overall_score,
                                                       reason=merge_result.reason,
                                                       merge_commit_sha=merge_result.merge_commit_sha)
            comment_result = github_client.post_issue_comment(
                installation_id, owner, repo_name, pr.github_pr_number, merge_comment)
            db.add(m.GithubAction(review_id=review.id, action_type="post_auto_merge_result",
                                   response_status=comment_result["status_code"],
                                   response_payload=comment_result.get("payload"),
                                   succeeded=comment_result["succeeded"]))
            db.commit()
        except Exception:
            logger.exception("Failed to post auto-merge outcome comment: review_id=%s", review.id)
    _log_analysis_timing("auto_merge", auto_merge_started_at, trigger, repo, pr)

    # --- Notify owner --------------------------------------------------
    notification_started_at = time.perf_counter()
    try:
        github_client.notify_owner(
            installation_id, owner, repo_name, pr.github_pr_number,
            f"SecurePR review complete for PR #{pr.github_pr_number}: **{decision.status.value}** "
            f"(risk {assessment.overall_score}/100, {assessment.category}).",
        )
        db.add(m.Notification(review_id=review.id, recipient=owner, channel="github",
                               subject=f"PR #{pr.github_pr_number} {decision.status.value}", body=body[:2000],
                               delivered=True))
        db.commit()
    except Exception:
        logger.exception(
            "Failed to send owner notification: trigger=%s repository=%s pr_number=%s "
            "head_sha=%s review_id=%s",
            trigger,
            repo.full_name,
            pr.github_pr_number,
            pr.head_sha,
            review.id,
        )

    _log_analysis_timing("notification", notification_started_at, trigger, repo, pr)
    _log_analysis_timing("total", analysis_started_at, trigger, repo, pr)
    return review


def _get_or_create_policy_row(db: Session, repo: m.Repository, policy_doc: dict) -> str:
    """Mirrors the on-disk/DB policy document into the `policies` table so
    each Review can link to the exact policy snapshot (name + version) it
    was evaluated against -- required for faithful decision replay even
    after the policy file changes later."""
    name = policy_doc.get("name", "default")
    version = policy_doc.get("version", 1)
    row = db.query(m.Policy).filter(
        m.Policy.repository_id == repo.id, m.Policy.name == name, m.Policy.version == version,
    ).first()
    if row:
        return row.id
    row = m.Policy(repository_id=repo.id, name=name, version=version, content_json=policy_doc, is_active=True)
    db.add(row)
    db.flush()
    return row.id
