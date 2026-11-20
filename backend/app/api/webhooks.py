import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.models import models as m
from app.services.background_jobs import enqueue_job

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

logger = logging.getLogger("securepr.webhooks")
settings = get_settings()


# ============================================================
# SECURITY
# ============================================================


def _verify_signature(raw_body: bytes, signature_header: str | None) -> None:
    """Verify that the webhook request was sent by GitHub."""

    if not settings.GITHUB_WEBHOOK_SECRET:
        logger.error(
            "Webhook signature verification unavailable: GITHUB_WEBHOOK_SECRET is not configured."
        )
        raise HTTPException(
            status_code=503,
            detail="Webhook signature verification is unavailable",
        )

    if not signature_header:
        logger.error(
            "Webhook signature verification failed: "
            "X-Hub-Signature-256 header is missing."
        )
        raise HTTPException(
            status_code=401,
            detail="Missing X-Hub-Signature-256 header",
        )

    expected_signature = (
        "sha256="
        + hmac.new(
            settings.GITHUB_WEBHOOK_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
    )

    if not hmac.compare_digest(expected_signature, signature_header):
        logger.error("Webhook signature verification failed: invalid signature.")
        raise HTTPException(
            status_code=401,
            detail="Invalid webhook signature",
        )

    logger.info("Webhook signature verification succeeded.")


# ============================================================
# DIAGNOSTIC HELPERS
# ============================================================


def _safe_get(data: Any, *keys: str) -> Any:
    """Safely retrieve nested dictionary values."""

    current = data

    for key in keys:
        if not isinstance(current, dict):
            return None

        current = current.get(key)

    return current


def _log_payload_diagnostics(
    payload: dict,
    event_name: str | None,
) -> None:
    """
    Log the webhook payload structure without logging sensitive data.
    This helps identify exactly why an installation ID is missing.
    """

    logger.info("========== WEBHOOK DIAGNOSTICS START ==========")

    logger.info("GitHub event header: %s", event_name)
    logger.info(
        "Top-level payload keys: %s",
        sorted(payload.keys()),
    )

    # --------------------------------------------------------
    # Repository diagnostics
    # --------------------------------------------------------

    repository = payload.get("repository")

    logger.info(
        "Repository object present: %s",
        isinstance(repository, dict),
    )

    if isinstance(repository, dict):
        logger.info(
            "Repository details: id=%s full_name=%s owner=%s",
            repository.get("id"),
            repository.get("full_name"),
            _safe_get(repository, "owner", "login"),
        )
        logger.info(
            "Repository object keys: %s",
            sorted(repository.keys()),
        )

    # --------------------------------------------------------
    # Installation diagnostics
    # --------------------------------------------------------

    installation = payload.get("installation")

    logger.info(
        "Installation object present at payload['installation']: %s",
        isinstance(installation, dict),
    )

    if isinstance(installation, dict):
        logger.info(
            "Installation object keys: %s",
            sorted(installation.keys()),
        )
        logger.info(
            "Installation ID from payload['installation']['id']: %s",
            installation.get("id"),
        )
    else:
        logger.warning("No installation object found at payload['installation'].")

    # Check other possible locations for diagnostics.
    alternate_locations = {
        "payload.installation_id": payload.get("installation_id"),
        "repository.installation_id": _safe_get(
            payload,
            "repository",
            "installation_id",
        ),
        "sender.installation_id": _safe_get(
            payload,
            "sender",
            "installation_id",
        ),
        "organization.installation_id": _safe_get(
            payload,
            "organization",
            "installation_id",
        ),
    }

    for location, value in alternate_locations.items():
        logger.info(
            "Diagnostic lookup [%s] = %s",
            location,
            value,
        )

    # --------------------------------------------------------
    # Pull request diagnostics
    # --------------------------------------------------------

    pull_request = payload.get("pull_request")

    logger.info(
        "Pull request object present: %s",
        isinstance(pull_request, dict),
    )

    if isinstance(pull_request, dict):
        logger.info(
            "Pull request details: number=%s title=%s",
            payload.get("number"),
            pull_request.get("title"),
        )
        logger.info(
            "Pull request object keys: %s",
            sorted(pull_request.keys()),
        )

    # --------------------------------------------------------
    # Sender diagnostics
    # --------------------------------------------------------

    sender = payload.get("sender")

    if isinstance(sender, dict):
        logger.info(
            "Webhook sender: login=%s type=%s",
            sender.get("login"),
            sender.get("type"),
        )

    logger.info("========== WEBHOOK DIAGNOSTICS END ==========")


def _extract_installation_id(payload: dict) -> str | None:
    """
    Extract the GitHub App installation ID.

    Multiple locations are checked for diagnostics and compatibility.
    """

    logger.info("Starting installation ID extraction.")

    installation = payload.get("installation")

    # Primary location used by GitHub App webhooks.
    if isinstance(installation, dict):
        installation_id = installation.get("id")

        if installation_id:
            logger.info(
                "SUCCESS: Installation ID found in payload['installation']['id']: %s",
                installation_id,
            )
            return str(installation_id)

        logger.warning("Installation object exists, but installation['id'] is empty.")

    # Diagnostic fallback locations.
    fallback_locations = [
        ("payload['installation_id']", payload.get("installation_id")),
        (
            "payload['repository']['installation_id']",
            _safe_get(payload, "repository", "installation_id"),
        ),
    ]

    for location, value in fallback_locations:
        if value:
            logger.warning(
                "Installation ID found in fallback location %s: %s",
                location,
                value,
            )
            return str(value)

    logger.error(
        "FAILED: Installation ID could not be found anywhere in the webhook payload."
    )

    return None


# ============================================================
# DATABASE HELPERS
# ============================================================


def _get_or_create_repo(
    db: Session,
    payload: dict,
    installation_id: str | None,
) -> m.Repository:

    gh_repo = payload.get("repository")

    if not isinstance(gh_repo, dict):
        logger.error("Repository creation failed: 'repository' object is missing.")
        raise HTTPException(
            status_code=400,
            detail="Webhook payload does not contain a valid repository object.",
        )

    github_repo_id = str(gh_repo.get("id", ""))

    if not github_repo_id:
        logger.error("Repository creation failed: repository ID is missing.")
        raise HTTPException(
            status_code=400,
            detail="Repository ID is missing from webhook payload.",
        )

    repo = (
        db.query(m.Repository)
        .filter(m.Repository.github_repo_id == github_repo_id)
        .first()
    )

    if not repo:
        logger.info(
            "Creating new repository record: github_repo_id=%s",
            github_repo_id,
        )

        repo = m.Repository(
            github_repo_id=github_repo_id,
            owner_login=gh_repo.get("owner", {}).get("login", ""),
            name=gh_repo.get("name", ""),
            full_name=gh_repo.get("full_name", ""),
            default_branch=gh_repo.get("default_branch", "main"),
            installation_id=installation_id or "",
        )

        db.add(repo)
        db.flush()

    else:
        logger.info(
            "Existing repository record found: id=%s installation_id=%s",
            repo.id,
            repo.installation_id,
        )

        if installation_id:
            repo.installation_id = installation_id
            logger.info(
                "Repository installation ID updated: %s",
                installation_id,
            )

    return repo


def _get_or_create_pr(
    db: Session,
    repo: m.Repository,
    payload: dict,
) -> m.PullRequest:

    gh_pr = payload.get("pull_request")

    if not isinstance(gh_pr, dict):
        logger.error("PR creation failed: 'pull_request' object is missing.")
        raise HTTPException(
            status_code=400,
            detail="Webhook payload does not contain a valid pull request.",
        )

    pr_number = payload.get("number")

    if not pr_number:
        logger.error("PR creation failed: PR number is missing.")
        raise HTTPException(
            status_code=400,
            detail="Pull request number is missing.",
        )

    pr = (
        db.query(m.PullRequest)
        .filter(
            m.PullRequest.repository_id == repo.id,
            m.PullRequest.github_pr_number == pr_number,
        )
        .first()
    )

    if not pr:
        logger.info(
            "Creating new PR record: repository=%s PR=%s",
            repo.full_name,
            pr_number,
        )

        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=pr_number,
            title=gh_pr.get("title", ""),
            author_login=gh_pr.get("user", {}).get("login", ""),
            head_sha=gh_pr.get("head", {}).get("sha", ""),
            ai_state="AI_PENDING",
            base_branch=gh_pr.get("base", {}).get("ref", ""),
            head_branch=gh_pr.get("head", {}).get("ref", ""),
            state=gh_pr.get("state", ""),
            merged_at=_parse_github_datetime(gh_pr.get("merged_at"))
            if gh_pr.get("merged") is True
            else None,
            merge_commit_sha=gh_pr.get("merge_commit_sha")
            if gh_pr.get("merged") is True
            else None,
        )

        db.add(pr)

    else:
        logger.info(
            "Updating existing PR record: id=%s PR=%s",
            pr.id,
            pr_number,
        )

        new_head_sha = gh_pr.get("head", {}).get(
            "sha",
            pr.head_sha,
        )
        if new_head_sha != pr.head_sha:
            pr.ai_state = "AI_PENDING"
            pr.ai_error_code = None
            pr.ai_retryable_at = None
            pr.ai_attempt_id = None
        pr.head_sha = new_head_sha
        pr.title = gh_pr.get("title", pr.title)
        pr.author_login = gh_pr.get("user", {}).get("login", pr.author_login)
        pr.base_branch = gh_pr.get("base", {}).get("ref", pr.base_branch)
        pr.head_branch = gh_pr.get("head", {}).get("ref", pr.head_branch)
        pr.state = gh_pr.get("state", pr.state)
        if gh_pr.get("merged") is True:
            pr.merged_at = _parse_github_datetime(gh_pr.get("merged_at"))
            pr.merge_commit_sha = gh_pr.get("merge_commit_sha")

    db.flush()

    return pr


