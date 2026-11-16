"""Classify dynamically discovered GitHub CI checks for one commit revision."""
import json
from dataclasses import dataclass

_PENDING_STATUSES = {"queued", "in_progress", "pending", "requested", "waiting"}
_SUCCESS_CONCLUSIONS = {"success"}
_NEUTRAL_CONCLUSIONS = {"neutral", "skipped"}
_FAILURE_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "error",
    "failure",
    "stale",
    "startup_failure",
    "timed_out",
}


@dataclass(frozen=True)
class CIStatus:
    state: str
    finalized: bool
    pending_checks: tuple[str, ...] = ()
    error: str | None = None


def snapshot_signature(checks: list[dict], suites: list[dict]) -> str:
    """Return a stable identity/state signature, ignoring poll timestamps."""
    snapshot = []
    for kind, items in (("check", checks), ("suite", suites)):
        for item in items:
            snapshot.append({
                "kind": kind,
                "identity": item.get("identity"),
                "name": item.get("name"),
                "provider": item.get("provider"),
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "sha": item.get("sha"),
            })
    snapshot.sort(key=lambda item: (
        str(item["kind"]),
        str(item["identity"]),
        str(item["provider"]),
        str(item["name"]),
    ))
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"))


def _item_state(item: dict) -> tuple[str, str | None]:
    status = str(item.get("status") or "").casefold()
    if status in _PENDING_STATUSES:
        return "pending", str(item.get("name") or item.get("identity") or "Unnamed check")
    if status != "completed":
        return "error", str(item.get("name") or item.get("identity") or "Unnamed check")

    conclusion = str(item.get("conclusion") or "").casefold()
    if conclusion in _SUCCESS_CONCLUSIONS:
        return "success", None
    if conclusion in _NEUTRAL_CONCLUSIONS:
        return "neutral", None
    if conclusion in _FAILURE_CONCLUSIONS:
        return "failure", None
    return "error", str(item.get("name") or item.get("identity") or "Unnamed check")


def evaluate_ci_status(
    checks: list[dict],
    suites: list[dict],
    *,
    required_checks_pending: list[str],
    fetch_error: str | None,
    quiet_seconds: float,
    stabilization_seconds: int,
    timed_out: bool = False,
) -> CIStatus:
    """Evaluate provider results without conflating them with SecurePR policy."""
    if fetch_error and not timed_out:
        return CIStatus("ERROR", False, ("GitHub CI status unavailable",), fetch_error)
    if timed_out:
        return CIStatus("TIMEOUT", True, error=fetch_error)

    pending = []
    has_failure = False
    has_neutral = False
    has_error = False
    for item in checks + suites:
        state, name = _item_state(item)
        if state == "pending" and name:
            pending.append(name)
        elif state == "failure":
            has_failure = True
        elif state == "neutral":
            has_neutral = True
        elif state == "error":
            has_error = True

    pending = sorted(set(pending))
    if has_error:
        return CIStatus("ERROR", False, ("GitHub returned an unrecognized CI state",))
    if pending:
        return CIStatus("RUNNING", False, tuple(pending))
    if quiet_seconds < stabilization_seconds:
        return CIStatus("DISCOVERING", False, ("Waiting for CI checks to stabilize",))
    if not checks:
        return CIStatus("NO_CHECKS_FOUND" if suites else "NO_CI_CONFIGURED", True)
    if required_checks_pending:
        return CIStatus("RUNNING", False, tuple(sorted(set(required_checks_pending))))
    if has_failure:
        return CIStatus("FAILURE", True)
    if has_neutral:
        return CIStatus("NEUTRAL", True)
    return CIStatus("SUCCESS", True)
