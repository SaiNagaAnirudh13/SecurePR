import pytest

from app.models.models import DecisionStatus
from app.services.decision_engine import DecisionInput, decide


def _base_input(**overrides):
    defaults = dict(
        overall_score=10,
        risk_category="LOW",
        findings=[],
        policy_violations=[],
        policy_warnings=[],
        requires_human_review_by_policy=False,
        circuit_breaker_tripped=False,
        circuit_breaker_reason=None,
        author_eligible=True,
        author_risk_threshold=39,
        analysis_complete=True,
        ci_state="PASSED",
    )
    defaults.update(overrides)
    return DecisionInput(**defaults)


def test_approves_low_risk_eligible_author():
    d = decide(_base_input())
    assert d.status == DecisionStatus.APPROVED


def test_blocks_on_critical_finding_even_if_eligible():
    d = decide(_base_input(findings=[{"severity": "CRITICAL"}]))
    assert d.status == DecisionStatus.BLOCKED


def test_blocks_on_policy_violation():
    d = decide(_base_input(policy_violations=["blocked path touched"]))
    assert d.status == DecisionStatus.BLOCKED


def test_manual_review_when_circuit_breaker_tripped_even_if_low_risk():
    d = decide(
        _base_input(
            circuit_breaker_tripped=True, circuit_breaker_reason="paused for audit"
        )
    )
    assert d.status == DecisionStatus.MANUAL_REVIEW


def test_manual_review_when_author_not_eligible():
    d = decide(_base_input(author_eligible=False))
    assert d.status == DecisionStatus.MANUAL_REVIEW


def test_manual_review_when_score_exceeds_threshold():
    d = decide(_base_input(overall_score=50, author_risk_threshold=39))
    assert d.status == DecisionStatus.MANUAL_REVIEW


def test_manual_review_when_analysis_incomplete():
    d = decide(_base_input(analysis_complete=False))
    assert d.status == DecisionStatus.MANUAL_REVIEW


def test_eligibility_cannot_override_critical_finding():
    d = decide(
        _base_input(
            author_eligible=True,
            overall_score=5,
            findings=[{"is_mandatory_blocker": True}],
        )
    )
    assert d.status == DecisionStatus.BLOCKED


def test_critical_finding_blocks_independently_of_ci():
    d = decide(_base_input(findings=[{"severity": "CRITICAL"}]))
    assert d.status == DecisionStatus.BLOCKED


def test_ci_failure_blocks_before_risk_or_policy_evaluation():
    d = decide(
        _base_input(
            ci_state="FAILED",
            overall_score=1,
            failed_checks=[
                {
                    "name": "dependency-audit",
                    "provider": "GitHub Actions",
                    "conclusion": "failure",
                    "url": "https://github.com/example/repo/actions/runs/12",
                }
            ],
        )
    )
    assert d.status == DecisionStatus.BLOCKED
    assert any("dependency-audit" in reason for reason in d.reasons)
    assert any("GitHub Actions" in reason for reason in d.reasons)
    assert any("actions/runs/12" in reason for reason in d.reasons)


@pytest.mark.parametrize(
    "ci_state",
    ["IN_PROGRESS", "UNKNOWN", "UNAVAILABLE", "NOT_CONFIGURED", "NON_SUCCESS"],
)
def test_nonverified_ci_waits_before_risk_or_policy_evaluation(ci_state):
    d = decide(
        _base_input(
            ci_state=ci_state,
            circuit_breaker_tripped=True,
            author_eligible=False,
        )
    )
    assert d.status == DecisionStatus.WAITING_FOR_CI


def test_missing_ci_state_defaults_to_non_approvable():
    values = vars(_base_input()).copy()
    values.pop("ci_state")
    assert decide(DecisionInput(**values)).status == DecisionStatus.WAITING_FOR_CI


def test_low_risk_and_passed_ci_still_obeys_manual_review_policy():
    d = decide(
        _base_input(
            ci_state="PASSED",
            requires_human_review_by_policy=True,
            policy_warnings=["Human review required for this path."],
        )
    )
    assert d.status == DecisionStatus.MANUAL_REVIEW
