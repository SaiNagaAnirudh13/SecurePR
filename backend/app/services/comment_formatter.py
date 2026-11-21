"""
Formats SecurePR's independent AI analysis and current CI state as Markdown.
"""

from datetime import datetime
from urllib.parse import quote, urlsplit

DECISION_META = {
    "APPROVED": {"emoji": "✅", "label": "Approved", "color": "🟢"},
    "BLOCKED": {"emoji": "⛔", "label": "Blocked", "color": "🔴"},
    "MANUAL_REVIEW": {"emoji": "🟡", "label": "Manual Review Required", "color": "🟡"},
    "REVIEW_UNAVAILABLE": {
        "emoji": "⚠️",
        "label": "AI Review Not Completed",
        "color": "🟡",
    },
    "WAITING_FOR_AI": {
        "emoji": "⏳",
        "label": "Waiting for AI",
        "color": "⚪",
    },
    "PENDING": {"emoji": "⏳", "label": "Pending", "color": "⚪"},
}

RISK_LABEL = {
    "LOW": "🟢 Low",
    "MEDIUM": "🟡 Medium",
    "HIGH": "🔴 High",
    "NOT_EVALUATED": "⚪ Not evaluated",
}

SEVERITY_META = {
    "CRITICAL": {"emoji": "🔴", "label": "Critical"},
    "HIGH": {"emoji": "🟠", "label": "High"},
    "MEDIUM": {"emoji": "🟡", "label": "Medium"},
    "LOW": {"emoji": "🔵", "label": "Low"},
}

DIMENSION_LABELS = {
    "change_surface": "Change Surface",
    "reversibility": "Reversibility",
    "data_security": "Data Security",
    "operational": "Operational Impact",
    "verification": "Verification",
    "policy_compliance": "Policy Compliance",
}

RECOMMENDED_ACTION = {
    "policy_compliance": "Remove or revert the change to the flagged path, or request a policy exception.",
    "verification": "Add/adjust tests for the changed code, or wait for CI to finish and re-check.",
    "data_security": "Have a security-focused reviewer double check this change before merging.",
    "reversibility": "Confirm a rollback plan exists before merging (e.g. a down-migration or feature flag).",
    "operational": "Confirm the deployment/infra change has been reviewed by whoever owns that system.",
    "change_surface": "Consider splitting this into smaller, independently reviewable PRs.",
}

AI_FAILURE_REASONS = {
    "GROQ_CONFIGURATION_MISSING": "AI provider configuration is missing.",
    "GROQ_MODEL_CONFIGURATION_INVALID": "AI model configuration is invalid.",
    "GROQ_AUTHENTICATION_FAILED": "Groq authentication failed; provider configuration must be checked.",
    "GROQ_QUOTA_EXCEEDED": "Groq API quota or rate limit was exceeded.",
    "GROQ_PROVIDER_UNAVAILABLE": "Groq is temporarily unavailable.",
    "GROQ_REQUEST_TIMEOUT": "Groq analysis timed out.",
    "GROQ_NETWORK_ERROR": "Groq could not be reached because of a network error.",
    "GROQ_INVALID_RESPONSE": "Groq returned an invalid or incomplete response.",
    "PR_FILES_UNAVAILABLE": "The changed files could not be retrieved for AI analysis.",
}


def _ai_failure_reason(error_code: str | None) -> str:
    if not isinstance(error_code, str):
        return "Groq analysis could not be completed."
    return AI_FAILURE_REASONS.get(error_code, "Groq analysis could not be completed.")


def _score_bar(score: float) -> str:
    filled = max(0, min(10, round(score / 10)))
    return "█" * filled + "░" * (10 - filled)


