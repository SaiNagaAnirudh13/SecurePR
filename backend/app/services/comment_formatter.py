"""
Formats SecurePR's analysis into polished, GitHub-friendly Markdown.

Two comment shapes are produced:
  - format_waiting_comment(): a short placeholder posted the moment SecurePR
    sees a PR but is still waiting on mandatory CI checks to finish.
  - format_comment(): the full report, posted once a final decision
    (APPROVED / BLOCKED / MANUAL_REVIEW) has been reached.
"""
from datetime import datetime

DECISION_META = {
    "APPROVED":       {"emoji": "✅", "label": "Approved",       "color": "🟢"},
    "BLOCKED":        {"emoji": "⛔", "label": "Blocked",        "color": "🔴"},
    "MANUAL_REVIEW":  {"emoji": "🟡", "label": "Manual Review Required", "color": "🟡"},
    "WAITING_FOR_CI":  {"emoji": "⏳", "label": "Waiting for CI",  "color": "⚪"},
    "PENDING":        {"emoji": "⏳", "label": "Pending",         "color": "⚪"},
}

RISK_LABEL = {"LOW": "🟢 Low", "MEDIUM": "🟡 Medium", "HIGH": "🔴 High", "NOT_EVALUATED": "⚪ Not evaluated"}

SEVERITY_META = {
    "CRITICAL": {"emoji": "🔴", "label": "Critical"},
    "HIGH":     {"emoji": "🟠", "label": "High"},
    "MEDIUM":   {"emoji": "🟡", "label": "Medium"},
    "LOW":      {"emoji": "🔵", "label": "Low"},
}

DIMENSION_LABELS = {
    "change_surface": "Change Surface", "reversibility": "Reversibility",
    "data_security": "Data Security", "operational": "Operational Impact",
    "verification": "Verification", "policy_compliance": "Policy Compliance",
}

RECOMMENDED_ACTION = {
    "policy_compliance": "Remove or revert the change to the flagged path, or request a policy exception.",
    "verification": "Add/adjust tests for the changed code, or wait for CI to finish and re-check.",
    "data_security": "Have a security-focused reviewer double check this change before merging.",
    "reversibility": "Confirm a rollback plan exists before merging (e.g. a down-migration or feature flag).",
    "operational": "Confirm the deployment/infra change has been reviewed by whoever owns that system.",
    "change_surface": "Consider splitting this into smaller, independently reviewable PRs.",
}


def _score_bar(score: float) -> str:
    filled = max(0, min(10, round(score / 10)))
    return "█" * filled + "░" * (10 - filled)


def format_waiting_comment(*, pending_checks: list[str], changed_files_count: int | None,
                            policy_name: str, policy_version: int, dashboard_url: str,
                            changed_files_unavailable: bool = False) -> str:
    checks_md = "\n".join(f"- ⏳ `{c}` — not yet completed" for c in pending_checks) or "- (no mandatory checks configured)"
    files_summary = (
        "SecurePR could not retrieve the changed-file list, so no file count is shown."
        if changed_files_unavailable
        else f"SecurePR found {changed_files_count or 0} changed file(s) in this Pull Request."
    )
    return (
        f"## ⏳ SecurePR Review — **Waiting for CI**\n\n"
        f"{files_summary} SecurePR is holding its final decision until the following "
        f"**mandatory** check(s) report a result:\n\n"
        f"{checks_md}\n\n"
        f"No action is needed — SecurePR is watching this commit and will post the final review "
        f"as soon as all mandatory checks finish. If checks remain pending, SecurePR will "
        f"route the review to manual review after its fallback timeout.\n\n"
        f"<sub>Policy: **{policy_name} v{policy_version}** · "
        f"[Open in SecurePR dashboard]({dashboard_url}) · "
        f"Auto-merge, if enabled, is considered only after final analysis.</sub>"
    )


