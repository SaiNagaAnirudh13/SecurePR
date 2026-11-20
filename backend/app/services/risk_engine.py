"""
Risk Scoring Engine.

Combines deterministic safety signals with validated AI-generated risk
scores. Deterministic signals may raise an evaluated risk score, but they
cannot replace a missing or invalid AI result; the decision engine enforces
that prerequisite before risk and policy evaluation.
"""

from dataclasses import dataclass, field
from typing import Optional

RISK_DIMENSIONS = [
    "change_surface",
    "reversibility",
    "data_security",
    "operational",
    "verification",
    "policy_compliance",
]

DIMENSION_WEIGHTS = {
    "change_surface": 0.20,
    "reversibility": 0.15,
    "data_security": 0.25,
    "operational": 0.15,
    "verification": 0.15,
    "policy_compliance": 0.10,
}

SENSITIVE_PATH_HINTS = [
    "auth",
    "security",
    "secrets",
    "credential",
    "payment",
    "billing",
    "migrations",
    "migration",
    ".github/workflows",
    "infra",
    "terraform",
    "iam",
    "permission",
    "encryption",
    "crypto",
    "key",
    "password",
]

IRREVERSIBLE_HINTS = [
    "drop table",
    "truncate",
    "delete from",
    "rm -rf",
    "alter table",
    "drop column",
    "drop database",
]

SECRET_PATTERN_HINTS = [
    "api_key",
    "apikey",
    "secret_key",
    "private_key",
    "password=",
    "token=",
    "aws_secret",
    "-----begin",
]


@dataclass
class DimensionScore:
    dimension: str
    score: float  # 0-100
    rationale: str
    source: str  # "heuristic" | "ai" | "blended"


@dataclass
class RiskAssessment:
    scores: dict  # dimension -> DimensionScore
    overall_score: float
    category: str
    findings: list = field(default_factory=list)  # list[dict]


def _category_for(score: float) -> str:
    if score == 0:
        return "NOT_EVALUATED"
    if score < 40:
        return "LOW"
    if score < 70:
        return "MEDIUM"
    return "HIGH"


def _heuristic_change_surface(changed_files: list[dict]) -> DimensionScore:
    total_lines = sum(
        f.get("additions", 0) + f.get("deletions", 0) for f in changed_files
    )
    n_files = len(changed_files)
    # Blast radius grows with file count and total line churn
    score = min(100, n_files * 4 + total_lines * 0.08)
    return DimensionScore(
        "change_surface",
        round(score, 1),
        f"{n_files} file(s) changed, {total_lines} total line(s) touched.",
        "heuristic",
    )


def _heuristic_reversibility(changed_files: list[dict]) -> DimensionScore:
    score = 10.0
    flagged = []
    for f in changed_files:
        patch = (f.get("patch_excerpt") or "").lower()
        if any(h in patch for h in IRREVERSIBLE_HINTS):
            score = max(score, 80.0)
            flagged.append(f["file_path"])
        if (
            "migrations" in f["file_path"].lower()
            or "migration" in f["file_path"].lower()
        ):
            score = max(score, 55.0)
            flagged.append(f["file_path"])
    rationale = (
        "Irreversible operations detected in: " + ", ".join(flagged)
        if flagged
        else "No irreversible schema/data operations detected."
    )
    return DimensionScore("reversibility", round(score, 1), rationale, "heuristic")


def _heuristic_data_security(changed_files: list[dict]) -> DimensionScore:
    score = 5.0
    flagged = []
    for f in changed_files:
        path = f["file_path"].lower()
        patch = (f.get("patch_excerpt") or "").lower()
        if any(h in path for h in SENSITIVE_PATH_HINTS):
            score = max(score, 60.0)
            flagged.append(f["file_path"])
        if any(h in patch for h in SECRET_PATTERN_HINTS):
            score = max(score, 90.0)
            flagged.append(f["file_path"] + " (possible hardcoded secret)")
    rationale = (
        "Sensitive security/data surface touched: " + ", ".join(flagged)
        if flagged
        else "No sensitive security or data-handling paths touched."
    )
    return DimensionScore("data_security", round(score, 1), rationale, "heuristic")


