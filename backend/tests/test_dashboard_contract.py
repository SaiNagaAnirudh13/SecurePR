import json
import os
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app.api.dashboard import (
    _authoritative_decision,
    _auto_merge_status,
    _review_status,
    get_review,
    list_reviews,
    recent_reviews,
    reevaluate,
    set_eligibility,
)
from app.core.auth import OperatorIdentity
from app.core.db import Base
from app.models import models as m
from app.schemas.schemas import EligibilityUpdate, ReevaluateRequest
from app.services import pipeline
from app.services.pipeline import RevisionLockUnavailable


def test_dashboard_never_reports_approval_when_ai_is_unavailable():
    pr = SimpleNamespace(
        ai_state="AI_INVALID_RESPONSE",
        head_sha="current-sha",
        merged_at=None,
    )
    review = SimpleNamespace(
        ai_state="AI_COMPLETED",
        head_sha="current-sha",
        decision=m.DecisionStatus.APPROVED,
    )
    repo = SimpleNamespace()

    assert _authoritative_decision(None, pr, review) == "REVIEW_UNAVAILABLE"
    assert _auto_merge_status(None, repo, pr, review)[0] == "AI_REVIEW_UNAVAILABLE"


def test_disabling_saved_policy_queues_cancellation_without_review_status(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dashboard-policy-cancel.db'}", future=True
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
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            author_login="octocat",
            head_sha="abc123",
            state="open",
        )
        db.add(pr)
        db.flush()
        user = m.User(github_login="octocat")
        db.add(user)
        db.flush()
        db.add(
            m.AutoApprovalEligibility(
                repository_id=repo.id,
                user_id=user.id,
                is_enabled=True,
                max_risk_threshold=39,
            )
        )
        db.add(
            m.Review(
                pull_request_id=pr.id,
                head_sha=pr.head_sha,
                decision=m.DecisionStatus.APPROVED,
                github_review_state="APPROVE",
                github_action_confirmed=True,
                overall_score=20,
            )
        )
        db.commit()
        response = set_eligibility(
            EligibilityUpdate(
                repository_id=repo.id,
                user_login="octocat",
                is_enabled=False,
                max_risk_threshold=39,
            ),
            OperatorIdentity(user_id="operator", role="admin"),
            db,
        )

        saved = db.query(m.AutoApprovalEligibility).one()
        job = db.query(m.BackgroundJob).filter_by(job_type="AUTO_MERGE_CANCEL").one()
        assert response["status"] == "ok"
        assert saved.is_enabled is False
        assert saved.policy_revision == 1
        assert job.payload["pull_request_id"] == pr.id
        assert job.payload["policy_revision"] == saved.policy_revision
        pr.ai_state = "AI_COMPLETED"
        review = db.query(m.Review).filter_by(pull_request_id=pr.id).one()
        review.ai_state = "AI_COMPLETED"
        db.commit()
        assert _auto_merge_status(db, repo, pr, review)[0] == "CANCELLATION_PENDING"
        job.state = "FAILED"
        db.commit()
        assert _auto_merge_status(db, repo, pr, review)[0] == (
            "CANCELLATION_UNVERIFIED"
        )
        retry = set_eligibility(
            EligibilityUpdate(
                repository_id=repo.id,
                user_login="octocat",
                is_enabled=False,
                max_risk_threshold=39,
            ),
            OperatorIdentity(user_id="operator", role="admin"),
            db,
        )
        assert retry["auto_merge_cancellations_queued"] == 1
        assert job.state == "QUEUED"
    finally:
        db.close()
        engine.dispose()


def test_policy_cancellation_job_reconciles_without_enabled_review_status(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'dashboard-policy-reconcile.db'}", future=True
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
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            author_login="octocat",
            head_sha="abc123",
            state="open",
        )
        user = m.User(github_login="octocat")
        db.add_all([pr, user])
        db.flush()
        db.add(
            m.AutoApprovalEligibility(
                repository_id=repo.id,
                user_id=user.id,
                is_enabled=False,
                max_risk_threshold=39,
                policy_revision=4,
            )
        )
        db.add(
            m.Review(
                pull_request_id=pr.id,
                head_sha=pr.head_sha,
                decision=m.DecisionStatus.APPROVED,
                ai_state="AI_COMPLETED",
                overall_score=20,
                auto_merge_status=None,
            )
        )
        db.commit()
        cancellation_calls = []
        monkeypatch.setattr(
            pipeline.auto_merge,
            "cancel_pending_auto_merge",
            lambda **kwargs: cancellation_calls.append(kwargs) or False,
        )

        pipeline.process_auto_merge_cancel_job(
            db,
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "policy_revision": 4,
                "_job_id": "cancel-job",
            },
        )

        assert len(cancellation_calls) == 1
        assert cancellation_calls[0]["pull_request_id"] == pr.id
        assert (
            db.query(m.AuditLog)
            .filter_by(action="reconcile_github_auto_merge_policy")
            .count()
            == 1
        )
    finally:
        db.close()
        engine.dispose()