def format_comment(
    *,
    decision: str,
    reasons: list[str],
    overall_score: float,
    category: str,
    dimension_scores: dict,
    findings: list[dict],
    changed_files: list[dict],
    circuit_breaker_tripped: bool,
    circuit_breaker_reason: str | None,
    impact_items: list,
    policy_name: str,
    policy_version: int,
    dashboard_url: str,
    ci_checks: list[dict] | None = None,
    analysis_error: str | None = None,
    ci_state: str | None = None,
    ai_state: str | None = None,
    ai_error_code: str | None = None,
    ai_retryable_at: datetime | None = None,
) -> str:
    if decision == "REVIEW_UNAVAILABLE":
        return format_ai_unavailable_comment(
            ai_state=ai_state or "AI_FAILED",
            ai_error_code=ai_error_code,
            ai_retryable_at=ai_retryable_at,
            ci_checks=ci_checks,
            ci_state=ci_state,
        )

    meta = DECISION_META.get(decision, {"emoji": "", "label": decision})
    if decision == "BLOCKED" and ci_state == "FAILED":
        meta = {"emoji": "⛔", "label": "BLOCKED: CI Failed", "color": "🔴"}
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    # ---------------------------------------------------------------- header
    lines = [
        f"## {meta['emoji']} SecurePR Review — **{meta['label']}**",
        "",
        "| | |",
        "|---|---|",
        f"| **AI Analysis** | {'Incomplete' if analysis_error else 'Completed'} |",
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
        lines.append(
            f"| {DIMENSION_LABELS.get(dim, dim.replace('_', ' ').title())} | `{score}/100` | `{_score_bar(score)}` |"
        )
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
        lines.append(
            f"<details open>\n<summary><b>🔎 Findings ({len(findings)})</b></summary>\n"
        )
        for f in findings[:20]:
            sev = SEVERITY_META.get(
                f.get("severity") or "",
                {"emoji": "⚪", "label": f.get("severity") or "Info"},
            )
            location = " ".join(
                value
                for value in (f.get("file_path"), f.get("line_reference"))
                if value
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
        lines.append(
            f"<details>\n<summary><b>📁 Changed Files ({len(changed_files)} files, "
            f"+{additions}/-{deletions})</b></summary>\n"
        )
        for f in changed_files[:30]:
            flag = " 🚫 **blocked path**" if f.get("is_blocked_path") else ""
            lines.append(
                f"- `{f['file_path']}` (+{f.get('additions', 0)}/-{f.get('deletions', 0)}){flag}"
            )
        if len(changed_files) > 30:
            lines.append(
                f"- _…and {len(changed_files) - 30} more file(s), see the dashboard for the full list._"
            )
        lines.append("\n</details>\n")

    # ---------------------------------------------------------- impact simulator
    if impact_items:
        confirmed = [i for i in impact_items if _get(i, "confidence") == "CONFIRMED"]
        lines.append(
            f"<details>\n<summary><b>🧬 Change Impact Simulator</b> "
            f"({len(confirmed)} confirmed, {len(impact_items)} total)</summary>\n"
        )
        lines.append("| Component | Type | Confidence | Potential Consequence |")
        lines.append("|---|---|---|---|")
        for i in impact_items[:20]:
            name, ctype, conf, cons = (
                _get(i, "component_name"),
                _get(i, "component_type"),
                _get(i, "confidence"),
                _get(i, "potential_consequence"),
            )
            conf_icon = {"CONFIRMED": "🟢", "INFERRED": "🟡", "UNRESOLVED": "⚪"}.get(
                conf, ""
            )
            lines.append(f"| `{name}` | {ctype} | {conf_icon} {conf} | {cons} |")
        if len(impact_items) > 20:
            lines.append(
                f"\n_…and {len(impact_items) - 20} more, see the dashboard for the full list._"
            )
        lines.append("\n</details>\n")

    # --------------------------------------------------------- decision reasons
    lines.append("### 🧭 Why this decision")
    for r in reasons:
        lines.append(f"- {r}")
    lines.append("")
    lines.append(
        f"<sub>Policy: **{policy_name} v{policy_version}** · "
        f"[Open full analysis & evidence in the SecurePR dashboard]({dashboard_url})</sub>"
    )
    lines.append("")
    lines.append(
        "_Auto-merge is opt-in and requests GitHub's protected auto-merge only "
        "after all applicable CI checks pass._"
    )
    return "\n".join(lines)


def format_ai_unavailable_comment(
    *,
    ai_state: str,
    ai_error_code: str | None,
    ai_retryable_at: datetime | None,
    ci_checks: list[dict] | None,
    ci_state: str | None,
) -> str:
    reason = _ai_failure_reason(ai_error_code)
    lines = [
        "## SecurePR AI Review — **NOT COMPLETED**",
        "",
        "SecurePR received this pull request, but the AI code review could not be completed.",
        "",
        f"**AI Review Status:** Not Completed (`{ai_state}`)  ",
        "**Review Decision:** UNAVAILABLE  ",
        "**Risk Score:** Not Evaluated  ",
        f"**CI Status:** {_ci_summary_line(ci_checks, ci_state)}",
        "",
        f"**Reason:** {reason}",
        "",
        "**No AI approval decision has been made.**",
        "The PR requires a successful AI analysis before SecurePR can issue its normal final review decision.",
    ]
    if ai_retryable_at:
        lines.extend(
            [
                "",
                f"**Retryable After:** {ai_retryable_at.strftime('%Y-%m-%d %H:%M UTC')}",
            ]
        )
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
    """Render the single durable status comment for a PR revision."""
    is_final = bool(
        review and review.decision.value not in {"WAITING_FOR_CI", "PENDING"}
    )
    if not is_final:
        title = "SecurePR Review — Waiting for CI"
    elif review.decision.value == "REVIEW_UNAVAILABLE":
        title = "SecurePR AI Review — NOT COMPLETED"
    elif review.decision.value == "BLOCKED" and ci_state == "FAILED":
        title = "SecurePR Review — BLOCKED: CI Failed"
    else:
        title = f"SecurePR Review — {review.decision.value}"
    lines = [
        f"## {title}",
        "",
        f"SecurePR has received PR #{pr_number} at revision `{head_sha}`.",
        (
            "AI review was not completed; no AI approval decision has been made."
            if is_final and review.decision.value == "REVIEW_UNAVAILABLE"
            else f"Authoritative decision: **{review.decision.value}** "
            f"(risk {review.overall_score}/100, {review.risk_category})."
            if is_final
            else "AI analysis and CI verification are being processed; no approval decision is final."
        ),
        "",
        f"- **CI status:** {ci_state.replace('_', ' ').title()}",
    ]
    if is_final:
        if review.decision.value == "REVIEW_UNAVAILABLE":
            lines.extend(
                [
                    "- **AI Review Status:** Not Completed",
                    "- **Review Decision:** UNAVAILABLE",
                    "- **Risk Score:** Not Evaluated",
                    f"- **Reason:** {_ai_failure_reason(getattr(review, 'ai_error_code', None))}",
                ]
            )
            retryable_at = getattr(review, "ai_retryable_at", None)
            if retryable_at:
                lines.append(
                    f"- **Retryable After:** {retryable_at.strftime('%Y-%m-%d %H:%M UTC')}"
                )
        else:
            lines.extend(
                f"- **Decision reason:** {reason}"
                for reason in (review.decision_reasons or [])
            )
    if ci_error:
        lines.append(f"- **CI discovery error:** {ci_error}")
    if ci_checks:
        lines.extend(
            [
                "",
                "| Check | Provider | Result | Details |",
                "|---|---|---|---|",
            ]
        )
        for check in ci_checks[:100]:
            lines.append(
                f"| {_markdown_code(check.get('name') or check.get('context') or 'Unnamed check')} "
                f"| {_markdown_code(check.get('provider') or 'Unknown provider')} "
                f"| {_ci_icon(check)} "
                f"| {_markdown_link(check.get('url') or check.get('html_url'))} |"
            )
        if len(ci_checks) > 100:
            lines.append(
                f"\n_{len(ci_checks) - 100} additional checks are summarized in the counts above._"
            )
    elif ci_state == "NOT_CONFIGURED":
        lines.append("No executable checks were discovered for this revision.")
    elif ci_state in {"UNKNOWN", "UNAVAILABLE"}:
        lines.append("Executable CI checks have not been confirmed yet.")

    if is_final and review.decision.value == "BLOCKED" and ci_state == "FAILED":
        lines.append(
            "Approval and auto-merge are prohibited because one or more CI checks failed."
        )
    elif ci_state == "UNKNOWN":
        lines.append(
            "CI checks are being discovered; SecurePR will monitor actual Check Runs and Commit Statuses as they appear."
        )
    elif ci_state == "IN_PROGRESS":
        lines.append(
            "CI checks are being monitored; their current actual states are listed above."
        )
    elif ci_state == "UNAVAILABLE":
        lines.append(
            "GitHub CI discovery is unavailable; SecurePR will retry and will not report CI as passing."
        )
    lines.extend(["", marker])
    return "\n".join(lines)


def format_auto_merge_comment(
    *,
    status: str,
    overall_score: float,
    reason: str,
    merge_commit_sha: str | None = None,
) -> str:
    """Short, explicit follow-up after the final review/merge outcome."""
    if status == "MERGED":
        commit = f"\n\nMerge commit: `{merge_commit_sha}`" if merge_commit_sha else ""
        return (
            "## ✅ SecurePR Review — **Approved & Automatically Merged**\n\n"
            f"Risk Score: `{overall_score}/100` (LOW)\n\n"
            "CI Status: All discovered checks passed  \nCircuit Breaker: Inactive  \nPolicy: Passed\n\n"
            "🤖 **Auto-Merge:** This Pull Request satisfied all SecurePR safety requirements and was automatically merged."
            f"{commit}"
        )
    headings = {
        "DISABLED_DASHBOARD_CONFIGURATION": "Auto-approval disabled in the saved dashboard configuration",
        "DASHBOARD_CONFIGURATION_MISSING": "Saved dashboard configuration required",
        "NOT_ELIGIBLE": "Saved dashboard configuration or review requirements not met",
        "GITHUB_AUTO_MERGE_DISABLED": "GitHub repository auto-merge is disabled",
        "PERMISSION_MISSING": "GitHub App permission missing",
        "CI_PENDING": "Waiting for required CI",
        "CI_FAILED": "Auto-merge blocked by failed CI",
        "BRANCH_PROTECTION_UNAVAILABLE": "Required branch protection could not be verified",
        "REQUIRED_REVIEW_PENDING": "Waiting for required GitHub review",
        "AUTO_MERGE_ENABLED": "GitHub auto-merge enabled",
        "CANCELLATION_PENDING": "GitHub auto-merge cancellation pending verification",
        "CANCELLATION_UNVERIFIED": "Stale auto-merge cancellation could not be verified",
        "MERGED": "Pull request merged",
    }
    heading = headings.get(status, "Auto-merge unavailable")
    return (
        f"## SecurePR Auto-Merge — **{heading}**\n\n"
        f"SecurePR approval risk: `{overall_score}/100`.\n\n"
        f"**Status:** `{status}`  \n**Reason:** {reason}"
    )


def _get(obj, attr):
    return getattr(obj, attr) if hasattr(obj, attr) else obj.get(attr)


def _ci_icon(check: dict) -> str:
    if check.get("status") in {
        "queued",
        "requested",
        "waiting",
        "pending",
        "in_progress",
    }:
        return "⏳ In progress"
    if check.get("status") != "completed":
        return "❔ Unknown status"
    conclusion = check.get("conclusion")
    return {
        "success": "✅ Passed",
        "failure": "❌ Failed",
        "neutral": "➖ Neutral",
        "skipped": "⏭️ Skipped",
        "cancelled": "🚫 Cancelled",
        "timed_out": "⌛ Timed out",
        "action_required": "⚠️ Action required",
        "startup_failure": "❌ Startup failure",
    }.get(conclusion or "", f"❔ {conclusion}")


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
        "IN_PROGRESS": "⏳ In progress — approval is waiting for CI",
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