def _heuristic_operational(changed_files: list[dict]) -> DimensionScore:
    score = 10.0
    flagged = []
    for f in changed_files:
        path = f["file_path"].lower()
        if any(
            h in path
            for h in [
                "dockerfile",
                "docker-compose",
                ".github/workflows",
                "requirements.txt",
                "package.json",
                "infra",
                "terraform",
                "config",
            ]
        ):
            score = max(score, 45.0)
            flagged.append(f["file_path"])
    rationale = (
        "Deployment/config/dependency surface touched: " + ", ".join(flagged)
        if flagged
        else "No deployment, infrastructure, or dependency files touched."
    )
    return DimensionScore("operational", round(score, 1), rationale, "heuristic")


def _heuristic_verification(
    changed_files: list[dict], ci_checks: Optional[list[dict]]
) -> DimensionScore:
    has_test_changes = any("test" in f["file_path"].lower() for f in changed_files)
    non_test_changes = any("test" not in f["file_path"].lower() for f in changed_files)
    score = 20.0
    notes = []
    if non_test_changes and not has_test_changes:
        score = 55.0
        notes.append("Non-test code changed with no accompanying test changes.")
    if ci_checks:
        failed = [
            c["name"]
            for c in ci_checks
            if c.get("status") == "completed"
            and c.get("conclusion") not in ("success", "skipped", "neutral")
        ]
        if failed:
            score = max(score, 85.0)
            notes.append("Failing CI checks: " + ", ".join(failed))
    else:
        notes.append("No CI check results were available at analysis time.")
        score = max(score, 50.0)
    return DimensionScore("verification", round(score, 1), " ".join(notes), "heuristic")


def compute_deterministic_scores(
    changed_files: list[dict], ci_checks: Optional[list[dict]] = None
) -> dict:
    return {
        "change_surface": _heuristic_change_surface(changed_files),
        "reversibility": _heuristic_reversibility(changed_files),
        "data_security": _heuristic_data_security(changed_files),
        "operational": _heuristic_operational(changed_files),
        "verification": _heuristic_verification(changed_files, ci_checks),
    }


def blend_with_ai(heuristic_scores: dict, ai_scores: Optional[dict]) -> dict:
    """
    Blend heuristic + AI dimension scores. AI can only ever raise a score or
    add findings -- it is never allowed to lower a deterministic risk score,
    since that would let a model talk its way past a hard-coded safety signal.
    """
    blended = dict(heuristic_scores)
    if not ai_scores:
        return blended
    for dim, heuristic in heuristic_scores.items():
        ai_val = ai_scores.get(dim)
        if ai_val is None:
            continue
        try:
            ai_score = float(ai_val.get("score", 0))
        except (TypeError, ValueError):
            continue
        if ai_score > heuristic.score:
            blended[dim] = DimensionScore(
                dim,
                round(ai_score, 1),
                heuristic.rationale + " | AI: " + str(ai_val.get("rationale", "")),
                "blended",
            )
    return blended


def compute_policy_compliance_score(policy_violations: list[str]) -> DimensionScore:
    score = min(100.0, len(policy_violations) * 35.0) if policy_violations else 5.0
    rationale = (
        "Policy violations: " + "; ".join(policy_violations)
        if policy_violations
        else "No policy violations detected."
    )
    return DimensionScore("policy_compliance", round(score, 1), rationale, "heuristic")


def compute_overall(scores: dict) -> RiskAssessment:
    overall = sum(
        scores[d].score * DIMENSION_WEIGHTS[d] for d in RISK_DIMENSIONS if d in scores
    )
    overall = round(overall, 2)
    return RiskAssessment(
        scores=scores, overall_score=overall, category=_category_for(overall)
    )
