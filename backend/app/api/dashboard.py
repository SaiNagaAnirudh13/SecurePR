from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import get_current_operator, require_admin
from app.core.config import get_settings
from app.core.db import get_db
from app.models import models as m
from app.schemas.schemas import (
    CircuitBreakerResumeRequest,
    CircuitBreakerTripRequest,
    EligibilityUpdate,
    ReevaluateRequest,
    RepositoryAutoMergeUpdate,
)
from app.services import ci_status, policy_engine
from app.services import circuit_breaker as cb_service
from app.services.pipeline import run_analysis

router = APIRouter(
    prefix="/api",
    tags=["dashboard"],
    dependencies=[Depends(get_current_operator)],
)


def _latest_review_for_pr(
    db: Session, pr_id: str, head_sha: str | None = None
) -> m.Review | None:
    if not head_sha:
        return None
    query = db.query(m.Review).filter(
        m.Review.pull_request_id == pr_id,
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
    )
    if head_sha:
        query = query.filter(m.Review.head_sha == head_sha)
    return query.order_by(m.Review.created_at.desc()).first()


def _auto_merge_status(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    review: m.Review | None,
) -> tuple[str, str | None]:
    if pr.merged_at:
        return "MERGED", None
    if pr.ai_state != "AI_COMPLETED" or not review or review.ai_state != "AI_COMPLETED":
        return (
            "AI_REVIEW_UNAVAILABLE",
            "A validated AI analysis for the current PR revision is required.",
        )
    settings = get_settings()
    if not settings.AUTO_MERGE_ENABLED:
        return "DISABLED_GLOBAL", "Auto-merge is disabled by SecurePR configuration."
    if not repo.is_auto_merge_enabled:
        return (
            "DISABLED_REPOSITORY",
            "Auto-merge is disabled for this repository in SecurePR settings.",
        )
    ci_summary = _ci_status_summary(db, pr.id, pr.head_sha or "")
    if ci_summary["state"] == "FAILED":
        return (
            "CI_FAILED",
            "Auto-merge is blocked because CI failed for the current PR revision.",
        )
    if ci_summary["state"] != "PASSED":
        if ci_summary["state"] == "UNAVAILABLE":
            return "VERIFICATION_UNAVAILABLE", "GitHub CI verification is unavailable."
        return "CI_PENDING", "Auto-merge is waiting for complete CI verification."
    if not review or review.head_sha != pr.head_sha:
        return "NOT_ELIGIBLE", "No review is available for the current PR revision."
    if review.decision == m.DecisionStatus.WAITING_FOR_CI:
        return "CI_PENDING", "Auto-merge is waiting for CI verification."
    if review.decision == m.DecisionStatus.BLOCKED:
        ci_failure = next(
            (
                reason
                for reason in (review.decision_reasons or [])
                if "CI failed for the reviewed revision" in reason
            ),
            None,
        )
        if ci_failure:
            return "CI_FAILED", ci_failure
        return "NOT_ELIGIBLE", "The authoritative SecurePR decision is BLOCKED."
    if review.decision != m.DecisionStatus.APPROVED:
        return "NOT_ELIGIBLE", f"SecurePR decision is {review.decision.value}."
    if review.auto_merge_status:
        return review.auto_merge_status, review.auto_merge_failure_reason
    return "NOT_REQUESTED", "Auto-merge eligibility has not been verified yet."


def _authoritative_decision(
    db: Session, pr: m.PullRequest, review: m.Review | None
) -> str:
    """Derive the current decision from exact-SHA CI before trusting cached review state."""
    if pr.ai_state in {"AI_PENDING", "AI_RUNNING"}:
        return m.DecisionStatus.WAITING_FOR_AI.value
    if pr.ai_state != "AI_COMPLETED":
        return m.DecisionStatus.REVIEW_UNAVAILABLE.value
    ci_summary = _ci_status_summary(db, pr.id, pr.head_sha or "")
    if ci_summary["state"] == "FAILED":
        return m.DecisionStatus.BLOCKED.value
    if ci_summary["state"] != "PASSED":
        return m.DecisionStatus.WAITING_FOR_CI.value
    if not review or review.head_sha != pr.head_sha:
        return m.DecisionStatus.WAITING_FOR_AI.value
    if review.ai_state != "AI_COMPLETED":
        return m.DecisionStatus.REVIEW_UNAVAILABLE.value
    if review.decision == m.DecisionStatus.WAITING_FOR_CI:
        return m.DecisionStatus.WAITING_FOR_CI.value
    return review.decision.value


