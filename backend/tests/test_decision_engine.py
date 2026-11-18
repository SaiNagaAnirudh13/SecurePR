from app.services.decision_engine import decide, DecisionInput
from app.models.models import DecisionStatus


def _base_input(**overrides):
    defaults = dict(
        overall_score=10, risk_category="LOW", findings=[], policy_violations=[], policy_warnings=[],
        requires_human_review_by_policy=False, circuit_breaker_tripped=False, circuit_breaker_reason=None,
        author_eligible=True, author_risk_threshold=39, analysis_complete=True,
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


def test_critical_finding_blocks_independently_of_ci():
    d = decide(_base_input(findings=[{"severity": "CRITICAL"}]))
    assert d.status == DecisionStatus.BLOCKED


def test_confirmed_ci_failure_can_block_through_policy_violation():
    d = decide(_base_input(policy_violations=["A discovered CI execution failed."]))
    assert d.status == DecisionStatus.BLOCKED


def test_pending_ci_does_not_change_review_decision():
    d = decide(_base_input(circuit_breaker_tripped=True, author_eligible=False))
    assert d.status == DecisionStatus.MANUAL_REVIEW
