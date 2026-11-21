"""
Decision Engine.

This is THE deterministic gate. The AI model (Groq) contributes risk
scores and findings as inputs, but every approve/block/manual-review
decision is made here using explicit, auditable rules -- never "because the
AI said so". This module has no dependency on the Groq client.

Order of evaluation (first matching hard rule wins):
  1. AI analysis unavailable or incomplete                -> WAITING_FOR_AI / REVIEW_UNAVAILABLE
  2. CI not verified or not passed                       -> WAITING_FOR_CI
     (a discovered failure instead always returns BLOCKED)
  3. Any CRITICAL finding                                -> BLOCKED
     (a confirmed secret or destructive SQL statement in the diff is a
     reason to block outright -- there's no reason to make the contributor
     wait for CI to find that out, but CI still gates publication)
  4. Policy hard violations found from data we already
     have (blocked path, disallowed author, or a confirmed
     policy violation)                                      -> BLOCKED
  5. Circuit breaker tripped (global/repo/user)            -> MANUAL_REVIEW (never auto-block for this reason alone)
  6. Author not eligible for auto-approval                 -> MANUAL_REVIEW
  7. Overall risk score > author's configured threshold    -> MANUAL_REVIEW
  8. Policy soft warning (human_review_required_paths,
     max files exceeded)                                   -> MANUAL_REVIEW
  9. Otherwise                                              -> APPROVED
"""

from dataclasses import dataclass, field

from app.models.models import DecisionStatus


@dataclass
class DecisionInput:
    overall_score: float
    risk_category: str
    findings: (
        list  # list[dict] with at least {"severity": str, "is_mandatory_blocker": bool}
    )
    policy_violations: list
    policy_warnings: list
    requires_human_review_by_policy: bool
    circuit_breaker_tripped: bool
    circuit_breaker_reason: str | None
    author_eligible: bool
    author_risk_threshold: int | None
    analysis_complete: bool
    ai_state: str = "AI_COMPLETED"
    ci_state: str = "UNKNOWN"
    failed_checks: list = field(default_factory=list)


@dataclass
class Decision:
    status: DecisionStatus
    reasons: list = field(default_factory=list)


def decide(inp: DecisionInput) -> Decision:
    reasons: list[str] = []

    if inp.ai_state in {"AI_PENDING", "AI_RUNNING"}:
        return Decision(
            DecisionStatus.WAITING_FOR_AI,
            ["AI analysis has not completed; no review decision is available."],
        )
    if inp.ai_state != "AI_COMPLETED" or not inp.analysis_complete:
        return Decision(
            DecisionStatus.REVIEW_UNAVAILABLE,
            [
                "AI analysis could not be completed and validated. No AI approval decision has been made."
            ],
        )

    if inp.ci_state == "FAILED":
        reasons.append(
            "Approval is blocked because CI failed for the reviewed revision. "
            f"AI risk score {inp.overall_score}/100 ({inp.risk_category}) is informational only."
        )
        for check in inp.failed_checks:
            name = check.get("name") or check.get("context") or "Unnamed check"
            provider = check.get("provider") or "Unknown provider"
            conclusion = check.get("conclusion") or "failed"
            details = check.get("url") or check.get("html_url")
            detail_text = f" — {details}" if details else ""
            reasons.append(
                f"Failed check: {name} (provider: {provider}; {conclusion}){detail_text}"
            )
        return Decision(DecisionStatus.BLOCKED, reasons)

    if inp.ci_state != "PASSED":
        state = inp.ci_state.replace("_", " ").casefold()
        reasons.append(
            f"Approval is waiting for CI verification ({state}); "
            "missing or incomplete checks are never treated as successful."
        )
        return Decision(DecisionStatus.WAITING_FOR_CI, reasons)

    critical_findings = [
        f
        for f in inp.findings
        if f.get("severity") == "CRITICAL" or f.get("is_mandatory_blocker")
    ]
    if critical_findings:
        reasons.append(
            f"{len(critical_findings)} critical/mandatory-blocker finding(s) present."
        )
        return Decision(DecisionStatus.BLOCKED, reasons)

    if inp.policy_violations:
        reasons.extend(inp.policy_violations)
        return Decision(DecisionStatus.BLOCKED, reasons)

    if inp.circuit_breaker_tripped:
        reasons.append(
            f"Circuit breaker is active: {inp.circuit_breaker_reason}. "
            f"Automatic approval suspended; analysis and comments still published."
        )
        return Decision(DecisionStatus.MANUAL_REVIEW, reasons)

    if not inp.author_eligible:
        reasons.append(
            "Author is not currently configured as eligible for automatic approval."
        )
        return Decision(DecisionStatus.MANUAL_REVIEW, reasons)

    threshold = (
        inp.author_risk_threshold if inp.author_risk_threshold is not None else 39
    )
    if inp.overall_score > threshold:
        reasons.append(
            f"Overall risk score {inp.overall_score} exceeds author's auto-approval threshold ({threshold})."
        )
        return Decision(DecisionStatus.MANUAL_REVIEW, reasons)

    if inp.requires_human_review_by_policy:
        reasons.extend(
            inp.policy_warnings or ["Policy flags this change for human review."]
        )
        return Decision(DecisionStatus.MANUAL_REVIEW, reasons)

    reasons.append(
        f"Risk score {inp.overall_score} ({inp.risk_category}) within author's threshold "
        f"({threshold}); no policy violations, no critical findings, circuit breaker inactive."
    )
    return Decision(DecisionStatus.APPROVED, reasons)