def _publication_statuses(
    db: Session, repository_id: str, pr_id: str, head_sha: str | None
) -> dict:
    if not head_sha:
        return {}
    rows = (
        db.query(m.ReviewPublication)
        .filter(
            m.ReviewPublication.repository_id == repository_id,
            m.ReviewPublication.pull_request_id == pr_id,
            m.ReviewPublication.head_sha == head_sha,
        )
        .all()
    )
    return {row.message_type: row.state for row in rows}


def _ci_status_summary(db: Session, pull_request_id: str, head_sha: str) -> dict:
    """Return independently monitored CI state for one exact PR revision."""
    revision = (
        db.query(m.CIRevision)
        .filter(
            m.CIRevision.pull_request_id == pull_request_id,
            m.CIRevision.head_sha == head_sha,
        )
        .first()
    )
    if not revision:
        return {
            "passed": 0,
            "failed": 0,
            "pending": 0,
            "neutral": 0,
            "label": "unknown",
            "state": "UNKNOWN",
            "sha": head_sha,
            "error": None,
            "checks": [],
            "check_suites": [],
            "required_checks": [],
            "missing_required_checks": [],
            "last_verified_at": None,
        }

    executions = (
        db.query(m.CIExecution)
        .filter(m.CIExecution.ci_revision_id == revision.id)
        .all()
    )
    records = [
        {
            "identity": row.identity,
            "kind": row.kind,
            "name": row.name,
            "context": row.context,
            "provider": row.provider,
            "status": row.status,
            "app_id": row.app_id,
            "conclusion": row.conclusion,
            "url": row.external_url,
            "html_url": row.external_url,
            "sha": row.head_sha,
        }
        for row in executions
    ]
    summary = ci_status.evaluate_ci_status(
        records,
        fetch_error=revision.error,
        discovery_complete=revision.discovery_complete,
        required_checks=revision.required_checks,
    )
    labels = {
        "PASSED": "passed",
        "FAILED": "failed",
        "IN_PROGRESS": "pending",
        "NO_CHECKS_FOUND": "no_checks",
        "NOT_CONFIGURED": "no_checks",
        "UNAVAILABLE": "unavailable",
        "UNKNOWN": "unknown",
        "NON_SUCCESS": "non_success",
    }
    reported_state = summary.state
    return {
        "passed": summary.passed_count,
        "failed": summary.failed_count,
        "pending": summary.pending_count,
        "neutral": summary.neutral_count,
        "label": labels.get(reported_state, labels.get(summary.state, "unknown")),
        "state": reported_state,
        "sha": revision.head_sha,
        "error": revision.error,
        "required_checks": revision.required_checks or [],
        "missing_required_checks": list(summary.missing_required_checks),
        "last_verified_at": revision.last_verified_at,
        "failed_checks": [
            record
            for record in records
            if ci_status.classify_execution(
                record.get("status"), record.get("conclusion")
            )
            == "failed"
        ],
        "checks": [record for record in records if record["kind"] != "check_suite"],
        "check_suites": [
            record for record in records if record["kind"] == "check_suite"
        ],
    }


def _review_status(db: Session, pr: m.PullRequest) -> str:
    if pr.ai_state in {
        "AI_PENDING",
        "AI_RUNNING",
        "AI_FAILED",
        "AI_UNAVAILABLE",
        "AI_INVALID_RESPONSE",
    }:
        return pr.ai_state
    review = (
        db.query(m.Review)
        .filter(
            m.Review.pull_request_id == pr.id,
            m.Review.head_sha == pr.head_sha,
            m.Review.is_ci_wait_placeholder == False,  # noqa: E712
        )
        .order_by(m.Review.created_at.desc())
        .first()
    )
    if review and review.decision == m.DecisionStatus.WAITING_FOR_CI:
        return "WAITING_FOR_CI"
    job = (
        db.query(m.BackgroundJob)
        .filter(
            m.BackgroundJob.idempotency_key
            == f"review:{pr.repository_id}:{pr.id}:{pr.head_sha}"
        )
        .first()
    )
    if review:
        if not review.analysis_completed_at:
            return "PROCESSING"
        if review.github_action_confirmed:
            return "COMPLETED"
        if not job or job.state in {"FAILED", "COMPLETED"}:
            return "FAILED"
        return "QUEUED" if job.state == "QUEUED" else "PROCESSING"
    if not job:
        return "NOT_STARTED"
    return {
        "QUEUED": "QUEUED",
        "RUNNING": "PROCESSING",
        "COMPLETED": "COMPLETED",
        "FAILED": "FAILED",
    }.get(job.state, "UNKNOWN")


