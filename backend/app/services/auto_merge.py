"""Fail-closed request for GitHub's protected auto-merge workflow."""

import logging
from dataclasses import dataclass

import requests
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.integrations.github_client import github_client
from app.models import models as m
from app.services import ci_status, circuit_breaker

logger = logging.getLogger("securepr.auto_merge")
GITHUB_VERIFICATION_ERRORS = (
    requests.RequestException,
    TimeoutError,
    ValueError,
    RuntimeError,
)


@dataclass
class AutoMergeResult:
    status: str
    reason: str
    merge_commit_sha: str | None = None
    response_status: int | None = None
    response_payload: dict | None = None
    ci_state: str | None = None
    ci_checks: list[dict] | None = None
    required_checks: list[dict] | None = None
    policy_revision: int | None = None


def saved_eligibility(db, repo, pr) -> m.AutoApprovalEligibility | None:
    """Resolve the authoritative dashboard policy for this repository and PR author."""
    if (
        not repo
        or not repo.is_active
        or not repo.owner_login
        or not repo.name
        or repo.full_name.casefold() != f"{repo.owner_login}/{repo.name}".casefold()
        or not pr.author_login
    ):
        return None
    user = (
        db.query(m.User)
        .filter(func.lower(m.User.github_login) == pr.author_login.casefold())
        .first()
    )
    if not user:
        return None
    return (
        db.query(m.AutoApprovalEligibility)
        .filter(
            m.AutoApprovalEligibility.repository_id == repo.id,
            m.AutoApprovalEligibility.user_id == user.id,
        )
        .first()
    )


def _failure_message(result: dict) -> str:
    payload = result.get("payload")
    if isinstance(payload, dict):
        errors = payload.get("errors")
        if isinstance(errors, list):
            messages: list[str] = []
            for error in errors:
                if isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str):
                        messages.append(message)
            if messages:
                return "; ".join(messages)
        if isinstance(payload.get("message"), str):
            return payload["message"]
    return "GitHub did not return a usable auto-merge response."


def _is_permission_error(message: str) -> bool:
    normalized = message.casefold()
    return (
        "resource not accessible by integration" in normalized
        or "permission" in normalized
        or "forbidden" in normalized
    )


def _capabilities_pull_request(response: dict) -> dict:
    payload = response.get("payload")
    repository = payload.get("repository") if isinstance(payload, dict) else None
    pull_request = (
        repository.get("pullRequest") if isinstance(repository, dict) else None
    )
    if not isinstance(pull_request, dict):
        raise TypeError("GitHub did not return the pull request auto-merge state.")
    return pull_request


def _disable_and_verify_auto_merge(
    *, installation_id: str, owner: str, repo_name: str, pr_number: int, node_id: str
) -> None:
    disabled = github_client.disable_pull_request_auto_merge(installation_id, node_id)
    if not disabled.get("succeeded"):
        raise RuntimeError(
            f"GitHub could not cancel the stale auto-merge request: "
            f"{_failure_message(disabled)}"
        )
    confirmation = github_client.get_auto_merge_capabilities(
        installation_id, owner, repo_name, pr_number
    )
    if not confirmation.get("succeeded"):
        raise RuntimeError(
            "GitHub could not verify cancellation of the stale auto-merge request: "
            f"{_failure_message(confirmation)}"
        )
    if _capabilities_pull_request(confirmation).get("autoMergeRequest"):
        raise RuntimeError(
            "GitHub still reports an active auto-merge request after cancellation."
        )


