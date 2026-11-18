"""Classify actual CI executions without coupling them to AI review decisions."""
from dataclasses import dataclass


_PENDING_STATUSES = {
    "queued",
    "requested",
    "waiting",
    "pending",
    "in_progress",
}
_FAILED_CONCLUSIONS = {"failure", "error", "timed_out", "startup_failure", "action_required"}
_NON_SUCCESS_TERMINAL = {"cancelled", "neutral", "skipped", "stale"}


@dataclass(frozen=True)
class CIStatus:
    state: str
    pending_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    neutral_count: int = 0
    unknown_count: int = 0
    error: str | None = None


def classify_execution(status: str | None, conclusion: str | None) -> str:
    """Return passed, failed, pending, non_success, or unknown for a real record."""
    normalized_status = (status or "").casefold()
    normalized_conclusion = (conclusion or "").casefold()

    if normalized_status in _PENDING_STATUSES:
        return "pending"
    if normalized_status != "completed":
        return "unknown"
    if normalized_conclusion == "success":
        return "passed"
    if normalized_conclusion in _FAILED_CONCLUSIONS:
        return "failed"
    if normalized_conclusion in _NON_SUCCESS_TERMINAL:
        return "non_success"
    return "unknown"


def evaluate_ci_status(
    executions: list[dict],
    *,
    fetch_error: str | None = None,
    discovery_complete: bool = True,
) -> CIStatus:
    """Summarize discovered executions; suites alone never imply active work."""
    if not executions:
        if fetch_error:
            return CIStatus("UNAVAILABLE", error=fetch_error)
        return CIStatus("NOT_CONFIGURED" if discovery_complete else "UNKNOWN")

    states = [
        classify_execution(item.get("status"), item.get("conclusion"))
        for item in executions
        if item.get("kind") != "check_suite"
    ]
    if not states:
        return CIStatus(
            "UNAVAILABLE" if fetch_error else "NOT_CONFIGURED",
            error=fetch_error,
        )

    pending_count = states.count("pending")
    passed_count = states.count("passed")
    failed_count = states.count("failed")
    neutral_count = states.count("non_success")
    unknown_count = states.count("unknown")

    if failed_count:
        state = "FAILED"
    elif fetch_error:
        state = "UNAVAILABLE"
    elif unknown_count:
        state = "UNKNOWN"
    elif pending_count:
        state = "IN_PROGRESS"
    elif neutral_count:
        state = "NON_SUCCESS"
    elif passed_count:
        state = "PASSED"
    else:
        state = "NOT_CONFIGURED"

    return CIStatus(
        state,
        pending_count=pending_count,
        passed_count=passed_count,
        failed_count=failed_count,
        neutral_count=neutral_count,
        unknown_count=unknown_count,
        error=fetch_error,
    )
