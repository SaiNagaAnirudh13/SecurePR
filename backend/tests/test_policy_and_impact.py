from app.services import impact_simulator, policy_engine


def test_blocked_path_detected():
    policy = policy_engine.load_default_policy()
    files = [{"file_path": "infra/secrets/db.pem", "additions": 1, "deletions": 0, "patch_excerpt": ""}]
    ev = policy_engine.evaluate_policy(policy, files, ci_checks=None, author_login="alice")
    assert ev.blocked_paths_touched
    assert ev.violations


def test_review_policy_has_no_named_ci_requirements():
    policy = policy_engine.load_default_policy()
    assert "mandatory_ci_checks" not in policy
    assert "ci_check_aliases" not in policy
    assert policy["block_on_ci_failure"] is True


def test_unfamiliar_or_missing_ci_names_do_not_change_file_policy():
    policy = policy_engine.load_default_policy()
    files = [{"file_path": "app/main.py", "additions": 3, "deletions": 1, "patch_excerpt": ""}]
    without_ci = policy_engine.evaluate_policy(policy, files, ci_checks=[], author_login="alice")
    with_unknown_ci = policy_engine.evaluate_policy(
        policy,
        files,
        ci_checks=[{"name": "third-party-quality-gate", "status": "in_progress"}],
        author_login="alice",
    )
    assert without_ci.violations == with_unknown_ci.violations == []
    assert without_ci.requires_human_review == with_unknown_ci.requires_human_review


def test_human_review_path():
    policy = policy_engine.load_default_policy()
    files = [{"file_path": "backend/auth/login.py", "additions": 10, "deletions": 2, "patch_excerpt": ""}]
    ev = policy_engine.evaluate_policy(
        policy, files, ci_checks=[], author_login="alice"
    )
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