# ---------------------------------------------------------------- Overview --
@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    total = (
        db.query(func.count(m.Review.id))
        .filter(m.Review.is_ci_wait_placeholder == False)
        .scalar()
        or 0
    )  # noqa: E712
    by_decision = dict(
        db.query(m.Review.decision, func.count(m.Review.id))
        .filter(m.Review.is_ci_wait_placeholder == False)  # noqa: E712
        .group_by(m.Review.decision)
        .all()
    )
    by_category = dict(
        db.query(m.Review.risk_category, func.count(m.Review.id))
        .filter(m.Review.is_ci_wait_placeholder == False)  # noqa: E712
        .group_by(m.Review.risk_category)
        .all()
    )
    repos = db.query(m.Repository).filter(m.Repository.is_active == True).all()  # noqa: E712
    repo_out = []
    for r in repos:
        pr_count = (
            db.query(func.count(m.PullRequest.id))
            .filter(m.PullRequest.repository_id == r.id)
            .scalar()
            or 0
        )
        last_review = (
            db.query(m.Review)
            .join(m.PullRequest, m.Review.pull_request_id == m.PullRequest.id)
            .filter(m.PullRequest.repository_id == r.id)
            .order_by(m.Review.created_at.desc())
            .first()
        )
        repo_out.append(
            {
                "id": r.id,
                "full_name": r.full_name,
                "name": r.name,
                "owner_login": r.owner_login,
                "is_active": r.is_active,
                "pr_count": pr_count,
                "is_auto_merge_enabled": r.is_auto_merge_enabled,
                "last_analysis_at": last_review.created_at if last_review else None,
            }
        )
    return {
        "total_reviews": total,
        "by_decision": {
            str(k.value if hasattr(k, "value") else k): v
            for k, v in by_decision.items()
        },
        "by_risk_category": by_category,
        "repositories": repo_out,
    }


@router.get("/overview/trend")
def overview_trend(days: int = 7, db: Session = Depends(get_db)):
    """Daily count of finalized reviews, oldest first -- powers the Overview
    page's single 'Analysis Trend' line chart."""
    since = datetime.utcnow() - timedelta(days=days - 1)
    rows = (
        db.query(m.Review.created_at)
        .filter(m.Review.is_ci_wait_placeholder == False, m.Review.created_at >= since)  # noqa: E712
        .all()
    )
    buckets = {}
    for i in range(days):
        day = (since + timedelta(days=i)).date().isoformat()
        buckets[day] = 0
    for (created_at,) in rows:
        day = created_at.date().isoformat()
        if day in buckets:
            buckets[day] += 1
    return [{"date": day, "count": count} for day, count in buckets.items()]


# -------------------------------------------------------------- Repositories
@router.get("/repositories")
def list_repositories(db: Session = Depends(get_db)):
    repos = db.query(m.Repository).order_by(m.Repository.full_name).all()
    out = []
    for r in repos:
        pr_count = (
            db.query(func.count(m.PullRequest.id))
            .filter(m.PullRequest.repository_id == r.id)
            .scalar()
            or 0
        )
        out.append(
            {
                "id": r.id,
                "full_name": r.full_name,
                "name": r.name,
                "owner_login": r.owner_login,
                "is_active": r.is_active,
                "pr_count": pr_count,
                "is_auto_merge_enabled": r.is_auto_merge_enabled,
            }
        )
    return out


@router.get("/auto-merge/config")
def auto_merge_configuration():
    settings = get_settings()
    return {
        "enabled_by_securepr": settings.AUTO_MERGE_ENABLED,
        "low_risk_only": settings.AUTO_MERGE_LOW_RISK_ONLY,
        "merge_method": settings.AUTO_MERGE_METHOD,
    }


