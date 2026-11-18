import pytest

from app.services.ci_status import classify_execution, evaluate_ci_status


@pytest.mark.parametrize(
    ("conclusion", "expected"),
    [
        ("success", "passed"),
        ("failure", "failed"),
        ("error", "failed"),
        ("timed_out", "failed"),
        ("startup_failure", "failed"),
        ("action_required", "failed"),
        ("cancelled", "non_success"),
        ("neutral", "non_success"),
        ("skipped", "non_success"),
        ("stale", "non_success"),
        ("unexpected", "unknown"),
    ],
)
def test_terminal_conclusions_are_classified_explicitly(conclusion, expected):
    assert classify_execution("completed", conclusion) == expected


@pytest.mark.parametrize(
    "status", ["queued", "requested", "waiting", "pending", "in_progress"]
)
def test_running_executions_are_pending(status):
    assert classify_execution(status, None) == "pending"


def test_unknown_check_names_are_discovered_without_name_policy():
    record = {
        "kind": "check_run",
        "name": "Third-party quality gate / 🛠",
        "provider": "unfamiliar-provider",
        "status": "in_progress",
        "conclusion": None,
    }

    result = evaluate_ci_status([record])

    assert result.state == "IN_PROGRESS"
    assert result.pending_count == 1


def test_checks_from_multiple_providers_are_aggregated():
    result = evaluate_ci_status([
        {
            "kind": "check_run", "name": "compile", "provider": "actions",
            "status": "completed", "conclusion": "success",
        },
        {
            "kind": "commit_status", "context": "buildkite/deploy",
            "provider": "buildkite", "status": "completed", "conclusion": "success",
        },
    ])

    assert result.state == "PASSED"
    assert result.passed_count == 2


@pytest.mark.parametrize(
    ("conclusion", "state"),
    [
        ("failure", "FAILED"),
        ("timed_out", "FAILED"),
        ("cancelled", "NON_SUCCESS"),
        ("skipped", "NON_SUCCESS"),
        ("neutral", "NON_SUCCESS"),
        ("stale", "NON_SUCCESS"),
    ],
)
def test_failed_and_non_success_results_are_never_reported_as_passed(conclusion, state):
    result = evaluate_ci_status([{
        "kind": "check_run", "name": "unknown-check",
        "status": "completed", "conclusion": conclusion,
    }])

    assert result.state == state
    assert result.passed_count == 0
    assert result.failed_count == (1 if state == "FAILED" else 0)


def test_pending_work_does_not_hide_a_discovered_failure():
    result = evaluate_ci_status([
        {"kind": "check_run", "name": "verify", "status": "in_progress"},
        {"kind": "commit_status", "context": "deploy", "status": "completed", "conclusion": "error"},
    ])

    assert result.state == "FAILED"
    assert result.pending_count == 1
    assert result.failed_count == 1


@pytest.mark.parametrize(
    "executions",
    [
        [],
        [{
            "kind": "check_suite", "name": "queued suite",
            "status": "queued", "conclusion": None,
        }],
    ],
)
def test_no_execution_records_including_suite_only_is_no_checks(executions):
    result = evaluate_ci_status(executions)
    assert result.state == "NOT_CONFIGURED"
    assert result.pending_count == 0


def test_queued_suite_with_active_run_is_in_progress():
    result = evaluate_ci_status([
        {"kind": "check_suite", "status": "queued"},
        {"kind": "check_run", "name": "job", "status": "in_progress"},
    ])
    assert result.state == "IN_PROGRESS"
    assert result.pending_count == 1


def test_discovery_api_error_is_unavailable_not_success_or_failure():
    result = evaluate_ci_status(
        [], fetch_error="GitHub returned 503 while discovering check runs"
    )
    assert result.state == "UNAVAILABLE"
    assert result.error
    assert result.passed_count == result.failed_count == 0


def test_real_failure_remains_actionable_when_another_ci_api_is_unavailable():
    result = evaluate_ci_status(
        [{
            "kind": "check_run", "name": "real failing execution",
            "status": "completed", "conclusion": "failure",
        }],
        fetch_error="Commit status API returned 503",
    )
    assert result.state == "FAILED"
    assert result.failed_count == 1
    assert result.error == "Commit status API returned 503"


def test_no_records_before_first_discovery_is_unknown():
    result = evaluate_ci_status([], discovery_complete=False)
    assert result.state == "UNKNOWN"


def test_unknown_terminal_result_is_not_success():
    result = evaluate_ci_status([{
        "kind": "check_run", "name": "unknown state",
        "status": "completed", "conclusion": "new-provider-result",
    }])
    assert result.state == "UNKNOWN"
    assert result.passed_count == result.failed_count == 0


def test_exact_production_failure_queued_suites_do_not_override_passed_runs():
    runs = [
        {
            "kind": "check_run",
            "id": index,
            "identity": f"check_run:{index}",
            "name": name,
            "status": "completed",
            "conclusion": "success",
        }
        for index, name in enumerate(
            ("dependency-audit", "secret-scan", "build", "test"),
            start=1,
        )
    ]
    suites = [
        {
            "kind": "check_suite",
            "identity": f"check_suite:{index}",
            "name": "Cursor",
            "status": "queued",
            "conclusion": None,
        }
        for index in range(1, 5)
    ]
    suites.extend([
        {
            "kind": "check_suite",
            "identity": f"check_suite:{index}",
            "status": "completed",
            "conclusion": "success",
        }
        for index in range(5, 8)
    ])

    result = evaluate_ci_status(runs + suites)

    assert result.state == "PASSED"
    assert result.passed_count == 4
    assert result.pending_count == 0