def _parse_github_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        logger.warning("GitHub webhook contained an invalid merged_at timestamp.")
        return None


# ============================================================
# CHECK SUITE / CHECK RUN EVENT HANDLING (CI-wait fast path)
# ============================================================


def _ci_event_head_sha(payload: dict, event_name: str) -> str | None:
    if event_name == "status":
        value = payload.get("sha")
    elif event_name == "check_run":
        value = _safe_get(payload, "check_run", "head_sha")
    elif event_name == "check_suite":
        value = _safe_get(payload, "check_suite", "head_sha")
    else:
        value = None
    return str(value) if value else None


def _log_ci_event(
    *,
    event_name: str,
    repository: str | None,
    pr_number: int | None,
    head_sha: str | None,
    matched_current_revision: bool,
    reason: str | None = None,
) -> None:
    logger.info(
        "CI webhook event_type=%s repository=%s pr_number=%s head_sha=%s "
        "matched_current_revision=%s reason=%s",
        event_name,
        repository or "unknown",
        pr_number if pr_number is not None else "none",
        head_sha or "none",
        matched_current_revision,
        reason or "accepted",
        extra={
            "event_type": event_name,
            "repository": repository,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "matched_current_revision": matched_current_revision,
            "ignore_reason": reason,
        },
    )


