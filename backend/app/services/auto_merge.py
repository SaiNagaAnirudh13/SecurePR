"""Fail-closed request for GitHub's protected auto-merge workflow."""

from dataclasses import dataclass

from app.core.config import get_settings
from app.integrations.github_client import github_client
from app.services import ci_status, circuit_breaker


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


def attempt(
    *,
    db,
    repo,
    pr,
    review,
    installation_id,
    policy_doc,
    findings,
    author_eligible,
    author_threshold,
):
    """Revalidate the exact reviewed SHA and request auto-merge (never direct merge)."""
    settings = get_settings()
    if review.decision.value != "APPROVED" or not review.github_action_confirmed:
        return AutoMergeResult(
            "NOT_ELIGIBLE", "A confirmed SecurePR approval is required."
        )
    if not settings.AUTO_MERGE_ENABLED:
        return AutoMergeResult(
            "DISABLED_GLOBAL", "Auto-merge is disabled by SecurePR configuration."
        )
    if not repo.is_auto_merge_enabled:
        return AutoMergeResult(
            "DISABLED_REPOSITORY",
            "Auto-merge is disabled for this repository in SecurePR settings.",
        )

    allowed_categories = policy_doc.get("auto_merge_allowed_risk_categories", ["LOW"])
    valid_categories = {"LOW", "MEDIUM", "HIGH"}
    if not isinstance(allowed_categories, list) or any(
        not isinstance(category, str) or category not in valid_categories
        for category in allowed_categories
    ):
        return AutoMergeResult(
            "NOT_ELIGIBLE", "Repository policy has invalid auto-merge risk categories."
        )
    if review.risk_category not in allowed_categories:
        return AutoMergeResult(
            "NOT_ELIGIBLE",
            "Repository policy does not permit auto-merge at this risk category.",
        )
    if settings.AUTO_MERGE_LOW_RISK_ONLY and review.risk_category != "LOW":
        return AutoMergeResult(
            "NOT_ELIGIBLE", "Global configuration permits auto-merge only for LOW risk."
        )
    if (
        not author_eligible
        or author_threshold is None
        or review.overall_score > author_threshold
    ):
        return AutoMergeResult(
            "NOT_ELIGIBLE", "Author is not eligible at this review's risk threshold."
        )
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
    try:
        live_pr = github_client.get_pull_request(
            installation_id, owner, repo_name, pr.github_pr_number
        )
        if live_pr.get("state") != "open":
            return AutoMergeResult("NOT_ELIGIBLE", "PR is no longer open.")
        if (
            live_pr.get("head_sha") != review.head_sha
            or live_pr.get("base_ref") != pr.base_branch
        ):
            return AutoMergeResult(
                "NOT_ELIGIBLE", "PR head or base changed after SecurePR analysis."
            )
        try:
            checks = github_client.get_check_runs(
                installation_id, owner, repo_name, review.head_sha
            )
            statuses = github_client.get_commit_statuses(
                installation_id, owner, repo_name, review.head_sha
            )
            required_checks = github_client.get_required_status_checks(
                installation_id, owner, repo_name, pr.base_branch
            )
        except Exception as exc:
            return AutoMergeResult(
                "CI_VERIFICATION_UNAVAILABLE",
                f"GitHub CI verification failed before auto-merge: {exc}",
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
            db, repo.id, pr.author_login
        )
        if breaker_tripped:
            return AutoMergeResult(
                "NOT_ELIGIBLE", f"Circuit breaker became active: {breaker_reason}"
            )

        capabilities = github_client.get_auto_merge_capabilities(
            installation_id, owner, repo_name, pr.github_pr_number
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
        if github_pr.get("headRefOid") != review.head_sha:
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
        return AutoMergeResult(
            "AUTO_MERGE_ENABLED",
            f"GitHub confirmed {merge_method} auto-merge is enabled; the PR is not yet merged.",
            response_status=result.get("status_code"),
            response_payload=result.get("payload"),
        )
    except Exception as exc:
        return AutoMergeResult(
            "VERIFICATION_UNAVAILABLE",
            f"GitHub auto-merge eligibility could not be verified: {exc}",
        )