@router.post("/repositories/auto-merge", dependencies=[Depends(require_admin)])
def set_repository_auto_merge(
    req: RepositoryAutoMergeUpdate, db: Session = Depends(get_db)
):
    repo = db.query(m.Repository).filter(m.Repository.id == req.repository_id).first()
    if not repo:
        raise HTTPException(404, "Repository not found")
    repo.is_auto_merge_enabled = req.is_enabled
    db.commit()
    return {
        "repository_id": repo.id,
        "is_auto_merge_enabled": repo.is_auto_merge_enabled,
    }


@router.get("/repositories/{repo_id}/authors")
def list_repo_authors(repo_id: str, db: Session = Depends(get_db)):
    """Distinct PR authors seen for a repository, with their current
    auto-approval eligibility -- lets the dashboard offer a dropdown instead
    of a free-text GitHub-username field wherever one is already known."""
    rows = (
        db.query(m.PullRequest.author_login)
        .filter(m.PullRequest.repository_id == repo_id)
        .filter(m.PullRequest.author_login.isnot(None))
        .distinct()
        .all()
    )
    authors = sorted({r[0] for r in rows if r[0]})
    out = []
    for login in authors:
        user = db.query(m.User).filter(m.User.github_login == login).first()
        elig = None
        if user:
            elig = (
                db.query(m.AutoApprovalEligibility)
                .filter(
                    m.AutoApprovalEligibility.repository_id == repo_id,
                    m.AutoApprovalEligibility.user_id == user.id,
                )
                .first()
            )
        out.append(
            {
                "login": login,
                "is_enabled": bool(elig and elig.is_enabled),
                "max_risk_threshold": elig.max_risk_threshold if elig else None,
            }
        )
    return out


# -------------------------------------------------------- Repositories / PRs
@router.get("/repositories/{repo_id}/pull-requests")
def list_prs(repo_id: str, db: Session = Depends(get_db)):
    prs = (
        db.query(m.PullRequest)
        .filter(m.PullRequest.repository_id == repo_id)
        .order_by(m.PullRequest.updated_at.desc())
        .all()
    )
    out = []
    for pr in prs:
        latest = _latest_review_for_pr(db, pr.id, pr.head_sha)
        repo = (
            db.query(m.Repository).filter(m.Repository.id == pr.repository_id).first()
        )
        merge_status, merge_blocker = (
            _auto_merge_status(db, repo, pr, latest)
            if repo
            else ("NOT_ELIGIBLE", "Repository is no longer available.")
        )
        files_count = (
            db.query(func.count(m.ChangedFile.id))
            .filter(m.ChangedFile.review_id == latest.id)
            .scalar()
            if latest
            else 0
        )
        findings_count = (
            db.query(func.count(m.Finding.id))
            .filter(m.Finding.review_id == latest.id)
            .scalar()
            if latest
            else 0
        )
        out.append(
            {
                "id": pr.id,
                "number": pr.github_pr_number,
                "title": pr.title,
                "author": pr.author_login,
                "head_branch": pr.head_branch,
                "base_branch": pr.base_branch,
                "state": pr.state,
                "updated_at": pr.updated_at,
                "latest_decision": latest.decision.value if latest else None,
                "authoritative_decision": _authoritative_decision(db, pr, latest),
                "current_head_sha": pr.head_sha,
                "auto_merge_status": merge_status,
                "auto_merge_blocker": merge_blocker,
                "review_publications": _publication_statuses(
                    db, pr.repository_id, pr.id, pr.head_sha
                ),
                "latest_score": latest.overall_score
                if latest and latest.ai_state == "AI_COMPLETED"
                else None,
                "latest_risk_category": latest.risk_category
                if latest and latest.ai_state == "AI_COMPLETED"
                else None,
                "review_status": _review_status(db, pr),
                "ai_state": pr.ai_state,
                "ai_error_code": pr.ai_error_code,
                "ai_retryable_at": pr.ai_retryable_at,
                "ai_attempt_id": pr.ai_attempt_id,
                "ci_status": _ci_status_summary(db, pr.id, pr.head_sha or ""),
                "changed_files_count": files_count or 0,
                "findings_count": findings_count or 0,
                "last_analyzed_at": latest.created_at if latest else None,
            }
        )
    return out