def cancel_pending_auto_merge(
    *,
    installation_id: str,
    owner: str,
    repo_name: str,
    pr_number: int,
    db=None,
    pull_request_id: str | None = None,
    new_head_sha: str | None = None,
    cancellation_status: str = "CANCELLED_STALE_REVISION",
    cancellation_reason: str = (
        "GitHub auto-merge was cancelled after the PR head changed."
    ),
    stale_only: bool = True,
) -> bool:
    """Cancel any existing request before processing a newer PR revision."""
    response = github_client.get_auto_merge_capabilities(
        installation_id, owner, repo_name, pr_number
    )
    if not response.get("succeeded"):
        raise RuntimeError(
            "GitHub could not inspect the existing auto-merge request: "
            f"{_failure_message(response)}"
        )
    pull_request = _capabilities_pull_request(response)
    if not pull_request.get("autoMergeRequest"):
        _record_auto_merge_cancellation(
            db,
            pull_request_id,
            new_head_sha=new_head_sha,
            cancellation_status=cancellation_status,
            cancellation_reason=cancellation_reason,
            stale_only=stale_only,
        )
        return False
    node_id = pull_request.get("id")
    if not isinstance(node_id, str) or not node_id:
        raise RuntimeError(
            "GitHub did not return the PR node ID needed to cancel auto-merge."
        )
    _disable_and_verify_auto_merge(
        installation_id=installation_id,
        owner=owner,
        repo_name=repo_name,
        pr_number=pr_number,
        node_id=node_id,
    )
    _record_auto_merge_cancellation(
        db,
        pull_request_id,
        new_head_sha=new_head_sha,
        cancellation_status=cancellation_status,
        cancellation_reason=cancellation_reason,
        stale_only=stale_only,
    )
    logger.info(
        "Cancelled existing GitHub auto-merge request: repository=%s pr_number=%s",
        f"{owner}/{repo_name}",
        pr_number,
        extra={
            "repository": f"{owner}/{repo_name}",
            "pr_number": pr_number,
            "cancellation_result": "verified",
        },
    )
    return True


def _record_auto_merge_cancellation(
    db,
    pull_request_id: str | None,
    *,
    new_head_sha: str | None,
    cancellation_status: str,
    cancellation_reason: str,
    stale_only: bool,
) -> None:
    if db is None or not pull_request_id:
        return
    query = db.query(m.Review).filter(
        m.Review.pull_request_id == pull_request_id,
        m.Review.auto_merge_status == "AUTO_MERGE_ENABLED",
    )
    if stale_only:
        if not new_head_sha:
            return
        query = query.filter(m.Review.head_sha != new_head_sha)
    elif new_head_sha:
        query = query.filter(m.Review.head_sha == new_head_sha)
    cancelled_reviews = query.all()
    for review in cancelled_reviews:
        review.auto_merge_status = cancellation_status
        review.auto_merge_failure_reason = cancellation_reason
        db.add(
            m.AuditLog(
                actor="securepr",
                action="cancel_github_auto_merge",
                target_type="pull_request",
                target_id=pull_request_id,
                details_json={
                    "review_id": review.id,
                    "reviewed_sha": review.head_sha,
                    "current_sha": new_head_sha,
                    "reason": cancellation_reason,
                    "cancellation_result": "verified",
                },
            )
        )
    if cancelled_reviews:
        db.commit()


