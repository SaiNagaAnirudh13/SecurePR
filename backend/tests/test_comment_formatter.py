from datetime import datetime
from types import SimpleNamespace

from app.services.comment_formatter import format_comment
from app.services.comment_formatter import format_review_status_comment
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


def test_waiting_status_comment_announces_analysis_and_only_lists_discovered_checks():
    body = format_review_status_comment(
        pr_number=7,
        head_sha="abc123",
        ci_state="IN_PROGRESS",
        ci_checks=[{
            "name": "build",
            "provider": "GitHub Actions",
            "status": "in_progress",
            "conclusion": None,
        }],
        ci_error=None,
        review=None,
        findings=[],
        marker="<!-- securepr-status:pr:abc123 -->",
    )

    assert "SecurePR Review — Waiting for CI" in body
    assert "SecurePR has received PR #7" in body
    assert "AI analysis and CI verification are being processed." in body
    assert "build" in body
    assert "GitHub Actions" in body
    assert "Vercel" not in body
    assert "CI checks are being monitored" in body
    assert "Final results will be published" in body


def test_terminal_status_comment_remains_waiting_and_does_not_repeat_final_report():
    review = SimpleNamespace(
        analysis_completed_at=datetime.utcnow(),
        decision=SimpleNamespace(value="MANUAL_REVIEW"),
        overall_score=68.5,
        risk_category="MEDIUM",
        score_change_surface=20,
        score_reversibility=30,
        score_data_security=70,
        score_operational=40,
        score_verification=60,
        score_policy_compliance=5,
        github_action_confirmed=True,
        github_review_state="COMMENT",
        auto_merge_status="DISABLED_GLOBAL",
        decision_reasons=["A human reviewer is required."],
    )
    body = format_review_status_comment(
        pr_number=7,
        head_sha="abc123",
        ci_state="FAILED",
        ci_checks=[
            {"name": "build", "provider": "Actions", "status": "completed", "conclusion": "success"},
            {"name": "tests", "provider": "Actions", "status": "completed", "conclusion": "failure"},
            {"name": "docs", "provider": "Actions", "status": "completed", "conclusion": "skipped"},
        ],
        ci_error=None,
        review=review,
        findings=[{
            "severity": "HIGH",
            "title": "Unsafe operation",
            "description": "Review this change.",
            "file_path": "src/app.py",
            "line_reference": "42",
        }],
        marker="<!-- securepr-status:pr:abc123 -->",
    )

    assert "SecurePR Review — Waiting for CI" in body
    assert "Final results will be published" in body
    assert "68.5/100" not in body
    assert "Unsafe operation" not in body
    assert "**CI status:** Failed" in body
    assert "Vercel" not in body


def test_unavailable_ci_is_reported_as_unavailable_not_passed():
    body = format_review_status_comment(
        pr_number=7,
        head_sha="abc123",
        ci_state="UNAVAILABLE",
        ci_checks=[],
        ci_error="check_runs: GitHub returned 503",
        review=SimpleNamespace(
            analysis_completed_at=datetime.utcnow(),
            decision=SimpleNamespace(value="MANUAL_REVIEW"),
            overall_score=64,
            risk_category="MEDIUM",
            score_change_surface=20,
            score_reversibility=30,
            score_data_security=70,
            score_operational=40,
            score_verification=60,
            score_policy_compliance=5,
            github_action_confirmed=False,
            github_review_state=None,
            auto_merge_status=None,
            decision_reasons=["A human reviewer is required."],
        ),
        findings=[],
        marker="<!-- securepr-status:pr:abc123 -->",
    )

    assert "CI discovery error" in body
    assert "retry and will not report CI as passing" in body
