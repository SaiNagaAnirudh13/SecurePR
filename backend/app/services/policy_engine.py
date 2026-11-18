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
from dataclasses import dataclass, field

POLICY_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "policies")


@dataclass
class PolicyEvaluation:
    violations: list = field(default_factory=list)       # hard blockers
    warnings: list = field(default_factory=list)          # soft findings
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

    # 2. Human-review-required file patterns (soft: forces manual review, not a blocker finding)
    human_review_globs = policy.get("human_review_required_paths", [])
    for f in changed_files:
        for pattern in human_review_globs:
            if fnmatch.fnmatch(f["file_path"], pattern):
                ev.requires_human_review = True
                ev.warnings.append(f"Path requires human review: {f['file_path']}")
                break

    # 3. Max files / max lines guardrail
    max_files = policy.get("max_files_for_auto_approval")
    if max_files is not None and len(changed_files) > max_files:
        ev.requires_human_review = True
        ev.warnings.append(f"PR touches {len(changed_files)} files, exceeding auto-approval limit of {max_files}.")

    # 4. Disallowed authors / bot restriction
    disallowed_authors = set(policy.get("disallowed_authors", []))
    if author_login in disallowed_authors:
        ev.violations.append(f"Author '{author_login}' is explicitly disallowed from automatic approval.")

    return ev