def format_comment(*, decision: str, reasons: list[str], overall_score: float, category: str,
                    dimension_scores: dict, findings: list[dict], changed_files: list[dict],
                    circuit_breaker_tripped: bool, circuit_breaker_reason: str | None,
                    impact_items: list, policy_name: str, policy_version: int,
                    dashboard_url: str, ci_checks: list[dict] | None = None,
                    waited_for_ci: bool = False, analysis_error: str | None = None) -> str:
    meta = DECISION_META.get(decision, {"emoji": "", "label": decision})
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ---------------------------------------------------------------- header
    lines = [
        f"## {meta['emoji']} SecurePR Review — **{meta['label']}**",
        "",
        "| | |",
        "|---|---|",
        f"| **Overall Risk Score** | `{overall_score}/100` ({RISK_LABEL.get(category, category)}) |",
        f"| **CI / Check Status** | {_ci_summary_line(ci_checks)} |",
        f"| **Circuit Breaker** | {'🔴 Active — ' + (circuit_breaker_reason or '') if circuit_breaker_tripped else '🟢 Inactive'} |",
        f"| **Reviewed** | {now}{' (after waiting for CI to complete)' if waited_for_ci else ''} |",
        "",
    ]
    if analysis_error:
        lines.extend([f"> **Analysis incomplete:** {analysis_error}", ""])

    # ------------------------------------------------------------- risk grid
    lines.append("<details open>\n<summary><b>📊 Risk Breakdown</b></summary>\n")
    lines.append("| Dimension | Score | |")
    lines.append("|---|---|---|")
    for dim, s in dimension_scores.items():
        score = s.score if hasattr(s, "score") else s
        lines.append(f"| {DIMENSION_LABELS.get(dim, dim.replace('_', ' ').title())} | `{score}/100` | `{_score_bar(score)}` |")
    lines.append("\n</details>\n")

    # --------------------------------------------------------------- CI table
    if ci_checks:
        lines.append("<details>\n<summary><b>✅ CI Verification</b></summary>\n")
        lines.append("| Check | Result |")
        lines.append("|---|---|")
        for c in ci_checks:
            lines.append(f"| `{c.get('name')}` | {_ci_icon(c)} |")
        lines.append("\n</details>\n")

    # ---------------------------------------------------------------- findings
    if findings:
        lines.append(f"<details open>\n<summary><b>🔎 Findings ({len(findings)})</b></summary>\n")
        for f in findings[:20]:
            sev = SEVERITY_META.get(f.get("severity"), {"emoji": "⚪", "label": f.get("severity") or "Info"})
            loc = f" — `{f.get('file_path')}`" if f.get("file_path") else ""
            action = RECOMMENDED_ACTION.get(f.get("dimension"))
            lines.append(f"**{sev['emoji']} {sev['label']} — {f.get('title')}**{loc}")
            lines.append(f"> {f.get('description')}")
            if action:
                lines.append(f"> *Recommended action:* {action}")
            lines.append("")
        lines.append("</details>\n")

    # ------------------------------------------------------------ changed files
    if changed_files:
        additions = sum(f.get("additions", 0) for f in changed_files)
        deletions = sum(f.get("deletions", 0) for f in changed_files)
        lines.append(f"<details>\n<summary><b>📁 Changed Files ({len(changed_files)} files, "
                      f"+{additions}/-{deletions})</b></summary>\n")
        for f in changed_files[:30]:
            flag = " 🚫 **blocked path**" if f.get("is_blocked_path") else ""
            lines.append(f"- `{f['file_path']}` (+{f.get('additions', 0)}/-{f.get('deletions', 0)}){flag}")
        if len(changed_files) > 30:
            lines.append(f"- _…and {len(changed_files) - 30} more file(s), see the dashboard for the full list._")
        lines.append("\n</details>\n")

    # ---------------------------------------------------------- impact simulator
    if impact_items:
        confirmed = [i for i in impact_items if _get(i, "confidence") == "CONFIRMED"]
        lines.append(f"<details>\n<summary><b>🧬 Change Impact Simulator</b> "
                      f"({len(confirmed)} confirmed, {len(impact_items)} total)</summary>\n")
        lines.append("| Component | Type | Confidence | Potential Consequence |")
        lines.append("|---|---|---|---|")
        for i in impact_items[:20]:
            name, ctype, conf, cons = (_get(i, "component_name"), _get(i, "component_type"),
                                        _get(i, "confidence"), _get(i, "potential_consequence"))
            conf_icon = {"CONFIRMED": "🟢", "INFERRED": "🟡", "UNRESOLVED": "⚪"}.get(conf, "")
            lines.append(f"| `{name}` | {ctype} | {conf_icon} {conf} | {cons} |")
        if len(impact_items) > 20:
            lines.append(f"\n_…and {len(impact_items) - 20} more, see the dashboard for the full list._")
        lines.append("\n</details>\n")

    # --------------------------------------------------------- decision reasons
    lines.append("### 🧭 Why this decision")
    for r in reasons:
        lines.append(f"- {r}")
    lines.append("")
    lines.append(f"<sub>Policy: **{policy_name} v{policy_version}** · "
                  f"[Open full analysis & evidence in the SecurePR dashboard]({dashboard_url})</sub>")
    lines.append("")
    lines.append("_Auto-merge is disabled by default and requires every configured safety check to pass._")
    return "\n".join(lines)


