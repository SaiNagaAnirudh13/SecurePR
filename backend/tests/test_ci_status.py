import pytest

from app.services.ci_status import evaluate_ci_status


@pytest.mark.parametrize(
    ("conclusion", "state"),
    [
        ("success", "SUCCESS"),
        ("failure", "FAILURE"),
        ("cancelled", "FAILURE"),
        ("timed_out", "FAILURE"),
        ("action_required", "FAILURE"),
        ("neutral", "NEUTRAL"),
        ("skipped", "NEUTRAL"),
    ],
)
def test_terminal_conclusions_have_explicit_ci_states(conclusion, state):
    result = evaluate_ci_status(
        [{"name": "Custom workflow", "status": "completed", "conclusion": conclusion}],
        [],
        required_checks_pending=[],
        fetch_error=None,
        quiet_seconds=30,
        stabilization_seconds=30,
    )

    assert result.state == state
    assert result.finalized


@pytest.mark.parametrize("status", ["queued", "pending", "in_progress"])
def test_pending_check_states_keep_ci_running(status):
    result = evaluate_ci_status(
        [{"name": "Custom workflow", "status": status, "conclusion": None}],
        [],
        required_checks_pending=[],
        fetch_error=None,
        quiet_seconds=60,
        stabilization_seconds=30,
    )

    assert result.state == "RUNNING"
    assert not result.finalized


def test_check_discovery_waits_for_stabilization_and_resets_on_new_check():
    first = evaluate_ci_status(
        [{"name": "Build", "identity": "check_run:1", "status": "completed", "conclusion": "success"}],
        [],
        required_checks_pending=[],
        fetch_error=None,
        quiet_seconds=5,
        stabilization_seconds=30,
    )
    after_new_check = evaluate_ci_status(
        [
            {"name": "Build", "identity": "check_run:1", "status": "completed", "conclusion": "success"},
            {"name": "Security scan", "identity": "check_run:2", "status": "completed", "conclusion": "success"},
        ],
        [],
        required_checks_pending=[],
        fetch_error=None,
        quiet_seconds=0,
        stabilization_seconds=30,
    )

    assert first.state == "DISCOVERING"
    assert after_new_check.state == "DISCOVERING"
    assert not after_new_check.finalized


def test_required_checks_remain_independent_from_dynamic_discovery():
    result = evaluate_ci_status(
        [{"name": "Build and test", "status": "completed", "conclusion": "success"}],
        [],
        required_checks_pending=["security-gate"],
        fetch_error=None,
        quiet_seconds=60,
        stabilization_seconds=30,
    )

    assert result.state == "RUNNING"
    assert result.pending_checks == ("security-gate",)


@pytest.mark.parametrize(
    ("checks", "suites", "state"),
    [
        ([], [], "NO_CI_CONFIGURED"),
        ([], [{"name": "Actions", "status": "completed", "conclusion": "success"}], "NO_CHECKS_FOUND"),
    ],
)
def test_absent_checks_are_not_success(checks, suites, state):
    result = evaluate_ci_status(
        checks,
        suites,
        required_checks_pending=["required-check"],
        fetch_error=None,
        quiet_seconds=60,
        stabilization_seconds=30,
    )

    assert result.state == state
    assert result.finalized


def test_api_failure_is_error_and_timeout_is_terminal_not_success():
    unavailable = evaluate_ci_status(
        [],
        [],
        required_checks_pending=[],
        fetch_error="GitHub CI APIs were unavailable",
        quiet_seconds=60,
        stabilization_seconds=30,
    )
    timed_out = evaluate_ci_status(
        [],
        [],
        required_checks_pending=[],
        fetch_error="GitHub CI APIs were unavailable",
        quiet_seconds=60,
        stabilization_seconds=30,
        timed_out=True,
    )

    assert unavailable.state == "ERROR"
    assert not unavailable.finalized
    assert timed_out.state == "TIMEOUT"
    assert timed_out.finalized


def test_unrecognized_terminal_data_fails_closed():
    result = evaluate_ci_status(
        [{"name": "Unknown provider state", "status": "completed", "conclusion": "unexpected"}],
        [],
        required_checks_pending=[],
        fetch_error=None,
        quiet_seconds=60,
        stabilization_seconds=30,
    )

    assert result.state == "ERROR"
    assert not result.finalized
