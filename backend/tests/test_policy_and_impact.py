from app.services import policy_engine, impact_simulator


def test_blocked_path_detected():
    policy = policy_engine.load_default_policy()
    files = [{"file_path": "infra/secrets/db.pem", "additions": 1, "deletions": 0, "patch_excerpt": ""}]
    ev = policy_engine.evaluate_policy(policy, files, ci_checks=None, author_login="alice")
    assert ev.blocked_paths_touched
    assert ev.violations


def test_mandatory_ci_missing_is_pending_not_a_violation():
    """A mandatory check that simply hasn't reported yet should be PENDING
    (wait for CI), not a hard violation -- only a completed, non-passing
    check should ever be a violation."""
    policy = policy_engine.load_default_policy()
    files = [{"file_path": "app/main.py", "additions": 3, "deletions": 1, "patch_excerpt": ""}]
    ev = policy_engine.evaluate_policy(
        policy, files, ci_checks=[{"name": "build", "status": "completed", "conclusion": "success"}],
        author_login="alice")
    assert "unit-tests" in ev.mandatory_checks_pending
    assert "lint" in ev.mandatory_checks_pending
    assert not ev.mandatory_checks_failed
    assert not ev.violations


def test_mandatory_ci_failed_is_a_hard_violation():
    policy = policy_engine.load_default_policy()
    files = [{"file_path": "app/main.py", "additions": 3, "deletions": 1, "patch_excerpt": ""}]
    ev = policy_engine.evaluate_policy(
        policy, files,
        ci_checks=[
            {"name": "build", "status": "completed", "conclusion": "success"},
            {"name": "unit-tests", "status": "completed", "conclusion": "failure"},
            {"name": "lint", "status": "in_progress", "conclusion": None},
        ],
        author_login="alice")
    assert ev.mandatory_checks_failed == ["unit-tests"]
    assert ev.mandatory_checks_pending == ["lint"]
    assert any("unit-tests" in v for v in ev.violations)


def test_mandatory_ci_aliases_match_human_readable_github_names():
    policy = policy_engine.load_default_policy()
    files = [{"file_path": "app/main.py", "additions": 3, "deletions": 1, "patch_excerpt": ""}]
    ev = policy_engine.evaluate_policy(
        policy,
        files,
        ci_checks=[
            {"name": "Build / Install dependencies", "status": "completed", "conclusion": "success"},
            {"name": "Tests / Unit tests", "status": "completed", "conclusion": "success"},
            {"name": "Lint / Ruff", "status": "completed", "conclusion": "success"},
        ],
        author_login="alice",
    )
    assert ev.mandatory_checks_failed == []
    assert ev.mandatory_checks_pending == []
    assert not ev.violations


def test_human_review_path():
    policy = policy_engine.load_default_policy()
    files = [{"file_path": "backend/auth/login.py", "additions": 10, "deletions": 2, "patch_excerpt": ""}]
    ev = policy_engine.evaluate_policy(
        policy, files,
        ci_checks=[{"name": c, "status": "completed", "conclusion": "success"}
                   for c in ("build", "unit-tests", "lint")],
        author_login="alice")
    assert ev.requires_human_review


def test_impact_simulator_confirmed_function():
    files = [{"file_path": "app/services/billing.py",
              "patch_excerpt": "+def charge_customer(amount):\n+    pass"}]
    items = impact_simulator.simulate(files)
    confirmed = [i for i in items if i.confidence == "CONFIRMED" and i.component_type == "function"]
    assert any(i.component_name == "charge_customer" for i in confirmed)


def test_impact_simulator_route_detection():
    files = [{"file_path": "app/api/payments.py",
              "patch_excerpt": "+@app.post(\"/payments/charge\")\n+def charge():\n+    pass"}]
    items = impact_simulator.simulate(files)
    apis = [i for i in items if i.component_type == "api"]
    assert any("POST /payments/charge" == i.component_name for i in apis)
