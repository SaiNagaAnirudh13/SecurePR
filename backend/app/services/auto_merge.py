"""Fail-closed auto-merge gate, executed only after final SecurePR analysis."""
from dataclasses import dataclass

from app.core.config import get_settings
from app.integrations.github_client import github_client
from app.services import circuit_breaker, policy_engine


@dataclass
class AutoMergeResult:
    status: str
    reason: str
    merge_commit_sha: str | None = None
    response_status: int | None = None
    response_payload: dict | None = None


def attempt(*, db, repo, pr, review, installation_id, policy_doc, findings, author_eligible, author_threshold):
    """Re-check every mutable external condition, then merge with a SHA guard."""
    settings = get_settings()
    if review.decision.value != "APPROVED":
        return AutoMergeResult("NOT_ELIGIBLE", "Final SecurePR decision is not APPROVED.")
    if not review.github_action_confirmed:
        return AutoMergeResult("MANUAL_REQUIRED", "GitHub did not confirm SecurePR's approval action.")
    allowed_categories = policy_doc.get("auto_merge_allowed_risk_categories", ["LOW"])
    valid_categories = {"LOW", "MEDIUM", "HIGH"}
    if (
        not isinstance(allowed_categories, list)
        or any(
            not isinstance(category, str) or category not in valid_categories
            for category in allowed_categories
        )
    ):
        return AutoMergeResult("NOT_ELIGIBLE", "Repository policy has invalid auto-merge risk categories.")
    if review.risk_category not in allowed_categories:
        return AutoMergeResult("NOT_ELIGIBLE", "Repository policy does not permit auto-merge at this risk category.")
    if settings.AUTO_MERGE_LOW_RISK_ONLY and review.risk_category != "LOW":
        return AutoMergeResult("NOT_ELIGIBLE", "Global configuration permits auto-merge only for LOW risk.")
    if not settings.AUTO_MERGE_ENABLED:
        return AutoMergeResult("DISABLED_GLOBAL", "Automatic merging is disabled by configuration.")
    if not repo.is_auto_merge_enabled:
        return AutoMergeResult("DISABLED_REPOSITORY", "This repository is not configured to allow automatic merging.")
    if not author_eligible or author_threshold is None or review.overall_score > author_threshold:
        return AutoMergeResult("NOT_ELIGIBLE", "Author is not eligible at this review's risk threshold.")
    if any(f.get("severity") in ("CRITICAL", "HIGH") or f.get("is_mandatory_blocker") for f in findings):
        return AutoMergeResult("NOT_ELIGIBLE", "Critical, high-severity, or mandatory-blocker findings prevent auto-merge.")

    owner, repo_name = repo.full_name.split("/", 1)
    try:
        live_pr = github_client.get_pull_request(installation_id, owner, repo_name, pr.github_pr_number)
        if live_pr.get("state") != "open":
            return AutoMergeResult("MANUAL_REQUIRED", "PR is no longer open.")
        if live_pr.get("head_sha") != review.head_sha or live_pr.get("base_ref") != pr.base_branch:
            return AutoMergeResult("MANUAL_REQUIRED", "PR head or base changed after SecurePR analysis.")
        # GitHub may briefly return null while calculating mergeability; never
        # interpret that as permission to merge.
        if live_pr.get("mergeable") is not True or live_pr.get("mergeable_state") in ("dirty", "blocked", "behind", "unknown"):
            return AutoMergeResult("MANUAL_REQUIRED", "GitHub reports that the PR is not currently mergeable.")
        # Required CI may be represented as modern check runs or legacy
        # commit statuses; auto-merge must re-validate both representations.
        live_checks = github_client.get_check_runs(installation_id, owner, repo_name, review.head_sha)
        live_checks += github_client.get_commit_statuses(installation_id, owner, repo_name, review.head_sha)
        live_policy = policy_engine.evaluate_policy(policy_doc, [], live_checks, pr.author_login)
        if live_policy.violations or live_policy.mandatory_checks_failed:
            return AutoMergeResult("MANUAL_REQUIRED", "Mandatory CI checks are failing or policy validation failed on re-check.")
        if live_policy.mandatory_checks_pending:
            return AutoMergeResult(
                "MANUAL_REQUIRED",
                "Mandatory CI checks are not all complete and successful on re-check.",
            )
        tripped, reason = circuit_breaker.is_tripped(db, repo.id, pr.author_login)
        if tripped:
            return AutoMergeResult("MANUAL_REQUIRED", f"Circuit breaker became active: {reason}")

        merged = github_client.merge_pull_request(installation_id, owner, repo_name, pr.github_pr_number, review.head_sha)
        if merged["succeeded"]:
            return AutoMergeResult("MERGED", "All auto-merge safety checks passed.",
                                   merged["payload"].get("sha"), merged["status_code"], merged["payload"])
        return AutoMergeResult("FAILED", merged["payload"].get("message", "GitHub rejected the merge request."),
                               response_status=merged["status_code"], response_payload=merged["payload"])
    except Exception as exc:
        return AutoMergeResult("FAILED", f"GitHub re-validation or merge request failed: {exc}")