def _handle_check_event(
    db: Session,
    payload: dict,
    event_name: str,
    delivery_id: str | None,
) -> dict:
    """Queue an idempotent CI refresh without starting or blocking AI analysis."""

    gh_repo = payload.get("repository")
    if not isinstance(gh_repo, dict):
        _log_ci_event(
            event_name=event_name,
            repository=None,
            pr_number=None,
            head_sha=_ci_event_head_sha(payload, event_name),
            matched_current_revision=False,
            reason="repository_object_missing",
        )
        return {"status": "ignored", "reason": "No repository object on check event."}

    repository_name = gh_repo.get("full_name")
    head_sha = _ci_event_head_sha(payload, event_name)
    check_payload_key = {
        "check_run": "check_run",
        "check_suite": "check_suite",
    }.get(event_name)
    event_pull_requests = (
        _safe_get(payload, check_payload_key, "pull_requests")
        if check_payload_key
        else None
    )
    pr_numbers = (
        sorted(
            {
                item["number"]
                for item in event_pull_requests or []
                if isinstance(item, dict)
                and isinstance(item.get("number"), int)
                and not isinstance(item.get("number"), bool)
            }
        )
        if isinstance(event_pull_requests, list)
        else []
    )
    pr_number = pr_numbers[0] if len(pr_numbers) == 1 else None

    action = payload.get("action")
    if event_name != "status" and (not isinstance(action, str) or not action):
        reason = "action_missing"
        _log_ci_event(
            event_name=event_name,
            repository=repository_name,
            pr_number=pr_number,
            head_sha=head_sha,
            matched_current_revision=False,
            reason=reason,
        )
        return {"status": "ignored", "reason": f"{event_name} action is missing."}

    github_repo_id = str(gh_repo.get("id", ""))
    if not head_sha:
        _log_ci_event(
            event_name=event_name,
            repository=repository_name,
            pr_number=pr_number,
            head_sha=None,
            matched_current_revision=False,
            reason="head_sha_missing",
        )
        return {"status": "ignored", "reason": "No head_sha on check event."}

    try:
        repo = (
            db.query(m.Repository)
            .filter(m.Repository.github_repo_id == github_repo_id)
            .first()
        )
        if not repo:
            _log_ci_event(
                event_name=event_name,
                repository=repository_name,
                pr_number=pr_number,
                head_sha=head_sha,
                matched_current_revision=False,
                reason="repository_not_tracked",
            )
            return {
                "status": "ignored",
                "reason": "Repository not tracked by SecurePR yet.",
            }

        pr_query = db.query(m.PullRequest).filter(
            m.PullRequest.repository_id == repo.id,
            m.PullRequest.head_sha == head_sha,
            m.PullRequest.state == "open",
        )
        if pr_numbers:
            pr_query = pr_query.filter(m.PullRequest.github_pr_number.in_(pr_numbers))
        matching_prs = pr_query.all()
        if not matching_prs:
            _log_ci_event(
                event_name=event_name,
                repository=repo.full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                matched_current_revision=False,
                reason="no_open_pr_at_current_head_sha",
            )
            return {
                "status": "ignored",
                "reason": "No open SecurePR-tracked PR has this current head SHA.",
            }

        for pr in matching_prs:
            _log_ci_event(
                event_name=event_name,
                repository=repo.full_name,
                pr_number=pr.github_pr_number,
                head_sha=head_sha,
                matched_current_revision=True,
            )

        installation_id = _extract_installation_id(payload) or repo.installation_id
        if not installation_id:
            _log_ci_event(
                event_name=event_name,
                repository=repo.full_name,
                pr_number=pr_number,
                head_sha=head_sha,
                matched_current_revision=True,
                reason="installation_id_missing",
            )
            return {
                "status": "ignored",
                "reason": "No installation_id available for this repository.",
            }

        for pr in matching_prs:
            revision = (
                db.query(m.CIRevision)
                .filter(
                    m.CIRevision.repository_id == repo.id,
                    m.CIRevision.pull_request_id == pr.id,
                    m.CIRevision.head_sha == head_sha,
                )
                .first()
            )
            if revision is None:
                revision = m.CIRevision(
                    repository_id=repo.id,
                    pull_request_id=pr.id,
                    head_sha=head_sha,
                    state="UNKNOWN",
                )
                db.add(revision)
            revision.retry_count = 0
            key_source = (
                delivery_id
                or hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode("utf-8")
                ).hexdigest()
            )
            enqueue_job(
                db,
                "CI_REFRESH",
                f"ci-event:{repo.id}:{pr.id}:{head_sha}:{key_source}",
                {
                    "repository_id": repo.id,
                    "pull_request_id": pr.id,
                    "installation_id": installation_id,
                    "head_sha": head_sha,
                    "event_name": event_name,
                    "reason": "webhook",
                },
            )
        db.commit()
    except SQLAlchemyError as exc:
        try:
            db.rollback()
        except SQLAlchemyError:
            logger.exception(
                "Check webhook transaction rollback failed: event_type=%s repository=%s head_sha=%s",
                event_name,
                repository_name,
                head_sha,
            )
        logger.exception(
            "Check webhook database processing failed: event_type=%s repository=%s head_sha=%s delivery_id=%s",
            event_name,
            repository_name,
            head_sha,
            delivery_id or "unknown",
            extra={
                "event_type": event_name,
                "repository": repository_name,
                "head_sha": head_sha,
                "delivery_id": delivery_id,
                "database_error_type": type(exc).__name__,
                "processing_failed": True,
            },
        )
        raise HTTPException(
            status_code=503,
            detail="Check event could not be processed because a database operation failed.",
        ) from exc

    logger.info(
        "CI refresh queued independently: event_type=%s repository=%s head_sha=%s "
        "pr_numbers=%s delivery_id=%s",
        event_name,
        repo.full_name,
        head_sha,
        [pr.github_pr_number for pr in matching_prs],
        delivery_id or "payload-hash",
    )
    return {"status": "accepted", "reason": "CI state refresh queued independently."}


