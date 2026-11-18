from app.services.comment_formatter import format_comment
from app.services import risk_engine


def test_full_comment_renders_all_sections():
    files = [{"file_path": "app/db.py", "additions": 5, "deletions": 1, "patch_excerpt": ""}]
    scores = risk_engine.compute_deterministic_scores(files, ci_checks=[{"name": "build", "status": "completed", "conclusion": "success"}])
    scores["policy_compliance"] = risk_engine.compute_policy_compliance_score([])
    findings = [{"severity": "HIGH", "title": "High data security risk", "description": "x", "file_path": "app/db.py", "dimension": "data_security"}]
    ci_checks = [{"name": "build", "status": "completed", "conclusion": "success"},
                 {"name": "lint", "status": "in_progress", "conclusion": None}]
    body = format_comment(
        decision="APPROVED", reasons=["Risk score within threshold."], overall_score=22.5, category="LOW",
        dimension_scores=scores, findings=findings, changed_files=files, circuit_breaker_tripped=False,
        circuit_breaker_reason=None, impact_items=[], policy_name="default", policy_version=1,
        dashboard_url="http://localhost:5173", ci_checks=ci_checks, ci_state="IN_PROGRESS",
    )
    assert "SecurePR Review" in body
    assert "Risk Breakdown" in body
    assert "CI Verification" in body
    assert "Findings" in body
    assert "Why this decision" in body
    assert "AI Analysis" in body
    assert "AI review published; CI monitoring continues" in body
    assert "Waiting for CI" not in body
    assert "Auto-merge is disabled by default" in body