def attempt(
    *,
    db,
    repo,
    pr,
    review,
    installation_id,
    findings,
):
    """Revalidate the exact reviewed SHA and request auto-merge (never direct merge)."""
    settings = get_settings()
    if (
        pr.head_sha != review.head_sha
        or pr.state != "open"
        or pr.ai_state != "AI_COMPLETED"
        or review.ai_state != "AI_COMPLETED"
        or review.analysis_completed_at is None
    ):
        return AutoMergeResult(
            "NOT_ELIGIBLE",
            "A validated AI analysis for the current PR head SHA is required.",
        )
    if review.decision.value != "APPROVED" or not review.github_action_confirmed:
        return AutoMergeResult(
            "NOT_ELIGIBLE", "A confirmed SecurePR approval is required."
        )
    eligibility = saved_eligibility(db, repo, pr)
    if eligibility is None:
        return AutoMergeResult(
            "DASHBOARD_CONFIGURATION_MISSING",
            "No valid saved dashboard configuration exists for this repository and GitHub account.",
        )
    if not eligibility.is_enabled:
        return AutoMergeResult(
            "DISABLED_DASHBOARD_CONFIGURATION",
            "Auto-approval is disabled in the saved dashboard configuration.",
        )
    if not 0 <= eligibility.max_risk_threshold <= 100:
        return AutoMergeResult(
            "NOT_ELIGIBLE",
            "The saved dashboard risk threshold is invalid.",
        )
    if review.overall_score > eligibility.max_risk_threshold:
        return AutoMergeResult(
            "NOT_ELIGIBLE", "Risk exceeds the saved dashboard threshold."
        )
    policy_revision = eligibility.policy_revision
    if any(
        f.get("severity") in ("CRITICAL", "HIGH") or f.get("is_mandatory_blocker")
        for f in findings
    ):
        return AutoMergeResult(
            "NOT_ELIGIBLE",
            "Critical, high-severity, or mandatory-blocker findings prevent auto-merge.",
        )

    merge_method = settings.AUTO_MERGE_METHOD.upper()
    if merge_method not in {"MERGE", "SQUASH", "REBASE"}:
        return AutoMergeResult(
            "NOT_ELIGIBLE", "AUTO_MERGE_METHOD must be MERGE, SQUASH, or REBASE."
        )

    owner, repo_name = repo.full_name.split("/", 1)
    pr_number = pr.github_pr_number
    base_branch = pr.base_branch
    review_sha = review.head_sha
    repository_id = repo.id
    author_login = pr.author_login
    db.commit()
    try:
        live_pr = github_client.get_pull_request(
            installation_id, owner, repo_name, pr_number
        )
        if live_pr.get("state") != "open":
            return AutoMergeResult("NOT_ELIGIBLE", "PR is no longer open.")
        if (
            live_pr.get("head_sha") != review_sha
            or live_pr.get("base_ref") != base_branch
        ):
            return AutoMergeResult(
                "NOT_ELIGIBLE", "PR head or base changed after SecurePR analysis."
            )
        try:
            checks = github_client.get_check_runs(
                installation_id, owner, repo_name, review_sha
            )
            statuses = github_client.get_commit_statuses(
                installation_id, owner, repo_name, review_sha
            )
            required_checks = github_client.get_required_status_checks(
                installation_id, owner, repo_name, base_branch
            )
            github_client.get_required_pull_request_reviews(
                installation_id, owner, repo_name, base_branch
            )
        except GITHUB_VERIFICATION_ERRORS as exc:
            return AutoMergeResult(
                "BRANCH_PROTECTION_UNAVAILABLE",
                f"GitHub could not verify CI and fresh-review branch protection: {exc}",
                ci_state="UNAVAILABLE",
            )
        ci_checks = checks + statuses
        live_ci = ci_status.evaluate_ci_status(
            ci_checks,
            required_checks=required_checks,
            discovery_complete=True,
        )
        if live_ci.state == "FAILED":
            return AutoMergeResult(
                "CI_FAILED",
                "At least one applicable CI check failed on the reviewed SHA.",
                ci_state=live_ci.state,
                ci_checks=ci_checks,
                required_checks=required_checks,
            )
        if live_ci.state != "PASSED":
            return AutoMergeResult(
                "CI_PENDING",
                f"Required CI verification is {live_ci.state.lower()}; auto-merge was not requested.",
                ci_state=live_ci.state,
                ci_checks=ci_checks,
                required_checks=required_checks,
            )

        breaker_tripped, breaker_reason = circuit_breaker.is_tripped(
            db, repository_id, author_login
        )
        db.commit()
        if breaker_tripped:
            return AutoMergeResult(
                "NOT_ELIGIBLE", f"Circuit breaker became active: {breaker_reason}"
            )

        capabilities = github_client.get_auto_merge_capabilities(
            installation_id, owner, repo_name, pr_number
        )
        if not capabilities.get("succeeded"):
            message = _failure_message(capabilities)
            if _is_permission_error(message):
                return AutoMergeResult(
                    "PERMISSION_MISSING",
                    f"GitHub App cannot inspect or request auto-merge: {message}",
                    response_status=capabilities.get("status_code"),
                    response_payload=capabilities.get("payload"),
                )
            return AutoMergeResult(
                "VERIFICATION_UNAVAILABLE",
                f"Could not verify GitHub auto-merge requirements: {message}",
                response_status=capabilities.get("status_code"),
                response_payload=capabilities.get("payload"),
            )

        data = capabilities.get("payload") or {}
        github_repo = data.get("repository") or {}
        github_pr = github_repo.get("pullRequest") or {}
        if not isinstance(github_repo.get("autoMergeAllowed"), bool):
            return AutoMergeResult(
                "VERIFICATION_UNAVAILABLE",
                "GitHub did not report whether repository auto-merge is allowed.",
            )
        if not github_repo["autoMergeAllowed"]:
            return AutoMergeResult(
                "GITHUB_AUTO_MERGE_DISABLED",
                "GitHub repository settings do not allow auto-merge.",
            )
        if github_pr.get("headRefOid") != review_sha:
            return AutoMergeResult(
                "NOT_ELIGIBLE",
                "PR head changed during GitHub eligibility verification.",
            )
        if github_pr.get("state") != "OPEN" or github_pr.get("isDraft"):
            return AutoMergeResult(
                "NOT_ELIGIBLE", "GitHub reports that the PR is closed or still a draft."
            )

        review_decision = github_pr.get("reviewDecision")
        if review_decision in ("REVIEW_REQUIRED", "REVIEW_PENDING"):
            return AutoMergeResult(
                "REQUIRED_REVIEW_PENDING",
                "GitHub branch protection is waiting for required review approvals.",
            )
        if review_decision == "CHANGES_REQUESTED":
            return AutoMergeResult(
                "REVIEW_BLOCKED", "GitHub has an outstanding change-request review."
            )

        mergeable_state = live_pr.get("mergeable_state")
        if mergeable_state != "clean":
            state_reasons = {
                "dirty": "GitHub reports merge conflicts.",
                "behind": "The PR branch is behind its base branch.",
                "blocked": "GitHub reports additional branch-protection requirements are unmet.",
                "unstable": "GitHub reports one or more status checks are unsuccessful.",
            }
            return AutoMergeResult(
                "WAITING_FOR_GITHUB",
                state_reasons.get(
                    mergeable_state,
                    f"GitHub reports merge state '{mergeable_state or 'unknown'}'; "
                    "waiting for a clean merge state.",
                ),
            )
        if live_pr.get("mergeable") is not True:
            return AutoMergeResult(
                "WAITING_FOR_GITHUB",
                "GitHub reports that the PR is not currently mergeable.",
            )

        method_field = {
            "MERGE": "mergeCommitAllowed",
            "SQUASH": "squashMergeAllowed",
            "REBASE": "rebaseMergeAllowed",
        }[merge_method]
        if github_repo.get(method_field) is not True:
            return AutoMergeResult(
                "MERGE_METHOD_UNAVAILABLE",
                f"GitHub repository settings do not allow the configured {merge_method} merge method.",
            )

        auto_merge_request = github_pr.get("autoMergeRequest")
        if auto_merge_request:
            if auto_merge_request.get("mergeMethod") == merge_method:
                return AutoMergeResult(
                    "AUTO_MERGE_ENABLED",
                    f"GitHub already has {merge_method} auto-merge enabled for this PR.",
                    response_payload=auto_merge_request,
                    policy_revision=policy_revision,
                )
            return AutoMergeResult(
                "AUTO_MERGE_CONFLICT",
                "GitHub already has an auto-merge request using a different merge method.",
            )

        pull_request_id = github_pr.get("id")
        if not isinstance(pull_request_id, str) or not pull_request_id:
            return AutoMergeResult(
                "VERIFICATION_UNAVAILABLE",
                "GitHub did not return the PR node ID required for auto-merge.",
            )
        try:
            final_checks = github_client.get_check_runs(
                installation_id, owner, repo_name, review_sha
            )
            final_statuses = github_client.get_commit_statuses(
                installation_id, owner, repo_name, review_sha
            )
            final_required_checks = github_client.get_required_status_checks(
                installation_id, owner, repo_name, base_branch
            )
            github_client.get_required_pull_request_reviews(
                installation_id, owner, repo_name, base_branch
            )
            final_live_pr = github_client.get_pull_request(
                installation_id, owner, repo_name, pr_number
            )
        except GITHUB_VERIFICATION_ERRORS as exc:
            return AutoMergeResult(
                "VERIFICATION_UNAVAILABLE",
                f"GitHub could not revalidate the PR immediately before auto-merge: {exc}",
                ci_state="UNAVAILABLE",
            )
        if (
            final_live_pr.get("state") != "open"
            or final_live_pr.get("head_sha") != review_sha
            or final_live_pr.get("base_ref") != base_branch
            or final_live_pr.get("mergeable") is not True
            or final_live_pr.get("mergeable_state") != "clean"
        ):
            return AutoMergeResult(
                "NOT_ELIGIBLE",
                "The PR head, base, or mergeability changed during final auto-merge verification.",
            )
        final_ci_checks = final_checks + final_statuses
        final_ci = ci_status.evaluate_ci_status(
            final_ci_checks,
            required_checks=final_required_checks,
            discovery_complete=True,
        )
        if final_ci.state != "PASSED":
            return AutoMergeResult(
                "CI_FAILED" if final_ci.state == "FAILED" else "CI_PENDING",
                "The final live CI verification did not pass for the reviewed SHA.",
                ci_state=final_ci.state,
                ci_checks=final_ci_checks,
                required_checks=final_required_checks,
            )
        current_pr = (
            db.query(m.PullRequest)
            .filter(m.PullRequest.id == pr.id)
            .populate_existing()
            .first()
        )
        if (
            current_pr is None
            or current_pr.head_sha != review_sha
            or current_pr.state != "open"
        ):
            return AutoMergeResult(
                "NOT_ELIGIBLE",
                "The saved PR revision changed during final auto-merge verification.",
            )
        current_eligibility = saved_eligibility(db, repo, pr)
        if current_eligibility is None:
            return AutoMergeResult(
                "DASHBOARD_CONFIGURATION_MISSING",
                "The saved dashboard configuration is no longer available.",
            )
        if not current_eligibility.is_enabled:
            return AutoMergeResult(
                "DISABLED_DASHBOARD_CONFIGURATION",
                "Auto-approval was disabled in the saved dashboard configuration.",
            )
        if (
            not 0 <= current_eligibility.max_risk_threshold <= 100
            or review.overall_score > current_eligibility.max_risk_threshold
            or current_eligibility.policy_revision != policy_revision
        ):
            return AutoMergeResult(
                "NOT_ELIGIBLE",
                "The latest saved dashboard risk threshold does not authorize this review.",
            )
        db.commit()
        result = github_client.enable_pull_request_auto_merge(
            installation_id, pull_request_id, merge_method
        )
        if not result.get("succeeded"):
            message = _failure_message(result)
            if _is_permission_error(message):
                return AutoMergeResult(
                    "PERMISSION_MISSING",
                    f"GitHub App is missing permission to enable auto-merge: {message}",
                    response_status=result.get("status_code"),
                    response_payload=result.get("payload"),
                )
            return AutoMergeResult(
                "FAILED",
                f"GitHub rejected the auto-merge request: {message}",
                response_status=result.get("status_code"),
                response_payload=result.get("payload"),
            )
        enabled_pr = (
            (result.get("payload") or {})
            .get("enablePullRequestAutoMerge", {})
            .get("pullRequest", {})
        )
        request = (
            enabled_pr.get("autoMergeRequest") if isinstance(enabled_pr, dict) else None
        )
        if not isinstance(request, dict) or not request.get("enabledAt"):
            return AutoMergeResult(
                "FAILED",
                "GitHub returned success without confirming that auto-merge was enabled.",
                response_status=result.get("status_code"),
                response_payload=result.get("payload"),
            )
        try:
            post_request_pr = github_client.get_pull_request(
                installation_id, owner, repo_name, pr_number
            )
            github_client.get_required_pull_request_reviews(
                installation_id, owner, repo_name, base_branch
            )
        except GITHUB_VERIFICATION_ERRORS as exc:
            try:
                _disable_and_verify_auto_merge(
                    installation_id=installation_id,
                    owner=owner,
                    repo_name=repo_name,
                    pr_number=pr_number,
                    node_id=pull_request_id,
                )
            except GITHUB_VERIFICATION_ERRORS:
                logger.exception(
                    "Could not cancel auto-merge after live PR verification failed: "
                    "repository=%s pr_number=%s head_sha=%s",
                    repo.full_name,
                    pr_number,
                    review_sha,
                )
            return AutoMergeResult(
                "VERIFICATION_UNAVAILABLE",
                f"GitHub could not verify the PR and fresh-review protection "
                f"immediately after requesting auto-merge: {exc}",
            )
        if (
            post_request_pr.get("state") != "open"
            or post_request_pr.get("head_sha") != review_sha
            or post_request_pr.get("base_ref") != base_branch
        ):
            try:
                _disable_and_verify_auto_merge(
                    installation_id=installation_id,
                    owner=owner,
                    repo_name=repo_name,
                    pr_number=pr_number,
                    node_id=pull_request_id,
                )
            except GITHUB_VERIFICATION_ERRORS as exc:
                logger.exception(
                    "Could not cancel auto-merge for a stale PR revision: "
                    "repository=%s pr_number=%s reviewed_sha=%s current_sha=%s",
                    repo.full_name,
                    pr_number,
                    review_sha,
                    post_request_pr.get("head_sha"),
                )
                return AutoMergeResult(
                    "CANCELLATION_UNVERIFIED",
                    f"The PR changed during auto-merge activation and GitHub could "
                    f"not verify cancellation: {exc}",
                )
            return AutoMergeResult(
                "NOT_ELIGIBLE",
                "The PR changed during auto-merge activation; the request was cancelled.",
            )
        current_pr = (
            db.query(m.PullRequest)
            .filter(m.PullRequest.id == pr.id)
            .populate_existing()
            .first()
        )
        current_eligibility = saved_eligibility(db, repo, pr)
        db.commit()
        if (
            current_pr is None
            or current_pr.head_sha != review_sha
            or current_pr.state != "open"
            or current_eligibility is None
            or not current_eligibility.is_enabled
            or not 0 <= current_eligibility.max_risk_threshold <= 100
            or review.overall_score > current_eligibility.max_risk_threshold
            or current_eligibility.policy_revision != policy_revision
        ):
            try:
                _disable_and_verify_auto_merge(
                    installation_id=installation_id,
                    owner=owner,
                    repo_name=repo_name,
                    pr_number=pr_number,
                    node_id=pull_request_id,
                )
            except SQLAlchemyError:
                logger.exception(
                    "Database failure while verifying auto-merge authorization: "
                    "repository=%s pr_number=%s",
                    repo.full_name,
                    pr_number,
                )
                raise
            except GITHUB_VERIFICATION_ERRORS as exc:
                logger.exception(
                    "Could not cancel auto-merge after saved authorization changed: "
                    "repository=%s pr_number=%s head_sha=%s",
                    repo.full_name,
                    pr_number,
                    review_sha,
                )
                return AutoMergeResult(
                    "CANCELLATION_UNVERIFIED",
                    f"Saved authorization changed after auto-merge activation and "
                    f"cancellation could not be verified: {exc}",
                )
            return AutoMergeResult(
                "NOT_ELIGIBLE",
                "The PR or saved dashboard authorization changed during "
                "auto-merge activation; the request was cancelled.",
            )
        return AutoMergeResult(
            "AUTO_MERGE_ENABLED",
            f"GitHub confirmed {merge_method} auto-merge is enabled; the PR is not yet merged.",
            response_status=result.get("status_code"),
            response_payload=result.get("payload"),
            policy_revision=policy_revision,
        )
    except SQLAlchemyError:
        logger.exception(
            "Database failure while verifying auto-merge authorization: "
            "repository=%s pr_number=%s",
            repo.full_name,
            pr_number,
        )
        raise
    except GITHUB_VERIFICATION_ERRORS as exc:
        return AutoMergeResult(
            "VERIFICATION_UNAVAILABLE",
            f"GitHub auto-merge eligibility could not be verified: {exc}",
        )
