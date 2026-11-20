"""Classify actual CI executions without coupling them to AI review decisions."""

from dataclasses import dataclass

_PENDING_STATUSES = {
    "queued",
    "requested",
    "waiting",
    "pending",
    "in_progress",
}
_FAILED_CONCLUSIONS = {
    "failure",
    "error",
    "timed_out",
    "startup_failure",
    "action_required",
}
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
    missing_required_checks: tuple[str, ...] = ()


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
    discovery_complete: bool = False,
    required_checks: list[dict] | None = None,
) -> CIStatus:
    """Summarize actual executions and required checks without permissive defaults."""
    seen: set[tuple[str, str]] = set()
    executable_records = []
    for item in executions:
        if item.get("kind") == "check_suite":
            continue
        identity = item.get("identity")
        if identity:
            key = (str(item.get("kind") or "check_run"), str(identity))
            if key in seen:
                continue
            seen.add(key)
        executable_records.append(item)

    states = [
        classify_execution(item.get("status"), item.get("conclusion"))
        for item in executable_records
    ]

    pending_count = states.count("pending")
    passed_count = states.count("passed")
    failed_count = states.count("failed")
    neutral_count = states.count("non_success")
    unknown_count = states.count("unknown")
    missing_required = []
    for required in required_checks or []:
        context = required.get("context")
        if not isinstance(context, str) or not context:
            continue
        app_id = required.get("app_id")
        matching = [
            item
            for item in executable_records
            if (item.get("context") or item.get("name")) == context
            and (app_id is None or item.get("app_id") == app_id)
        ]
        if not matching:
            missing_required.append(context)
            pending_count += 1

    if failed_count:
        state = "FAILED"
    elif fetch_error:
        state = "UNAVAILABLE"
    elif not discovery_complete:
        state = "UNKNOWN"
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
        missing_required_checks=tuple(missing_required),
    )
