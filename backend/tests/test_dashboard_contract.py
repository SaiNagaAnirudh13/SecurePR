import os
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.api.dashboard import _review_status, get_review  # noqa: E402
from app.core.db import Base  # noqa: E402
from app.models import models as m  # noqa: E402


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
