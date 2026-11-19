"""
Formats SecurePR's independent AI analysis and current CI state as Markdown.
"""
from datetime import datetime
from urllib.parse import quote, urlsplit

DECISION_META = {
    "APPROVED":       {"emoji": "✅", "label": "Approved",       "color": "🟢"},
    "BLOCKED":        {"emoji": "⛔", "label": "Blocked",        "color": "🔴"},
    "MANUAL_REVIEW":  {"emoji": "🟡", "label": "Manual Review Required", "color": "🟡"},
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


def format_comment(*, decision: str, reasons: list[str], overall_score: float, category: str,
                    dimension_scores: dict, findings: list[dict], changed_files: list[dict],
                    circuit_breaker_tripped: bool, circuit_breaker_reason: str | None,
                    impact_items: list, policy_name: str, policy_version: int,
                    dashboard_url: str, ci_checks: list[dict] | None = None,
                    analysis_error: str | None = None,
                    ci_state: str | None = None) -> str:
    meta = DECISION_META.get(decision, {"emoji": "", "label": decision})
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ---------------------------------------------------------------- header
    lines = [
        f"## {meta['emoji']} SecurePR Review — **{meta['label']}**",
        "",
        "| | |",
        "|---|---|",
        "| **AI Analysis** | Completed |",
        f"| **Overall Risk Score** | `{overall_score}/100` ({RISK_LABEL.get(category, category)}) |",
        f"| **CI / Check Status** | {_ci_summary_line(ci_checks, ci_state)} |",
        "| **Review Result** | Published |",
        f"| **Circuit Breaker** | {'🔴 Active — ' + (circuit_breaker_reason or '') if circuit_breaker_tripped else '🟢 Inactive'} |",
        f"| **Reviewed** | {now} |",
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
        lines.append("| Check | Provider | Result | Details |")
        lines.append("|---|---|---|---|")
        for c in ci_checks:
            name = _markdown_code(c.get("name") or "Unnamed check")
            provider = _markdown_code(c.get("provider") or c.get("kind") or "GitHub")
            url = c.get("url") or c.get("html_url")
            details = _markdown_link(url)
            lines.append(f"| {name} | {provider} | {_ci_icon(c)} | {details} |")
        lines.append("\n</details>\n")

    # ---------------------------------------------------------------- findings
    if findings:
        lines.append(f"<details open>\n<summary><b>🔎 Findings ({len(findings)})</b></summary>\n")
        for f in findings[:20]:
            sev = SEVERITY_META.get(
                f.get("severity") or "",
                {"emoji": "⚪", "label": f.get("severity") or "Info"},
            )
            location = " ".join(
                value for value in (f.get("file_path"), f.get("line_reference")) if value
            )
            loc = f" — `{location}`" if location else ""
            action = RECOMMENDED_ACTION.get(f.get("dimension") or "")
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
    lines.append("_Auto-merge is disabled by default and requires every discovered CI execution to pass._")
    return "\n".join(lines)


def format_review_status_comment(
    *,
    pr_number: int,
    head_sha: str,
    ci_state: str,
    ci_checks: list[dict],
    ci_error: str | None,
    review,
    findings: list[dict],
    marker: str,
) -> str:
    """Render the single durable waiting comment for a PR revision."""
    lines = [
        "## SecurePR Review — Waiting for CI",
        "",
        f"SecurePR has received PR #{pr_number} at revision `{head_sha}`.",
        "AI analysis and CI verification are being processed.",
        "Final results will be published when the review is ready.",
        "",
        f"- **CI status:** {ci_state.replace('_', ' ').title()}",
    ]
    if ci_error:
        lines.append(f"- **CI discovery error:** {ci_error}")
    if ci_checks:
        lines.extend([
            "",
            "| Check | Provider | Result | Details |",
            "|---|---|---|---|",
        ])
        for check in ci_checks[:100]:
            lines.append(
                f"| {_markdown_code(check.get('name') or check.get('context') or 'Unnamed check')} "
                f"| {_markdown_code(check.get('provider') or 'Unknown provider')} "
                f"| {_ci_icon(check)} "
                f"| {_markdown_link(check.get('url') or check.get('html_url'))} |"
            )
        if len(ci_checks) > 100:
            lines.append(f"\n_{len(ci_checks) - 100} additional checks are summarized in the counts above._")
    elif ci_state == "NOT_CONFIGURED":
        lines.append("No executable checks were discovered for this revision.")
    elif ci_state in {"UNKNOWN", "UNAVAILABLE"}:
        lines.append("Executable CI checks have not been confirmed yet.")

    if ci_state == "UNKNOWN":
        lines.append("CI checks are being discovered; SecurePR will monitor actual Check Runs and Commit Statuses as they appear.")
    elif ci_state == "IN_PROGRESS":
        lines.append("CI checks are being monitored; their current actual states are listed above.")
    elif ci_state == "UNAVAILABLE":
        lines.append("GitHub CI discovery is unavailable; SecurePR will retry and will not report CI as passing.")
    lines.extend(["", marker])
    return "\n".join(lines)


def format_auto_merge_comment(*, status: str, overall_score: float, reason: str,
                              merge_commit_sha: str | None = None) -> str:
    """Short, explicit follow-up after the final review/merge outcome."""
    if status == "MERGED":
        commit = f"\n\nMerge commit: `{merge_commit_sha}`" if merge_commit_sha else ""
        return ("## ✅ SecurePR Review — **Approved & Automatically Merged**\n\n"
                f"Risk Score: `{overall_score}/100` (LOW)\n\n"
                "CI Status: All discovered checks passed  \nCircuit Breaker: Inactive  \nPolicy: Passed\n\n"
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
    if check.get("status") in {"queued", "requested", "waiting", "pending", "in_progress"}:
        return "⏳ In progress"
    if check.get("status") != "completed":
        return "❔ Unknown status"
    conclusion = check.get("conclusion")
    return {"success": "✅ Passed", "failure": "❌ Failed", "neutral": "➖ Neutral",
            "skipped": "⏭️ Skipped", "cancelled": "🚫 Cancelled",
            "timed_out": "⌛ Timed out", "action_required": "⚠️ Action required",
            "startup_failure": "❌ Startup failure"            }.get(conclusion or "", f"❔ {conclusion}")


def _markdown_code(value) -> str:
    safe = str(value).replace("\\", "\\\\").replace("`", "\\`").replace("|", "\\|")
    return f"`{safe.replace(chr(10), ' ').replace(chr(13), ' ')}`"


def _markdown_link(url) -> str:
    if not isinstance(url, str):
        return "—"
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "—"
    if parsed.scheme != "https" or not parsed.netloc:
        return "—"
    safe_url = quote(url, safe=":/?#@!$&'*,;=%")
    return f"[View]({safe_url})"


def _ci_summary_line(ci_checks: list[dict] | None, ci_state: str | None = None) -> str:
    state_labels = {
        "IN_PROGRESS": "⏳ In progress — AI review published; CI monitoring continues",
        "PASSED": "✅ All discovered checks passed",
        "FAILED": "❌ At least one discovered check failed",
        "NON_SUCCESS": "⚠️ One or more checks ended without success",
        "NOT_CONFIGURED": "⚪ No checks discovered",
        "NO_CHECKS_FOUND": "⚪ No checks discovered",
        "UNAVAILABLE": "⚠️ CI discovery unavailable",
        "UNKNOWN": "❔ CI status unknown",
    }
    if ci_state and ci_state in state_labels:
        return state_labels[ci_state]
    if not ci_checks:
        return "❔ CI status unknown"
    completed = [c for c in ci_checks if c.get("status") == "completed"]
    passed = [c for c in completed if c.get("conclusion") == "success"]
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
