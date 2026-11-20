import os
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.api.dashboard import (  # noqa: E402
    _authoritative_decision,
    _auto_merge_status,
    _review_status,
    get_review,
)
from app.core.db import Base  # noqa: E402
from app.models import models as m  # noqa: E402


def test_dashboard_never_exposes_cached_approval_after_current_ci_failure(
    tmp_path, monkeypatch
):
    engine = create_engine(f"sqlite:///{tmp_path / 'dashboard-ci.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()
    try:
        repo = m.Repository(
            github_repo_id="1",
            owner_login="octo",
            name="widgets",
            full_name="octo/widgets",
            installation_id="999",
            is_auto_merge_enabled=True,
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            head_sha="abc123",
            base_branch="main",
            state="open",
        )
        db.add(pr)
        db.flush()
        review = m.Review(
            pull_request_id=pr.id,
            head_sha=pr.head_sha,
            decision=m.DecisionStatus.APPROVED,
            auto_merge_status="AUTO_MERGE_ENABLED",
        )
        db.add(review)
        db.flush()
        revision = m.CIRevision(
            repository_id=repo.id,
            pull_request_id=pr.id,
            head_sha=pr.head_sha,
            state="FAILED",
            discovery_complete=True,
        )
        db.add(revision)
        db.flush()
        db.add(
            m.CIExecution(
                ci_revision_id=revision.id,
                identity="check_run:1",
                kind="check_run",
                name="unit-tests",
                provider="GitHub Actions",
                status="completed",
                conclusion="failure",
                head_sha=pr.head_sha,
            )
        )
        db.commit()
        monkeypatch.setattr(
            "app.api.dashboard.get_settings",
            lambda: type("Settings", (), {"AUTO_MERGE_ENABLED": True})(),
        )

        assert _authoritative_decision(db, pr, review) == "BLOCKED"
        assert _auto_merge_status(db, repo, pr, review) == (
            "CI_FAILED",
            "Auto-merge is blocked because CI failed for the current PR revision.",
        )
    finally:
        db.close()
        engine.dispose()


def test_dashboard_never_exposes_cached_approval_while_ci_is_pending(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dashboard-pending.db'}", future=True
    )
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()
    try:
        repo = m.Repository(
            github_repo_id="1",
            owner_login="octo",
            name="widgets",
            full_name="octo/widgets",
            installation_id="999",
            is_auto_merge_enabled=True,
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            head_sha="abc123",
            base_branch="main",
            state="open",
        )
        db.add(pr)
        db.flush()
        review = m.Review(
            pull_request_id=pr.id,
            head_sha=pr.head_sha,
            decision=m.DecisionStatus.APPROVED,
        )
        db.add(review)
        db.add(
            m.CIRevision(
                repository_id=repo.id,
                pull_request_id=pr.id,
                head_sha=pr.head_sha,
                state="IN_PROGRESS",
                discovery_complete=True,
            )
        )
        db.commit()
        monkeypatch.setattr(
            "app.api.dashboard.get_settings",
            lambda: type("Settings", (), {"AUTO_MERGE_ENABLED": True})(),
        )

        assert _authoritative_decision(db, pr, review) == "WAITING_FOR_CI"
        assert _auto_merge_status(db, repo, pr, review)[0] == "CI_PENDING"
    finally:
        db.close()
        engine.dispose()


def test_review_status_requires_confirmed_publication(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dashboard.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()
    try:
        repo = m.Repository(
            github_repo_id="1",
            owner_login="octo",
            name="widgets",
            full_name="octo/widgets",
            installation_id="999",
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            head_sha="abc123",
            state="open",
        )
        db.add(pr)
        db.flush()
        review = m.Review(
            pull_request_id=pr.id,
            head_sha=pr.head_sha,
            analysis_completed_at=datetime.utcnow(),
        )
        db.add(review)
        db.commit()

        assert _review_status(db, pr) == "FAILED"

        job = m.BackgroundJob(
            job_type="REVIEW",
            idempotency_key=f"review:{repo.id}:{pr.id}:{pr.head_sha}",
            payload={},
            state="QUEUED",
        )
        db.add(job)
        db.commit()
        assert _review_status(db, pr) == "QUEUED"

        review.github_action_confirmed = True
        db.commit()
        assert _review_status(db, pr) == "COMPLETED"
    finally:
        db.close()
        engine.dispose()


def test_review_detail_includes_finding_line_reference(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'review-detail.db'}", future=True)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine, future=True)()
    try:
        repo = m.Repository(
            github_repo_id="1",
            owner_login="octo",
            name="widgets",
            full_name="octo/widgets",
            installation_id="999",
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            head_sha="abc123",
            state="open",
        )
        db.add(pr)
        db.flush()
        review = m.Review(pull_request_id=pr.id, head_sha=pr.head_sha)
        db.add(review)
        db.flush()
        db.add(
            m.Finding(
                review_id=review.id,
                severity="HIGH",
                title="Unsafe input",
                file_path="app/service.py",
                line_reference="L42",
            )
        )
        db.commit()

        detail = get_review(review.id, db)
        assert detail["findings"][0]["line_reference"] == "L42"
    finally:
        db.close()
        engine.dispose()
