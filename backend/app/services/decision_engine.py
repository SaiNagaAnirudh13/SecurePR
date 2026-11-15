"""
Decision Engine.

This is THE deterministic gate. The AI model (Gemini) contributes risk
scores and findings as inputs, but every approve/block/manual-review
decision is made here using explicit, auditable rules -- never "because the
AI said so". This module has no dependency on the Gemini client.

Order of evaluation (first matching hard rule wins):
  1. Analysis incomplete / missing required data        -> MANUAL_REVIEW
  2. Any CRITICAL finding                                -> BLOCKED
     (a confirmed secret or destructive SQL statement in the diff is a
     reason to block outright -- there's no reason to make the contributor
     wait for CI to find that out)
  3. Policy hard violations found from data we already
     have (blocked path, disallowed author, a mandatory
     CI check that has COMPLETED and failed)              -> BLOCKED
  4. Mandatory CI checks that exist but have not yet
     completed (queued/in_progress)                       -> WAITING_FOR_CI
     (deferred, not a verdict -- the pipeline schedules a
     follow-up check and keeps this PR out of the approve/
     block path until GitHub reports a final result)
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
    findings: list  # list[dict] with at least {"severity": str, "is_mandatory_blocker": bool}
    policy_violations: list
    policy_warnings: list
    requires_human_review_by_policy: bool
    circuit_breaker_tripped: bool
    circuit_breaker_reason: str | None
    author_eligible: bool
    author_risk_threshold: int | None
    analysis_complete: bool
    mandatory_checks_pending: list = field(default_factory=list)


@dataclass
class Decision:
    status: DecisionStatus
    reasons: list = field(default_factory=list)


def decide(inp: DecisionInput) -> Decision:
    reasons: list[str] = []

    if not inp.analysis_complete:
        return Decision(DecisionStatus.MANUAL_REVIEW, ["Analysis did not complete fully; routed to manual review."])

    critical_findings = [f for f in inp.findings if f.get("severity") == "CRITICAL" or f.get("is_mandatory_blocker")]
    if critical_findings:
        reasons.append(f"{len(critical_findings)} critical/mandatory-blocker finding(s) present.")
        return Decision(DecisionStatus.BLOCKED, reasons)

    if inp.policy_violations:
        reasons.extend(inp.policy_violations)
        return Decision(DecisionStatus.BLOCKED, reasons)

    if inp.mandatory_checks_pending:
        reasons.append(
            f"Waiting for mandatory CI check(s) to complete: {', '.join(inp.mandatory_checks_pending)}. "
            f"SecurePR will finalize its decision once GitHub reports a final result for these checks."
        )
        return Decision(DecisionStatus.WAITING_FOR_CI, reasons)

    if inp.circuit_breaker_tripped:
        reasons.append(f"Circuit breaker is active: {inp.circuit_breaker_reason}. "
                        f"Automatic approval suspended; analysis and comments still published.")
        return Decision(DecisionStatus.MANUAL_REVIEW, reasons)

    if not inp.author_eligible:
        reasons.append("Author is not currently configured as eligible for automatic approval.")
        return Decision(DecisionStatus.MANUAL_REVIEW, reasons)

    threshold = inp.author_risk_threshold if inp.author_risk_threshold is not None else 39
    if inp.overall_score > threshold:
        reasons.append(f"Overall risk score {inp.overall_score} exceeds author's auto-approval threshold ({threshold}).")
        return Decision(DecisionStatus.MANUAL_REVIEW, reasons)

    if inp.requires_human_review_by_policy:
        reasons.extend(inp.policy_warnings or ["Policy flags this change for human review."])
        return Decision(DecisionStatus.MANUAL_REVIEW, reasons)

    reasons.append(f"Risk score {inp.overall_score} ({inp.risk_category}) within author's threshold "
                    f"({threshold}); no policy violations, no critical findings, circuit breaker inactive.")
    return Decision(DecisionStatus.APPROVED, reasons)
