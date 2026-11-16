"""
Policy Engine.

Loads policy documents (JSON, versioned, stored in the `policies` table and
mirrored on disk under /policies for human editing) and evaluates a PR
against them. Policies are data, not code -- changing approval rules never
requires touching application source.
"""
import fnmatch
import json
import os
import re
from dataclasses import dataclass, field

POLICY_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "policies")


def normalize_check_name(name: str | None) -> str:
    """Canonicalize GitHub check names so logical names and human-readable names match.

    Examples:
      "Build / Install dependencies" -> "build-install-dependencies"
      "Tests / Unit tests" -> "tests-unit-tests"
      "lint" -> "lint"
    """
    if not name:
        return ""
    value = name.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def _iter_mandatory_check_names(policy: dict) -> list[str]:
    entries = policy.get("mandatory_ci_checks", [])
    names: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            names.append(entry)
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            names.append(entry["name"])
    return names


def _configured_aliases(policy: dict, logical_name: str) -> list[str]:
    alias_map = policy.get("ci_check_aliases", {}) or {}
    if not isinstance(alias_map, dict):
        return []
    matches: list[str] = []
    for key in (logical_name, normalize_check_name(logical_name)):
        if key in alias_map:
            value = alias_map[key]
            if isinstance(value, str):
                matches.append(value)
            elif isinstance(value, list):
                matches.extend(item for item in value if isinstance(item, str))
    return matches


def _check_matches_required_name(policy: dict, required_name: str, candidate_name: str | None) -> bool:
    if not candidate_name:
        return False
    required_key = required_name.strip()
    candidate_key = candidate_name.strip()
    if required_key.lower() == candidate_key.lower():
        return True

    required_norm = normalize_check_name(required_key)
    candidate_norm = normalize_check_name(candidate_key)
    if required_norm == candidate_norm:
        return True

    alias_names = {normalize_check_name(alias) for alias in _configured_aliases(policy, required_name)}
    alias_names.add(required_norm)
    return candidate_norm in alias_names


def _prefer_check(candidate: dict, previous: dict) -> bool:
    candidate_time = (
        candidate.get("started_at")
        or candidate.get("created_at")
        or candidate.get("completed_at")
        or ""
    )
    previous_time = (
        previous.get("started_at")
        or previous.get("created_at")
        or previous.get("completed_at")
        or ""
    )
    if candidate_time and previous_time and candidate_time != previous_time:
        return candidate_time > previous_time
    if candidate_time != previous_time:
        return bool(candidate_time)

    candidate_pending = candidate.get("status") != "completed"
    previous_pending = previous.get("status") != "completed"
    if candidate_pending != previous_pending:
        return candidate_pending

    candidate_failed = candidate.get("conclusion") not in ("success", "neutral", "skipped")
    previous_failed = previous.get("conclusion") not in ("success", "neutral", "skipped")
    if candidate_failed != previous_failed:
        return candidate_failed
    return True


@dataclass
class PolicyEvaluation:
    violations: list = field(default_factory=list)       # hard blockers
    warnings: list = field(default_factory=list)          # soft findings
    mandatory_checks_missing: list = field(default_factory=list)  # kept for backward compatibility: failed + pending
    mandatory_checks_failed: list = field(default_factory=list)   # completed, did not succeed -> hard violation
    mandatory_checks_pending: list = field(default_factory=list)  # not yet completed -> wait, don't block
    blocked_paths_touched: list = field(default_factory=list)
    requires_human_review: bool = False
    policy_version: int = 1
    policy_name: str = "default"


def load_default_policy() -> dict:
    path = os.path.join(POLICY_DIR, "default_policy.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_repo_policy(repo_full_name: str) -> dict:
    """Repo-specific override file, falling back to the default policy."""
    safe_name = repo_full_name.replace("/", "__")
    repo_path = os.path.join(POLICY_DIR, f"{safe_name}.json")
    if os.path.exists(repo_path):
        with open(repo_path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    return load_default_policy()


def evaluate_policy(policy: dict, changed_files: list[dict], ci_checks: list[dict] | None,
                     author_login: str) -> PolicyEvaluation:
    ev = PolicyEvaluation(policy_version=policy.get("version", 1), policy_name=policy.get("name", "default"))

    # 1. Blocked paths
    blocked_globs = policy.get("blocked_paths", [])
    for f in changed_files:
        for pattern in blocked_globs:
            if fnmatch.fnmatch(f["file_path"], pattern):
                ev.blocked_paths_touched.append(f["file_path"])
                ev.violations.append(f"Blocked path touched: {f['file_path']} (matches '{pattern}')")
                break

    # 2. Mandatory CI checks -- distinguish three states per mandatory check name:
    #    SUCCEEDED   : status == completed, conclusion == success/neutral/skipped
    #    FAILED      : status == completed, conclusion is anything else      -> hard violation (BLOCKED)
    #    PENDING     : no matching check yet, or status != completed         -> wait for CI (not a violation)
    mandatory = _iter_mandatory_check_names(policy)
    checks_by_name = {}
    for c in (ci_checks or []):
        name = c.get("name")
        if not name:
            continue
        matched_required = None
        for required_name in mandatory:
            if _check_matches_required_name(policy, required_name, name):
                matched_required = required_name
                break
        if matched_required is None:
            continue

        previous = checks_by_name.get(matched_required)
        if previous is None or _prefer_check(c, previous):
            checks_by_name[matched_required] = c

    succeeded, failed, pending = set(), set(), set()
    for name in mandatory:
        check = checks_by_name.get(name)
        if check is None:
            pending.add(name)
        elif check.get("status") != "completed":
            pending.add(name)
        elif check.get("conclusion") in ("success", "neutral", "skipped"):
            succeeded.add(name)
        else:
            failed.add(name)

    if failed:
        ev.mandatory_checks_failed = sorted(failed)
        ev.violations.append(f"Mandatory CI checks failed: {', '.join(sorted(failed))}")
    if pending:
        ev.mandatory_checks_pending = sorted(pending)
    ev.mandatory_checks_missing = sorted(failed | pending)  # backward-compatible combined view

    # 3. Human-review-required file patterns (soft: forces manual review, not a blocker finding)
    human_review_globs = policy.get("human_review_required_paths", [])
    for f in changed_files:
        for pattern in human_review_globs:
            if fnmatch.fnmatch(f["file_path"], pattern):
                ev.requires_human_review = True
                ev.warnings.append(f"Path requires human review: {f['file_path']}")
                break

    # 4. Max files / max lines guardrail
    max_files = policy.get("max_files_for_auto_approval")
    if max_files is not None and len(changed_files) > max_files:
        ev.requires_human_review = True
        ev.warnings.append(f"PR touches {len(changed_files)} files, exceeding auto-approval limit of {max_files}.")

    # 5. Disallowed authors / bot restriction
    disallowed_authors = set(policy.get("disallowed_authors", []))
    if author_login in disallowed_authors:
        ev.violations.append(f"Author '{author_login}' is explicitly disallowed from automatic approval.")

    return ev
