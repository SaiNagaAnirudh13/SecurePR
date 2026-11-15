from app.services.decision_engine import decide, DecisionInput
from app.models.models import DecisionStatus


def _base_input(**overrides):
    defaults = dict(
        overall_score=10, risk_category="LOW", findings=[], policy_violations=[], policy_warnings=[],
        requires_human_review_by_policy=False, circuit_breaker_tripped=False, circuit_breaker_reason=None,
        author_eligible=True, author_risk_threshold=39, analysis_complete=True, mandatory_checks_pending=[],
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
    d = decide(_base_input(circuit_breaker_tripped=True, circuit_breaker_reason="paused for audit"))
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
    d = decide(_base_input(author_eligible=True, overall_score=5, findings=[{"is_mandatory_blocker": True}]))
    assert d.status == DecisionStatus.BLOCKED


def test_waits_for_ci_when_mandatory_checks_still_running():
    d = decide(_base_input(mandatory_checks_pending=["unit-tests", "lint"]))
    assert d.status == DecisionStatus.WAITING_FOR_CI
    assert "unit-tests" in d.reasons[0]


def test_critical_finding_blocks_without_waiting_for_ci():
    """A confirmed secret/destructive-SQL finding should block immediately --
    there's no reason to keep a contributor waiting on CI for that."""
    d = decide(_base_input(findings=[{"severity": "CRITICAL"}], mandatory_checks_pending=["unit-tests"]))
    assert d.status == DecisionStatus.BLOCKED


def test_failed_mandatory_check_blocks_even_if_others_still_pending():
    d = decide(_base_input(policy_violations=["Mandatory CI checks failed: unit-tests"],
                            mandatory_checks_pending=["lint"]))
    assert d.status == DecisionStatus.BLOCKED


def test_waiting_for_ci_takes_priority_over_circuit_breaker_and_eligibility():
    d = decide(_base_input(mandatory_checks_pending=["build"], circuit_breaker_tripped=True,
                            author_eligible=False))
    assert d.status == DecisionStatus.WAITING_FOR_CI