def format_auto_merge_comment(*, status: str, overall_score: float, reason: str,
                              merge_commit_sha: str | None = None) -> str:
    """Short, explicit follow-up after the final review/merge outcome."""
    if status == "MERGED":
        commit = f"\n\nMerge commit: `{merge_commit_sha}`" if merge_commit_sha else ""
        return ("## ✅ SecurePR Review — **Approved & Automatically Merged**\n\n"
                f"Risk Score: `{overall_score}/100` (LOW)\n\n"
                "CI Status: All mandatory checks passed  \nCircuit Breaker: Inactive  \nPolicy: Passed\n\n"
                "🤖 **Auto-Merge:** This Pull Request satisfied all SecurePR safety requirements and was automatically merged."
                f"{commit}")
    if status in ("DISABLED_GLOBAL", "DISABLED_REPOSITORY"):
        return ("## ✅ SecurePR Review — **Approved**\n\n"
                "This Pull Request was approved, but automatic merging is disabled by configuration.\n\n"
                f"Reason: {reason}")
    return ("## ⚠️ SecurePR Review — **Approved, Manual Merge Required**\n\n"
            "The PR passed SecurePR analysis, but automatic merging could not be completed.\n\n"
            f"Reason: {reason}")


def _get(obj, attr):
    return getattr(obj, attr) if hasattr(obj, attr) else obj.get(attr)


def _ci_icon(check: dict) -> str:
    if check.get("status") != "completed":
        return "⏳ Running"
    conclusion = check.get("conclusion")
    return {"success": "✅ Passed", "failure": "❌ Failed", "neutral": "➖ Neutral",
            "skipped": "⏭️ Skipped", "cancelled": "🚫 Cancelled",
            "timed_out": "⌛ Timed out", "action_required": "⚠️ Action required"}.get(conclusion, f"❔ {conclusion}")


def _ci_summary_line(ci_checks: list[dict] | None) -> str:
    if not ci_checks:
        return "⚪ No checks reported"
    completed = [c for c in ci_checks if c.get("status") == "completed"]
    passed = [c for c in completed if c.get("conclusion") in ("success", "neutral", "skipped")]
    failed = [c for c in completed if c not in passed]
    pending = [c for c in ci_checks if c.get("status") != "completed"]
    parts = []
    if passed:
        parts.append(f"✅ {len(passed)} passed")
    if failed:
        parts.append(f"❌ {len(failed)} failed")
    if pending:
        parts.append(f"⏳ {len(pending)} pending")
    return ", ".join(parts) if parts else "⚪ No checks reported"