# ============================================================
# GITHUB WEBHOOK ENDPOINT
# ============================================================


@router.post("/github")
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """
    Receive and process GitHub webhook events.
    """

    logger.info("================================================")
    logger.info("New GitHub webhook request received.")

    try:
        raw_body = await request.body()

        logger.info(
            "Webhook request body size: %s bytes",
            len(raw_body),
        )

        _verify_signature(
            raw_body,
            x_hub_signature_256,
        )

        try:
            payload = await request.json()
        except Exception:
            logger.exception("Failed to parse webhook request body as JSON.")
            raise HTTPException(
                status_code=400,
                detail="Invalid JSON webhook payload.",
            )

        if not isinstance(payload, dict):
            logger.error(
                "Webhook payload is not a JSON object. Type=%s",
                type(payload).__name__,
            )
            raise HTTPException(
                status_code=400,
                detail="Webhook payload must be a JSON object.",
            )

        action = payload.get("action")

        logger.info(
            "Webhook event received: event=%s action=%s",
            x_github_event,
            action,
        )

        # Always run diagnostics before processing.
        _log_payload_diagnostics(
            payload,
            x_github_event,
        )

        # ----------------------------------------------------
        # CI events also cover non-terminal transitions so discovery stability
        # can be reset when workflows or checks appear late.
        # ----------------------------------------------------

        if x_github_event in ("check_suite", "check_run", "status"):
            return _handle_check_event(db, payload, x_github_event, x_github_delivery)

        # ----------------------------------------------------
        # Ignore events that SecurePR does not process
        # ----------------------------------------------------

        if x_github_event != "pull_request":
            logger.info(
                "Webhook ignored because event is not pull_request: %s",
                x_github_event,
            )

            return {
                "status": "ignored",
                "reason": f"Event '{x_github_event}' is not handled.",
            }

        allowed_actions = (
            "opened",
            "synchronize",
            "reopened",
            "ready_for_review",
            "closed",
        )

        if action not in allowed_actions:
            logger.info(
                "Webhook ignored because action is not supported: %s",
                action,
            )

            return {
                "status": "ignored",
                "reason": f"Action '{action}' is not analyzed.",
            }

        logger.info("Webhook accepted for SecurePR analysis.")

        # ----------------------------------------------------
        # Extract installation ID
        # ----------------------------------------------------

        installation_id = _extract_installation_id(payload)

        if not installation_id:
            logger.error(
                "STOPPING ANALYSIS: No GitHub App installation ID "
                "was found. GitHub API calls cannot continue."
            )

            raise HTTPException(
                status_code=400,
                detail=(
                    "GitHub App installation ID is missing. "
                    "Check the webhook diagnostics in the backend logs."
                ),
            )

        # ----------------------------------------------------
        # Save repository and PR
        # ----------------------------------------------------

        try:
            repo = _get_or_create_repo(
                db,
                payload,
                installation_id,
            )

            db.flush()

            logger.info(
                "Repository saved successfully: "
                "repository_id=%s full_name=%s installation_id=%s",
                repo.id,
                repo.full_name,
                repo.installation_id,
            )

            pr = _get_or_create_pr(
                db,
                repo,
                payload,
            )

            db.flush()

            logger.info(
                "PR saved successfully: pull_request_id=%s number=%s",
                pr.id,
                pr.github_pr_number,
            )

            db.commit()

            logger.info("Database transaction committed successfully.")

        except HTTPException:
            db.rollback()
            raise

        except Exception:
            db.rollback()

            logger.exception("Database operation failed while processing webhook.")

            raise HTTPException(
                status_code=500,
                detail=(
                    "Failed to save repository or pull request. "
                    "Check backend logs for details."
                ),
            )

        # ----------------------------------------------------
        # Start background analysis
        # ----------------------------------------------------

        expected_head_sha = pr.head_sha
        if action == "closed":
            db.commit()
            return {
                "status": "accepted",
                "review_status": "UNCHANGED",
                "ci_status": "UNCHANGED",
                "repository_id": str(repo.id),
                "pull_request_id": str(pr.id),
            }

        logger.info(
            "PR analysis queued: event_type=%s repository=%s pr_number=%s "
            "head_sha=%s matched_current_revision=true",
            x_github_event,
            repo.full_name,
            pr.github_pr_number,
            expected_head_sha,
            extra={
                "event_type": x_github_event,
                "repository": repo.full_name,
                "pr_number": pr.github_pr_number,
                "head_sha": expected_head_sha,
                "matched_current_revision": True,
            },
        )

        revision = (
            db.query(m.CIRevision)
            .filter(
                m.CIRevision.repository_id == repo.id,
                m.CIRevision.pull_request_id == pr.id,
                m.CIRevision.head_sha == expected_head_sha,
            )
            .first()
        )
        if revision is None:
            revision = m.CIRevision(
                repository_id=repo.id,
                pull_request_id=pr.id,
                head_sha=expected_head_sha,
                state="UNKNOWN",
            )
            db.add(revision)
        enqueue_job(
            db,
            "REVIEW",
            f"review:{repo.id}:{pr.id}:{expected_head_sha}",
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "installation_id": installation_id,
                "head_sha": expected_head_sha,
            },
        )
        enqueue_job(
            db,
            "REVIEW_STATUS",
            f"review-status-initial:{pr.id}:{expected_head_sha}",
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "installation_id": installation_id,
                "head_sha": expected_head_sha,
            },
        )
        enqueue_job(
            db,
            "CI_REFRESH",
            f"ci-initial:{repo.id}:{pr.id}:{expected_head_sha}",
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "installation_id": installation_id,
                "head_sha": expected_head_sha,
                "event_name": "pull_request",
                "reason": "initial",
            },
        )
        db.commit()

        logger.info("Webhook processing completed successfully.")

        return {
            "status": "accepted",
            "review_status": "QUEUED",
            "ci_status": "UNKNOWN",
            "repository_id": str(repo.id),
            "pull_request_id": str(pr.id),
            "installation_id": installation_id,
        }

    except HTTPException:
        logger.exception("Webhook request failed with an expected HTTP error.")
        raise

    except Exception:
        logger.exception("UNEXPECTED WEBHOOK ERROR.")

        raise HTTPException(
            status_code=500,
            detail=("Unexpected webhook processing error. Check backend logs."),
        )