@router.get("/pull-requests/recent")
def recent_pull_requests(limit: int = 10, db: Session = Depends(get_db)):
    """Across every repository -- powers the Overview page's 'Recent Pull
    Requests' table without the user needing to pick a repository first."""
    prs = (
        db.query(m.PullRequest)
        .order_by(m.PullRequest.updated_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for pr in prs:
        repo = (
            db.query(m.Repository).filter(m.Repository.id == pr.repository_id).first()
        )
        latest = _latest_review_for_pr(db, pr.id, pr.head_sha)
        merge_status, merge_blocker = (
            _auto_merge_status(db, repo, pr, latest)
            if repo
            else ("NOT_ELIGIBLE", "Repository is no longer available.")
        )
        out.append(
            {
                "id": pr.id,
                "number": pr.github_pr_number,
                "title": pr.title,
                "author": pr.author_login,
                "repository_id": pr.repository_id,
                "repository_full_name": repo.full_name if repo else None,
                "updated_at": pr.updated_at,
                "latest_decision": latest.decision.value if latest else None,
                "authoritative_decision": _authoritative_decision(db, pr, latest),
                "current_head_sha": pr.head_sha,
                "auto_merge_status": merge_status,
                "auto_merge_blocker": merge_blocker,
                "latest_score": latest.overall_score
                if latest and latest.ai_state == "AI_COMPLETED"
                else None,
                "review_status": _review_status(db, pr),
                "ai_state": pr.ai_state,
                "ai_error_code": pr.ai_error_code,
                "ai_retryable_at": pr.ai_retryable_at,
                "ai_attempt_id": pr.ai_attempt_id,
                "ci_status": _ci_status_summary(db, pr.id, pr.head_sha or ""),
            }
        )
    return out


# --------------------------------------------------------------- PR Review -
@router.get("/pull-requests/{pr_id}")
def get_pull_request(pr_id: str, db: Session = Depends(get_db)):
    """Combined PR + repository + latest-review summary, so the frontend can
    deep-link straight to a PR's detail without a second round trip to
    resolve its repository name."""
    pr = db.query(m.PullRequest).filter(m.PullRequest.id == pr_id).first()
    if not pr:
        raise HTTPException(404, "Pull request not found")
    repo = db.query(m.Repository).filter(m.Repository.id == pr.repository_id).first()
    latest = _latest_review_for_pr(db, pr.id, pr.head_sha)
    merge_status, merge_blocker = (
        _auto_merge_status(db, repo, pr, latest)
        if repo
        else ("NOT_ELIGIBLE", "Repository is no longer available.")
    )
    return {
        "id": pr.id,
        "number": pr.github_pr_number,
        "title": pr.title,
        "author": pr.author_login,
        "head_branch": pr.head_branch,
        "base_branch": pr.base_branch,
        "state": pr.state,
        "repository": {"id": repo.id, "full_name": repo.full_name} if repo else None,
        "latest_review_id": latest.id if latest else None,
        "latest_decision": latest.decision.value if latest else None,
        "authoritative_decision": _authoritative_decision(db, pr, latest),
        "current_head_sha": pr.head_sha,
        "auto_merge_status": merge_status,
        "auto_merge_blocker": merge_blocker,
        "auto_merge_enabled_for_repository": repo.is_auto_merge_enabled
        if repo
        else False,
        "auto_merge_enabled_by_securepr": get_settings().AUTO_MERGE_ENABLED,
        "merged_at": pr.merged_at,
        "merge_commit_sha": pr.merge_commit_sha,
        "review_publications": _publication_statuses(
            db, pr.repository_id, pr.id, pr.head_sha
        ),
        "review_status": _review_status(db, pr),
        "ai_state": pr.ai_state,
        "ai_error_code": pr.ai_error_code,
        "ai_retryable_at": pr.ai_retryable_at,
        "ai_attempt_id": pr.ai_attempt_id,
        "head_sha": pr.head_sha,
        "ci_status": _ci_status_summary(db, pr.id, pr.head_sha or ""),
    }


@router.get("/pull-requests/{pr_id}/reviews")
def list_reviews(pr_id: str, db: Session = Depends(get_db)):
    reviews = (
        db.query(m.Review)
        .filter(m.Review.pull_request_id == pr_id)
        .order_by(m.Review.created_at.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "overall_score": r.overall_score,
            "risk_category": r.risk_category,
            "decision": r.decision.value,
            "ai_state": r.ai_state,
            "ai_error_code": r.ai_error_code,
            "ai_retryable_at": r.ai_retryable_at,
            "ai_attempt_id": r.ai_attempt_id,
            "head_sha": r.head_sha,
            "lifecycle_state": r.lifecycle_state,
            "auto_merge_status": r.auto_merge_status,
            "auto_merge_blocker": r.auto_merge_failure_reason,
            "is_reevaluation": r.is_reevaluation,
            "is_ci_wait_placeholder": r.is_ci_wait_placeholder,
            "created_at": r.created_at,
            "github_action_confirmed": r.github_action_confirmed,
        }
        for r in reviews
    ]


@router.get("/reviews/recent")
def recent_reviews(limit: int = 15, db: Session = Depends(get_db)):
    """Recent finalized reviews across all PRs -- lets the Change Impact
    Simulator page offer a picker instead of asking for a raw review id.
    NOTE: this route must be registered BEFORE /reviews/{review_id} or
    FastAPI will match the literal path 'recent' as a review_id."""
    reviews = (
        db.query(m.Review)
        .filter(m.Review.is_ci_wait_placeholder == False)  # noqa: E712
        .order_by(m.Review.created_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for r in reviews:
        pr = (
            db.query(m.PullRequest)
            .filter(m.PullRequest.id == r.pull_request_id)
            .first()
        )
        repo = (
            db.query(m.Repository).filter(m.Repository.id == pr.repository_id).first()
            if pr
            else None
        )
        impact_count = (
            db.query(func.count(m.ImpactPrediction.id))
            .filter(m.ImpactPrediction.review_id == r.id)
            .scalar()
            or 0
        )
        out.append(
            {
                "review_id": r.id,
                "pull_request_id": r.pull_request_id,
                "pr_number": pr.github_pr_number if pr else None,
                "pr_title": pr.title if pr else None,
                "repository_full_name": repo.full_name if repo else None,
                "decision": r.decision.value,
                "overall_score": r.overall_score,
                "created_at": r.created_at,
                "impact_count": impact_count,
            }
        )
    return out


@router.get("/reviews/{review_id}")
def get_review(review_id: str, db: Session = Depends(get_db)):
    """Full evidence-backed detail for decision replay -- returns the exact stored record, no recomputation."""
    review = db.query(m.Review).filter(m.Review.id == review_id).first()
    if not review:
        raise HTTPException(404, "Review not found")
    findings = db.query(m.Finding).filter(m.Finding.review_id == review_id).all()
    evidence = db.query(m.Evidence).filter(m.Evidence.review_id == review_id).all()
    changed_files = (
        db.query(m.ChangedFile).filter(m.ChangedFile.review_id == review_id).all()
    )
    impacts = (
        db.query(m.ImpactPrediction)
        .filter(m.ImpactPrediction.review_id == review_id)
        .all()
    )
    gh_actions = (
        db.query(m.GithubAction).filter(m.GithubAction.review_id == review_id).all()
    )
    policy = (
        db.query(m.Policy).filter(m.Policy.id == review.policy_id).first()
        if review.policy_id
        else None
    )
    pr = (
        db.query(m.PullRequest)
        .filter(m.PullRequest.id == review.pull_request_id)
        .first()
    )

    return {
        "id": review.id,
        "pull_request_id": review.pull_request_id,
        "head_sha": review.head_sha,
        "is_reevaluation": review.is_reevaluation,
        "superseded_review_id": review.superseded_review_id,
        "is_ci_wait_placeholder": review.is_ci_wait_placeholder,
        "ci_wait_pending_checks": review.ci_wait_pending_checks,
        "ci_status": _ci_status_summary(db, review.pull_request_id, review.head_sha),
        "pull_request": {
            "number": pr.github_pr_number,
            "title": pr.title,
            "author": pr.author_login,
        }
        if pr
        else None,
        "scores": {
            "change_surface": review.score_change_surface,
            "reversibility": review.score_reversibility,
            "data_security": review.score_data_security,
            "operational": review.score_operational,
            "verification": review.score_verification,
            "policy_compliance": review.score_policy_compliance,
            "overall": review.overall_score,
        }
        if review.ai_state == "AI_COMPLETED"
        else None,
        "ai_state": review.ai_state,
        "ai_error_code": review.ai_error_code,
        "ai_retryable_at": review.ai_retryable_at,
        "ai_attempt_id": review.ai_attempt_id,
        "risk_category": review.risk_category,
        "decision": review.decision.value,
        "decision_reasons": review.decision_reasons,
        "circuit_breaker_active": review.circuit_breaker_active,
        "github_action_confirmed": review.github_action_confirmed,
        "github_review_state": review.github_review_state,
        "policy": {"name": policy.name, "version": policy.version} if policy else None,
        "findings": [
            {
                "dimension": f.dimension,
                "severity": f.severity,
                "title": f.title,
                "description": f.description,
                "file_path": f.file_path,
                "line_reference": f.line_reference,
                "is_mandatory_blocker": f.is_mandatory_blocker,
            }
            for f in findings
        ],
        "evidence": [
            {"type": e.evidence_type, "source": e.source, "content": e.content}
            for e in evidence
        ],
        "changed_files": [
            {
                "path": c.file_path,
                "type": c.change_type,
                "additions": c.additions,
                "deletions": c.deletions,
                "is_blocked_path": c.is_blocked_path,
            }
            for c in changed_files
        ],
        "impact_predictions": [
            {
                "type": i.component_type,
                "name": i.component_name,
                "confidence": i.confidence.value,
                "evidence": i.evidence_summary,
                "consequence": i.potential_consequence,
            }
            for i in impacts
        ],
        "github_actions": [
            {
                "type": a.action_type,
                "succeeded": a.succeeded,
                "status": a.response_status,
            }
            for a in gh_actions
        ],
        "analysis_started_at": review.analysis_started_at,
        "analysis_completed_at": review.analysis_completed_at,
    }


@router.post("/pull-requests/reevaluate", dependencies=[Depends(require_admin)])
def reevaluate(req: ReevaluateRequest, db: Session = Depends(get_db)):
    """Re-run analysis WITHOUT overwriting prior review history -- creates a new linked Review row."""
    pr = db.query(m.PullRequest).filter(m.PullRequest.id == req.pull_request_id).first()
    if not pr:
        raise HTTPException(404, "Pull request not found")
    repo = db.query(m.Repository).filter(m.Repository.id == pr.repository_id).first()
    if not repo:
        raise HTTPException(404, "Repository not found")
    if not repo.installation_id:
        raise HTTPException(409, "Repository GitHub App installation is unavailable")
    latest = _latest_review_for_pr(db, pr.id)
    review = run_analysis(
        db,
        repo,
        pr,
        repo.installation_id,
        is_reevaluation=True,
        previous_review_id=latest.id if latest else None,
    )
    if not review:
        raise HTTPException(
            409, "Pull request changed or is no longer available for review"
        )
    return {"review_id": review.id, "decision": review.decision.value}


# ---------------------------------------------------------- Circuit Breaker
@router.get("/circuit-breakers")
def list_circuit_breakers(db: Session = Depends(get_db)):
    cbs = db.query(m.CircuitBreaker).all()
    out = []
    for c in cbs:
        repo = (
            db.query(m.Repository).filter(m.Repository.id == c.repository_id).first()
            if c.repository_id
            else None
        )
        out.append(
            {
                "id": c.id,
                "scope": c.scope.value,
                "repository_id": c.repository_id,
                "repository_full_name": repo.full_name if repo else None,
                "user_login": c.user_login,
                "is_tripped": c.is_tripped,
                "reason": c.reason,
                "tripped_by": c.tripped_by,
                "tripped_at": c.tripped_at,
                "auto_resume_at": c.auto_resume_at,
            }
        )
    return out


@router.post("/circuit-breakers/trip", dependencies=[Depends(require_admin)])
def trip_circuit_breaker(req: CircuitBreakerTripRequest, db: Session = Depends(get_db)):
    try:
        scope = m.CircuitScope(req.scope)
    except ValueError:
        raise HTTPException(400, "scope must be GLOBAL, REPOSITORY, or USER")
    cb = cb_service.trip(
        db,
        scope,
        req.actor,
        req.reason,
        req.repository_id,
        req.user_login,
        req.suspend_minutes,
    )
    return {"id": cb.id, "is_tripped": cb.is_tripped}


@router.post("/circuit-breakers/resume", dependencies=[Depends(require_admin)])
def resume_circuit_breaker(
    req: CircuitBreakerResumeRequest, db: Session = Depends(get_db)
):
    try:
        scope = m.CircuitScope(req.scope)
    except ValueError:
        raise HTTPException(400, "scope must be GLOBAL, REPOSITORY, or USER")
    cb = cb_service.resume(db, scope, req.actor, req.repository_id, req.user_login)
    if not cb:
        raise HTTPException(404, "No matching circuit breaker found")
    return {"id": cb.id, "is_tripped": cb.is_tripped}


# -------------------------------------------------------- Approval Eligibility
@router.get("/repositories/{repo_id}/eligibility")
def list_eligibility(repo_id: str, db: Session = Depends(get_db)):
    rows = (
        db.query(m.AutoApprovalEligibility)
        .filter(m.AutoApprovalEligibility.repository_id == repo_id)
        .all()
    )
    out = []
    for row in rows:
        user = db.query(m.User).filter(m.User.id == row.user_id).first()
        out.append(
            {
                "user_login": user.github_login if user else None,
                "is_enabled": row.is_enabled,
                "max_risk_threshold": row.max_risk_threshold,
                "updated_at": row.updated_at,
            }
        )
    return out


@router.post("/repositories/eligibility", dependencies=[Depends(require_admin)])
def set_eligibility(req: EligibilityUpdate, db: Session = Depends(get_db)):
    user = db.query(m.User).filter(m.User.github_login == req.user_login).first()
    if not user:
        user = m.User(github_login=req.user_login)
        db.add(user)
        db.flush()
    row = (
        db.query(m.AutoApprovalEligibility)
        .filter(
            m.AutoApprovalEligibility.repository_id == req.repository_id,
            m.AutoApprovalEligibility.user_id == user.id,
        )
        .first()
    )
    if not row:
        row = m.AutoApprovalEligibility(
            repository_id=req.repository_id, user_id=user.id
        )
        db.add(row)
    row.is_enabled = req.is_enabled
    row.max_risk_threshold = req.max_risk_threshold
    row.updated_by = req.updated_by
    db.commit()
    return {"status": "ok"}


# --------------------------------------------------------------------- Impact
@router.get("/reviews/{review_id}/impact")
def get_impact(review_id: str, db: Session = Depends(get_db)):
    impacts = (
        db.query(m.ImpactPrediction)
        .filter(m.ImpactPrediction.review_id == review_id)
        .all()
    )
    return [
        {
            "type": i.component_type,
            "name": i.component_name,
            "confidence": i.confidence.value,
            "evidence": i.evidence_summary,
            "consequence": i.potential_consequence,
        }
        for i in impacts
    ]


# --------------------------------------------------------------------- Policies
@router.get("/policies")
def list_policies(repository_id: str | None = None, db: Session = Depends(get_db)):
    """Lists policies actually recorded against reviews (the DB table), plus
    the default policy document on disk so it's visible even before any
    review has run against it."""
    query = db.query(m.Policy)
    if repository_id:
        query = query.filter(
            (m.Policy.repository_id == repository_id)
            | (m.Policy.repository_id.is_(None))
        )
    rows = query.order_by(m.Policy.name, m.Policy.version.desc()).all()
    out = []
    seen = set()
    for p in rows:
        repo = (
            db.query(m.Repository).filter(m.Repository.id == p.repository_id).first()
            if p.repository_id
            else None
        )
        out.append(
            {
                "id": p.id,
                "name": p.name,
                "version": p.version,
                "is_active": p.is_active,
                "scope": repo.full_name if repo else "global default",
                "created_at": p.created_at,
                "content": p.content_json,
            }
        )
        seen.add((p.name, p.version))

    default_doc = policy_engine.load_default_policy()
    if (default_doc.get("name", "default"), default_doc.get("version", 1)) not in seen:
        out.insert(
            0,
            {
                "id": None,
                "name": default_doc.get("name", "default"),
                "version": default_doc.get("version", 1),
                "is_active": True,
                "scope": "global default (not yet evaluated against any review)",
                "created_at": None,
                "content": default_doc,
            },
        )
    return out