def test_dashboard_never_exposes_cached_approval_after_current_ci_failure(tmp_path):
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
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            author_login="octocat",
            head_sha="abc123",
            ai_state="AI_COMPLETED",
            base_branch="main",
            state="open",
        )
        db.add(pr)
        db.flush()
        review = m.Review(
            pull_request_id=pr.id,
            head_sha=pr.head_sha,
            decision=m.DecisionStatus.APPROVED,
            ai_state="AI_COMPLETED",
            auto_merge_status="AUTO_MERGE_ENABLED",
        )
        db.add(review)
        db.flush()
        user = m.User(github_login="octocat")
        db.add(user)
        db.flush()
        db.add(
            m.AutoApprovalEligibility(
                repository_id=repo.id,
                user_id=user.id,
                is_enabled=True,
                max_risk_threshold=39,
            )
        )
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
        assert _authoritative_decision(db, pr, review) == "BLOCKED"
        assert _auto_merge_status(db, repo, pr, review) == (
            "CI_FAILED",
            "Auto-merge is blocked because CI failed for the current PR revision.",
        )
    finally:
        db.close()
        engine.dispose()


def test_dashboard_never_exposes_cached_approval_while_ci_is_pending(tmp_path):
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
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            author_login="octocat",
            head_sha="abc123",
            ai_state="AI_COMPLETED",
            base_branch="main",
            state="open",
        )
        db.add(pr)
        db.flush()
        review = m.Review(
            pull_request_id=pr.id,
            head_sha=pr.head_sha,
            decision=m.DecisionStatus.APPROVED,
            ai_state="AI_COMPLETED",
        )
        db.add(review)
        user = m.User(github_login="octocat")
        db.add(user)
        db.flush()
        db.add(
            m.AutoApprovalEligibility(
                repository_id=repo.id,
                user_id=user.id,
                is_enabled=True,
                max_risk_threshold=39,
            )
        )
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
            ai_state="AI_COMPLETED",
            state="open",
        )
        db.add(pr)
        db.flush()
        review = m.Review(
            pull_request_id=pr.id,
            head_sha=pr.head_sha,
            ai_state="AI_COMPLETED",
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


def test_reevaluation_reschedules_when_revision_lock_times_out(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reevaluation-lock.db'}", future=True
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
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            author_login="octocat",
            head_sha="sha-one",
            state="open",
        )
        db.add(pr)
        db.commit()

        def lock_timeout(*args, **kwargs):
            raise RevisionLockUnavailable()

        monkeypatch.setattr("app.api.dashboard.run_analysis", lock_timeout)

        response = reevaluate(ReevaluateRequest(pull_request_id=pr.id), db)
        payload = json.loads(response.body)

        assert response.status_code == 202
        job = (
            db.query(m.BackgroundJob)
            .filter(m.BackgroundJob.id == payload["job_id"])
            .one()
        )
        assert job.state == "QUEUED"
        assert job.payload["head_sha"] == "sha-one"
        assert job.payload["is_reevaluation"] is True
    finally:
        db.close()
        engine.dispose()


def test_unavailable_ai_scores_are_hidden_from_review_lists(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'unavailable-score.db'}", future=True
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
        )
        db.add(repo)
        db.flush()
        pr = m.PullRequest(
            repository_id=repo.id,
            github_pr_number=7,
            head_sha="abc123",
            ai_state="AI_UNAVAILABLE",
            state="open",
        )
        db.add(pr)
        db.flush()
        review = m.Review(
            pull_request_id=pr.id,
            head_sha=pr.head_sha,
            decision=m.DecisionStatus.REVIEW_UNAVAILABLE,
            lifecycle_state="REVIEW_UNAVAILABLE",
            ai_state="AI_UNAVAILABLE",
            analysis_completed_at=None,
        )
        db.add(review)
        db.commit()

        assert list_reviews(pr.id, db)[0]["overall_score"] is None
        assert recent_reviews(db=db)[0]["overall_score"] is None
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
            ai_state="AI_COMPLETED",
            state="open",
        )
        db.add(pr)
        db.flush()
        review = m.Review(
            pull_request_id=pr.id,
            head_sha=pr.head_sha,
            ai_state="AI_COMPLETED",
        )
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
