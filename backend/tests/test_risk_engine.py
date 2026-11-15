from app.services import risk_engine


def test_low_risk_small_change():
    files = [{"file_path": "docs/readme.md", "additions": 2, "deletions": 1, "patch_excerpt": "+ typo fix"}]
    scores = risk_engine.compute_deterministic_scores(files, ci_checks=[{"name": "build", "conclusion": "success"}])
    scores["policy_compliance"] = risk_engine.compute_policy_compliance_score([])
    assessment = risk_engine.compute_overall(scores)
    assert assessment.category in ("LOW", "MEDIUM")
    assert assessment.overall_score < 40


def test_high_risk_secret_and_sql():
    files = [{
        "file_path": "backend/auth/login.py", "additions": 120, "deletions": 40,
        "patch_excerpt": "+ api_key = 'sk-12345'\n+ DROP TABLE users;\n+ def handle_login():",
    }]
    scores = risk_engine.compute_deterministic_scores(
        files, ci_checks=[{"name": "build", "status": "completed", "conclusion": "failure"}])
    scores["policy_compliance"] = risk_engine.compute_policy_compliance_score(["blocked path"])
    assessment = risk_engine.compute_overall(scores)
    # A hardcoded secret + destructive SQL + failing CI should land solidly in
    # the upper risk bands even after weighting across all six dimensions.
    assert assessment.category in ("MEDIUM", "HIGH")
    assert assessment.overall_score >= 55


def test_blend_never_lowers_heuristic():
    files = [{"file_path": "x.py", "additions": 5, "deletions": 1, "patch_excerpt": ""}]
    heuristic = risk_engine.compute_deterministic_scores(files)
    ai_scores = {"change_surface": {"score": 0, "rationale": "AI thinks it's fine"}}
    blended = risk_engine.blend_with_ai(heuristic, ai_scores)
    assert blended["change_surface"].score == heuristic["change_surface"].score
