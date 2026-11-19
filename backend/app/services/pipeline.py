"""Independent AI-review and persistent CI-monitoring workflows."""
import hashlib
import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Iterator

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.integrations.gemini_client import gemini_client
from app.integrations.github_client import github_client
from app.models import models as m
from app.services import (
    auto_merge,
    ci_status,
    circuit_breaker,
    decision_engine,
    impact_simulator,
    policy_engine,
    risk_engine,
)
from app.services.background_jobs import enqueue_job
from app.services.comment_formatter import (
    format_auto_merge_comment,
    format_comment,
    format_review_status_comment,
)

logger = logging.getLogger("securepr.pipeline")
settings = get_settings()

_fallback_revision_locks: dict[tuple[str, int], threading.RLock] = {}
_fallback_revision_locks_guard = threading.Lock()


@contextmanager
def _revision_finalization_lock(
    db: Session, repository_id: str, pr_number: int
) -> Iterator[None]:
    """Serialize review creation/publication for one PR across app workers."""
    lock_key = (repository_id, pr_number)
    dialect = db.get_bind().dialect.name
    if dialect not in ("mssql", "postgresql"):
        with _fallback_revision_locks_guard:
            local_lock = _fallback_revision_locks.setdefault(
                lock_key, threading.RLock()
            )
        with local_lock:
            yield
        return

    resource_id = hashlib.sha256(
        f"{repository_id}:{pr_number}".encode("utf-8")
    ).hexdigest()
    resource = f"securepr:review:{resource_id}"
    connection = db.get_bind().connect()
    acquired = False
    try:
        if dialect == "postgresql":
            resource_key = int.from_bytes(
                hashlib.sha256(resource.encode("utf-8")).digest()[:8],
                byteorder="big",
                signed=True,
            )
            connection.execute(text("SET LOCAL lock_timeout = '30s'"))
            connection.execute(
                text("SELECT pg_advisory_lock(:resource_key)"),
                {"resource_key": resource_key},
            )
        else:
            result = connection.execute(
                text(
                    """
                    DECLARE @lock_result int;
                    EXEC @lock_result = sys.sp_getapplock
                        @Resource = :resource,
                        @LockMode = 'Exclusive',
                        @LockOwner = 'Session',
                        @LockTimeout = 30000;
                    SELECT @lock_result;
                    """
                ),
                {"resource": resource},
            ).scalar()
            if result is None or result < 0:
                raise RuntimeError(f"Could not acquire PR review lock (sp_getapplock={result})")
        acquired = True
        yield
    finally:
        if acquired:
            try:
                if dialect == "postgresql":
                    connection.execute(
                        text("SELECT pg_advisory_unlock(:resource_key)"),
                        {"resource_key": resource_key},
                    )
                else:
                    connection.execute(
                        text(
                            """
                            DECLARE @lock_result int;
                            EXEC @lock_result = sys.sp_releaseapplock
                                @Resource = :resource,
                                @LockOwner = 'Session';
                            SELECT @lock_result;
                            """
                        ),
                        {"resource": resource},
                    )
            except Exception:
                logger.exception("PR review lock release failed: repository_id=%s pr_number=%s",
                                 repository_id, pr_number)
        connection.close()


def _log_analysis_timing(stage: str, started_at: float, repo: m.Repository, pr: m.PullRequest) -> None:
    duration_ms = round((time.perf_counter() - started_at) * 1000)
    logger.info(
        "SecurePR analysis timing: stage=%s duration_ms=%s repository=%s pr_number=%s head_sha=%s",
        stage, duration_ms, repo.full_name, pr.github_pr_number, pr.head_sha,
        extra={"timing_event": "analysis", "stage": stage, "duration_ms": duration_ms,
               "repository": repo.full_name, "pr_number": pr.github_pr_number, "head_sha": pr.head_sha},
    )


def _get_or_create_policy_row(db: Session, repo: m.Repository, policy_doc: dict) -> str:
    name = policy_doc.get("name", "default")
    version = policy_doc.get("version", 1)
    row = db.query(m.Policy).filter(
        m.Policy.repository_id == repo.id, m.Policy.name == name, m.Policy.version == version,
    ).first()
    if row:
        return row.id
    row = m.Policy(repository_id=repo.id, name=name, version=version,
                   content_json=policy_doc, is_active=True)
    db.add(row)
    db.flush()
    return row.id


def _ci_revision(db: Session, repo: m.Repository, pr: m.PullRequest, head_sha: str) -> m.CIRevision:
    revision = db.query(m.CIRevision).filter(
        m.CIRevision.repository_id == repo.id,
        m.CIRevision.pull_request_id == pr.id,
        m.CIRevision.head_sha == head_sha,
    ).first()
    if revision:
        return revision
    revision = m.CIRevision(
        repository_id=repo.id,
        pull_request_id=pr.id,
        head_sha=head_sha,
        state="UNKNOWN",
    )
    db.add(revision)
    db.flush()
    return revision


def _ci_records(db: Session, revision: m.CIRevision) -> list[dict]:
    rows = db.query(m.CIExecution).filter(m.CIExecution.ci_revision_id == revision.id).all()
    return [
        {
            "identity": row.identity,
            "kind": row.kind,
            "provider": row.provider,
            "context": row.context,
            "name": row.name,
            "status": row.status,
            "conclusion": row.conclusion,
            "url": row.external_url,
            "html_url": row.external_url,
            "sha": row.head_sha,
        }
        for row in rows
    ]


def _publication(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    head_sha: str,
    message_type: str,
    marker: str,
) -> m.ReviewPublication:
    record = db.query(m.ReviewPublication).filter(
        m.ReviewPublication.repository_id == repo.id,
        m.ReviewPublication.pull_request_id == pr.id,
        m.ReviewPublication.head_sha == head_sha,
        m.ReviewPublication.message_type == message_type,
    ).first()
    if record:
        return record
    record = m.ReviewPublication(
        repository_id=repo.id,
        pull_request_id=pr.id,
        head_sha=head_sha,
        message_type=message_type,
        state="PENDING",
        marker=marker,
    )
    db.add(record)
    db.flush()
    return record


def _mark_publication_published(
    db: Session,
    record: m.ReviewPublication,
    result: dict,
) -> None:
    if not result.get("succeeded", False):
        return
    payload = result.get("payload")
    github_id = None
    if isinstance(payload, dict):
        github_id = payload.get("id")
    record.state = "PUBLISHED"
    record.github_message_id = str(github_id) if github_id is not None else None
    record.published_at = datetime.utcnow()


def _queue_review_status_update(
    db: Session, repo: m.Repository, pr: m.PullRequest, head_sha: str
) -> None:
    revision = db.query(m.CIRevision).filter(
        m.CIRevision.repository_id == repo.id,
        m.CIRevision.pull_request_id == pr.id,
        m.CIRevision.head_sha == head_sha,
    ).first()
    records = _ci_records(db, revision) if revision else []
    review = db.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == head_sha,
        m.Review.is_reevaluation == False,  # noqa: E712
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
    ).first()
    fingerprint = hashlib.sha256(json.dumps({
        "state": revision.state if revision else "UNKNOWN",
        "error": revision.error if revision else None,
        "records": records,
        "review": (
            {
                "id": review.id,
                "decision": review.decision.value,
                "github_action_confirmed": review.github_action_confirmed,
                "auto_merge_status": review.auto_merge_status,
            }
            if review else None
        ),
    }, sort_keys=True).encode("utf-8")).hexdigest()
    enqueue_job(
        db,
        "REVIEW_STATUS",
        f"review-status:{pr.id}:{head_sha}:{fingerprint}",
        {
            "repository_id": repo.id,
            "pull_request_id": pr.id,
            "installation_id": repo.installation_id,
            "head_sha": head_sha,
        },
    )


def _queue_late_ci_failures(
    db: Session, repo: m.Repository, pr: m.PullRequest, review: m.Review
) -> None:
    revision = db.query(m.CIRevision).filter(
        m.CIRevision.repository_id == repo.id,
        m.CIRevision.pull_request_id == pr.id,
        m.CIRevision.head_sha == review.head_sha,
    ).first()
    if not revision:
        return
    _queue_ci_notifications(db, repo, pr, revision)


def _queue_ci_notifications(
    db: Session, repo: m.Repository, pr: m.PullRequest, revision: m.CIRevision
) -> None:
    """CI failure details are included in the authoritative review, not extra comments."""
    return


def _publish_existing_review(
    db: Session, repo: m.Repository, pr: m.PullRequest,
    installation_id: str, review: m.Review,
) -> None:
    if review.github_action_confirmed:
        return
    body_row = db.query(m.Evidence).filter(
        m.Evidence.review_id == review.id,
        m.Evidence.evidence_type == "github_review_body",
    ).first()
    if not body_row:
        raise RuntimeError(
            f"Review {review.id} has no persisted GitHub publication body."
        )
    if review.github_review_state not in ("APPROVE", "COMMENT", "REQUEST_CHANGES"):
        raise RuntimeError(f"Review {review.id} has no valid GitHub review event.")

    marker = f"<!-- securepr-review:{pr.id}:{review.head_sha} -->"
    owner, repo_name = repo.full_name.split("/", 1)
    publication = _publication(
        db, repo, pr, review.head_sha, "FINAL", marker
    )
    if publication.state == "PUBLISHED":
        return
    try:
        result = github_client.post_review(
            installation_id, owner, repo_name, pr.github_pr_number,
            body_row.content, review.github_review_state,
            idempotency_marker=marker,
        )
        db.add(m.GithubAction(
            review_id=review.id, action_type="submit_review",
            request_payload={"event": review.github_review_state, "marker": marker},
            response_status=result.get("status_code"),
            response_payload=result.get("payload")
            if isinstance(result.get("payload"), dict) else None,
            succeeded=result.get("succeeded", False),
        ))
        review.github_action_confirmed = result.get("succeeded", False)
        _mark_publication_published(db, publication, result)
        db.commit()
        if not review.github_action_confirmed:
            raise RuntimeError(
                f"GitHub rejected review publication for review {review.id}."
            )
        if review.github_action_confirmed:
            _queue_late_ci_failures(db, repo, pr, review)
    except Exception:
        db.rollback()
        logger.exception("Failed to publish AI review: review_id=%s", review.id)
        raise


def run_analysis(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    is_reevaluation: bool = False,
    previous_review_id: str | None = None,
    expected_head_sha: str | None = None,
    trigger: str = "analysis",
    event_name: str | None = None,
) -> m.Review | None:
    """Run AI analysis for one revision without waiting on any CI operation."""
    repository_id, pull_request_id, pr_number = repo.id, pr.id, pr.github_pr_number
    with _revision_finalization_lock(db, repository_id, pr_number):
        db.expire_all()
        current_pr = db.query(m.PullRequest).filter(m.PullRequest.id == pull_request_id).first()
        current_repo = db.query(m.Repository).filter(m.Repository.id == repository_id).first()
        if current_pr is None or current_repo is None:
            return None
        if expected_head_sha and current_pr.head_sha != expected_head_sha:
            logger.info("Ignoring stale review request: repository=%s pr=%s expected_sha=%s current_sha=%s",
                        current_repo.full_name, pr_number, expected_head_sha, current_pr.head_sha)
            return None
        if not is_reevaluation and previous_review_id is None:
            existing = db.query(m.Review).filter(
                m.Review.pull_request_id == current_pr.id,
                m.Review.head_sha == current_pr.head_sha,
                m.Review.is_reevaluation == False,  # noqa: E712
                m.Review.is_ci_wait_placeholder == False,  # noqa: E712
            ).first()
            if existing:
                _publish_existing_review(
                    db, current_repo, current_pr, installation_id, existing
                )
                return existing
        return _run_analysis_locked(
            db, current_repo, current_pr, installation_id,
            is_reevaluation=is_reevaluation,
            previous_review_id=previous_review_id,
            trigger=trigger,
            event_name=event_name,
        )


def _run_analysis_locked(
    db: Session,
    repo: m.Repository,
    pr: m.PullRequest,
    installation_id: str,
    *,
    is_reevaluation: bool = False,
    previous_review_id: str | None = None,
    trigger: str = "analysis",
    event_name: str | None = None,
) -> m.Review | None:
    started_clock = time.perf_counter()
    started_at = datetime.utcnow()
    owner, repo_name = repo.full_name.split("/", 1)
    try:
        live_pr = github_client.get_pull_request(
            installation_id, owner, repo_name, pr.github_pr_number
        )
    except Exception:
        logger.exception("Could not verify PR before AI review: repository=%s pr=%s",
                         repo.full_name, pr.github_pr_number)
        raise
    if live_pr.get("state") != "open":
        pr.state = live_pr.get("state") or pr.state
        db.commit()
        return None
    live_sha = live_pr.get("head_sha")
    if not isinstance(live_sha, str) or not live_sha:
        raise ValueError("GitHub pull-request response is missing its head SHA.")
    if live_sha != pr.head_sha:
        logger.info(
            "Superseding stale analysis before publication: repository=%s pr=%s "
            "job_sha=%s live_sha=%s",
            repo.full_name, pr.github_pr_number, pr.head_sha, live_sha,
        )
        pr.head_sha = live_sha
        pr.updated_at = datetime.utcnow()
        db.commit()
        return None

    policy_doc = policy_engine.load_repo_policy(repo.full_name)
    policy_id = _get_or_create_policy_row(db, repo, policy_doc)
    revision = _ci_revision(db, repo, pr, pr.head_sha)
    db.commit()
    ci_records = _ci_records(db, revision)
    ci_execution_records = [item for item in ci_records if item["kind"] != "check_suite"]
    ci_evaluation = ci_status.evaluate_ci_status(
        ci_records,
        fetch_error=revision.error if revision.state == "UNAVAILABLE" else None,
        discovery_complete=revision.state != "UNKNOWN",
    )

    analysis_complete = True
    changed_files_error = None
    try:
        changed_files = github_client.get_pr_files(
            installation_id, owner, repo_name, pr.github_pr_number
        )
    except Exception as exc:
        logger.exception("Could not fetch changed PR files: repository=%s pr=%s",
                         repo.full_name, pr.github_pr_number)
        changed_files = []
        analysis_complete = False
        changed_files_error = f"Could not retrieve changed files: {exc}"

    policy_eval = policy_engine.evaluate_policy(
        policy_doc, changed_files, ci_execution_records, pr.author_login
    )
    ai_scores = None
    if analysis_complete:
        heuristic_scores = risk_engine.compute_deterministic_scores(
            changed_files, ci_execution_records
        )
        heuristic_scores["policy_compliance"] = risk_engine.compute_policy_compliance_score(
            policy_eval.violations
        )
        try:
            ai_scores = gemini_client.score_dimensions(changed_files)
        except Exception:
            logger.exception("AI risk scoring failed; using deterministic risk scores")
        blended = risk_engine.blend_with_ai(heuristic_scores, ai_scores)
        assessment = risk_engine.compute_overall(blended)
    else:
        rationale = "Not evaluated because GitHub did not provide the changed-file list."
        blended = {
            dimension: risk_engine.DimensionScore(
                dimension=dimension, score=0, rationale=rationale, source="not_evaluated"
            )
            for dimension in risk_engine.RISK_DIMENSIONS
        }
        assessment = risk_engine.RiskAssessment(
            scores=blended, overall_score=0, category="NOT_EVALUATED"
        )

    findings: list[dict] = []
    for path in policy_eval.blocked_paths_touched:
        findings.append({
            "dimension": "policy_compliance", "severity": "CRITICAL",
            "title": "Blocked path modified",
            "description": f"'{path}' matches a blocked-path rule.",
            "file_path": path, "is_mandatory_blocker": True,
        })
    if ci_evaluation.state == "FAILED" and policy_doc.get("block_on_ci_failure", True):
        failed_records = [
            item for item in ci_execution_records
            if ci_status.classify_execution(item.get("status"), item.get("conclusion")) == "failed"
        ]
        policy_eval.violations.append("A discovered CI execution failed.")
        for record in failed_records:
            findings.append({
                "dimension": "verification", "severity": "HIGH",
                "title": f"CI failed: {record.get('name') or record.get('context') or 'Unnamed check'}",
                "description": (
                    f"{record.get('provider') or 'CI provider'} reported "
                    f"{record.get('conclusion')} for this revision."
                ),
                "file_path": None, "is_mandatory_blocker": True,
            })
    for dim, score in blended.items():
        if score.score >= 70:
            findings.append({
                "dimension": dim, "severity": "HIGH",
                "title": f"High {dim.replace('_', ' ')} risk",
                "description": score.rationale, "file_path": None,
                "is_mandatory_blocker": False,
            })

    impact_items = impact_simulator.simulate(changed_files) if analysis_complete else []
    cb_tripped, cb_reason = circuit_breaker.is_tripped(db, repo.id, pr.author_login)
    author = db.query(m.User).filter(m.User.github_login == pr.author_login).first()
    eligibility = None
    if author:
        eligibility = db.query(m.AutoApprovalEligibility).filter(
            m.AutoApprovalEligibility.repository_id == repo.id,
            m.AutoApprovalEligibility.user_id == author.id,
        ).first()
    decision = decision_engine.decide(decision_engine.DecisionInput(
        overall_score=assessment.overall_score,
        risk_category=assessment.category,
        findings=findings,
        policy_violations=policy_eval.violations,
        policy_warnings=policy_eval.warnings,
        requires_human_review_by_policy=policy_eval.requires_human_review,
        circuit_breaker_tripped=cb_tripped,
        circuit_breaker_reason=cb_reason,
        author_eligible=bool(eligibility and eligibility.is_enabled),
        author_risk_threshold=eligibility.max_risk_threshold if eligibility else None,
        analysis_complete=analysis_complete,
    ))
    if changed_files_error:
        decision.reasons = [changed_files_error]

    latest_pr = github_client.get_pull_request(
        installation_id, owner, repo_name, pr.github_pr_number
    )
    latest_sha = latest_pr.get("head_sha")
    if latest_pr.get("state") != "open":
        pr.state = latest_pr.get("state") or pr.state
        db.commit()
        return None
    if latest_sha != pr.head_sha:
        if isinstance(latest_sha, str) and latest_sha:
            pr.head_sha = latest_sha
            db.commit()
            _ci_revision(db, repo, pr, latest_sha)
            enqueue_job(
                db,
                "REVIEW",
                f"review:{repo.id}:{pr.id}:{latest_sha}",
                {
                    "repository_id": repo.id,
                    "pull_request_id": pr.id,
                    "installation_id": installation_id,
                    "head_sha": latest_sha,
                },
            )
            enqueue_job(
                db,
                "CI_REFRESH",
                f"ci-initial:{repo.id}:{pr.id}:{latest_sha}",
                {
                    "repository_id": repo.id,
                    "pull_request_id": pr.id,
                    "installation_id": installation_id,
                    "head_sha": latest_sha,
                    "event_name": "pull_request",
                    "reason": "initial",
                },
            )
            _queue_review_status_update(db, repo, pr, latest_sha)
            db.commit()
        logger.info("Superseding in-flight analysis: repository=%s pr=%s reviewed_sha=%s current_sha=%s",
                    repo.full_name, pr.github_pr_number, live_sha, latest_sha)
        return None

    final_ci_revision = db.query(m.CIRevision).filter(
        m.CIRevision.repository_id == repo.id,
        m.CIRevision.pull_request_id == pr.id,
        m.CIRevision.head_sha == pr.head_sha,
    ).first()
    if final_ci_revision:
        ci_records = _ci_records(db, final_ci_revision)
        ci_execution_records = [item for item in ci_records if item["kind"] != "check_suite"]
        ci_evaluation = ci_status.evaluate_ci_status(
            ci_records,
            fetch_error=final_ci_revision.error
            if final_ci_revision.state == "UNAVAILABLE" else None,
            discovery_complete=final_ci_revision.state != "UNKNOWN",
        )

    review = m.Review(
        pull_request_id=pr.id,
        policy_id=policy_id,
        head_sha=pr.head_sha,
        is_reevaluation=is_reevaluation,
        superseded_review_id=previous_review_id,
        analysis_started_at=started_at,
    )
    db.add(review)
    db.flush()
    for finding in findings:
        db.add(m.Finding(review_id=review.id, **finding))
    for item in impact_items:
        db.add(m.ImpactPrediction(
            review_id=review.id, component_type=item.component_type,
            component_name=item.component_name, confidence=item.confidence,
            evidence_summary=item.evidence_summary,
            potential_consequence=item.potential_consequence,
        ))
    for changed_file in changed_files:
        db.add(m.ChangedFile(
            review_id=review.id,
            file_path=changed_file["file_path"],
            change_type=changed_file.get("change_type"),
            additions=changed_file.get("additions", 0),
            deletions=changed_file.get("deletions", 0),
            is_blocked_path=changed_file["file_path"] in policy_eval.blocked_paths_touched,
            patch_excerpt=changed_file.get("patch_excerpt"),
        ))
    for dimension, score in blended.items():
        db.add(m.Evidence(
            review_id=review.id, evidence_type="dimension_rationale", source=score.source,
            content=f"{dimension}: {score.rationale}",
        ))
    if ai_scores and ai_scores.get("summary"):
        db.add(m.Evidence(review_id=review.id, evidence_type="ai_rationale",
                          source="gemini", content=ai_scores["summary"]))
    if not analysis_complete:
        db.add(m.Evidence(review_id=review.id, evidence_type="error",
                          source="github_fetch", content=changed_files_error))
    db.add(m.Evidence(
        review_id=review.id,
        evidence_type="ci_result",
        source="securepr_ci_monitor",
        content=json.dumps({
            "state": ci_evaluation.state,
            "sha": pr.head_sha,
            "checks": ci_records,
            "error": ci_evaluation.error,
        }),
    ))
    review.score_change_surface = blended["change_surface"].score
    review.score_reversibility = blended["reversibility"].score
    review.score_data_security = blended["data_security"].score
    review.score_operational = blended["operational"].score
    review.score_verification = blended["verification"].score
    review.score_policy_compliance = blended["policy_compliance"].score
    review.overall_score = assessment.overall_score
    review.risk_category = assessment.category
    review.decision = decision.status
    review.decision_reasons = decision.reasons
    review.circuit_breaker_active = cb_tripped
    review.analysis_completed_at = datetime.utcnow()

    dashboard_url = f"{settings.DASHBOARD_ORIGIN}/#/pr/{pr.id}"
    body = format_comment(
        decision=decision.status.value, reasons=decision.reasons,
        overall_score=assessment.overall_score, category=assessment.category,
        dimension_scores=blended, findings=findings, changed_files=changed_files,
        circuit_breaker_tripped=cb_tripped, circuit_breaker_reason=cb_reason,
        impact_items=impact_items, policy_name=policy_eval.policy_name,
        policy_version=policy_eval.policy_version, dashboard_url=dashboard_url,
        ci_checks=ci_execution_records, ci_state=ci_evaluation.state,
        analysis_error=changed_files_error,
    )
    event_map = {
        "APPROVED": "APPROVE",
        "BLOCKED": "REQUEST_CHANGES",
        "MANUAL_REVIEW": "COMMENT",
    }
    github_event = event_map[decision.status.value]
    marker = f"<!-- securepr-review:{pr.id}:{review.head_sha} -->"
    body = f"{body}\n\n{marker}"
    review.github_review_state = github_event
    final_publication = _publication(
        db, repo, pr, review.head_sha, "FINAL", marker
    )
    db.add(m.Evidence(
        review_id=review.id,
        evidence_type="github_review_body",
        source="securepr",
        content=body,
    ))
    db.commit()
    db.refresh(review)
    if final_publication.state == "PUBLISHED":
        review.github_action_confirmed = True
        db.commit()
    else:
        try:
            result = github_client.post_review(
                installation_id, owner, repo_name, pr.github_pr_number, body, github_event,
                idempotency_marker=marker,
            )
            db.add(m.GithubAction(
                review_id=review.id, action_type="submit_review",
                request_payload={"event": github_event},
                response_status=result.get("status_code"),
                response_payload=result.get("payload")
                if isinstance(result.get("payload"), dict) else None,
                succeeded=result.get("succeeded", False),
            ))
            review.github_action_confirmed = result.get("succeeded", False)
            review.github_review_state = github_event
            _mark_publication_published(db, final_publication, result)
            db.commit()
            if not review.github_action_confirmed:
                raise RuntimeError(
                    f"GitHub rejected review publication for review {review.id}."
                )
            if review.github_action_confirmed:
                _queue_late_ci_failures(db, repo, pr, review)
        except Exception:
            logger.exception("Failed to publish AI review: review_id=%s", review.id)
            db.add(m.GithubAction(
                review_id=review.id, action_type="submit_review", succeeded=False
            ))
            db.commit()
            raise

    if decision.status == m.DecisionStatus.APPROVED:
        merge_result = auto_merge.attempt(
            db=db, repo=repo, pr=pr, review=review, installation_id=installation_id,
            policy_doc=policy_doc, findings=findings,
            author_eligible=bool(eligibility and eligibility.is_enabled),
            author_threshold=eligibility.max_risk_threshold if eligibility else None,
        )
        review.auto_merge_status = merge_result.status
        if merge_result.status in ("MERGED", "FAILED"):
            review.auto_merge_attempted_at = datetime.utcnow()
        if merge_result.status == "MERGED":
            review.auto_merged_at = datetime.utcnow()
            review.merge_commit_sha = merge_result.merge_commit_sha
        elif merge_result.status not in ("DISABLED_GLOBAL", "DISABLED_REPOSITORY"):
            review.auto_merge_failure_reason = merge_result.reason
        db.add(m.Evidence(
            review_id=review.id, evidence_type="auto_merge", source="securepr",
            content=json.dumps({
                "status": merge_result.status, "reason": merge_result.reason,
                "merge_commit_sha": merge_result.merge_commit_sha,
            }),
        ))
        db.commit()
        try:
            merge_comment = format_auto_merge_comment(
                status=merge_result.status, overall_score=assessment.overall_score,
                reason=merge_result.reason, merge_commit_sha=merge_result.merge_commit_sha,
            )
            comment_result = github_client.post_issue_comment(
                installation_id, owner, repo_name, pr.github_pr_number, merge_comment
            )
            db.add(m.GithubAction(
                review_id=review.id, action_type="post_auto_merge_result",
                response_status=comment_result.get("status_code"),
                response_payload=comment_result.get("payload"),
                succeeded=comment_result.get("succeeded", False),
            ))
            db.commit()
        except Exception:
            logger.exception("Failed to post auto-merge outcome: review_id=%s", review.id)

    completion_marker = f"<!-- securepr-complete:{pr.id}:{review.head_sha} -->"
    completion_publication = _publication(
        db, repo, pr, review.head_sha, "COMPLETION", completion_marker
    )
    if completion_publication.state != "PUBLISHED":
        completion_message = (
            f"### SecurePR Review Complete\n\n"
            f"SecurePR review complete for PR #{pr.github_pr_number}: "
            f"**{decision.status.value}** "
            f"(risk {assessment.overall_score}/100, {assessment.category}). "
            f"[View detailed review]({settings.DASHBOARD_ORIGIN}/#/pr/{pr.id}) "
            f"\n\n{completion_marker}"
        )
        try:
            result = github_client.post_issue_comment(
                installation_id, owner, repo_name, pr.github_pr_number,
                completion_message,
                idempotency_marker=completion_marker,
            )
            db.add(m.GithubAction(
                review_id=review.id, action_type="post_completion_notification",
                request_payload={"marker": completion_marker},
                response_status=result.get("status_code"),
                response_payload=result.get("payload")
                if isinstance(result.get("payload"), dict) else None,
                succeeded=result.get("succeeded", False),
            ))
            _mark_publication_published(db, completion_publication, result)
            db.add(m.Notification(
                review_id=review.id, recipient=owner, channel="github",
                subject=f"PR #{pr.github_pr_number} {decision.status.value}",
                body=completion_message,
                delivered=result.get("succeeded", False),
            ))
            db.commit()
            if not result.get("succeeded", False):
                raise RuntimeError(
                    f"GitHub rejected completion notification for review {review.id}."
                )
        except Exception:
            logger.exception("Failed to notify repository owner: review_id=%s", review.id)
            raise

    elapsed = time.perf_counter() - started_clock
    _queue_review_status_update(db, repo, pr, review.head_sha)
    db.commit()
    if elapsed >= settings.REVIEW_RESULT_DEADLINE_SECONDS:
        logger.warning(
            "AI review exceeded configured result deadline: elapsed_seconds=%.2f deadline_seconds=%s "
            "repository=%s pr=%s sha=%s review_id=%s",
            elapsed, settings.REVIEW_RESULT_DEADLINE_SECONDS,
            repo.full_name, pr.github_pr_number, pr.head_sha, review.id,
        )
    _log_analysis_timing("total", started_clock, repo, pr)
    return review


def process_review_job(db: Session, payload: dict) -> None:
    repo = db.query(m.Repository).filter(m.Repository.id == payload["repository_id"]).first()
    pr = db.query(m.PullRequest).filter(m.PullRequest.id == payload["pull_request_id"]).first()
    if not repo or not pr:
        raise ValueError("Review job references a missing repository or pull request.")
    owner, repo_name = repo.full_name.split("/", 1)
    live_pr = github_client.get_pull_request(
        payload["installation_id"], owner, repo_name, pr.github_pr_number
    )
    if live_pr.get("state") != "open":
        pr.state = live_pr.get("state") or pr.state
        db.commit()
        return
    live_sha = live_pr.get("head_sha")
    if not isinstance(live_sha, str) or not live_sha:
        raise ValueError("GitHub pull-request response is missing its head SHA.")
    if pr.head_sha != live_sha:
        logger.info(
            "Reconciling PR head before review job: repository=%s pr=%s stored_sha=%s live_sha=%s",
            repo.full_name, pr.github_pr_number, pr.head_sha, live_sha,
        )
        pr.head_sha = live_sha
        pr.state = "open"
        _ci_revision(db, repo, pr, live_sha)
        db.commit()
        enqueue_job(
            db,
            "CI_REFRESH",
            f"ci-initial:{repo.id}:{pr.id}:{live_sha}",
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "installation_id": payload["installation_id"],
                "head_sha": live_sha,
                "event_name": "pull_request",
                "reason": "reconciled",
            },
        )
        enqueue_job(
            db,
            "REVIEW_STATUS",
            f"review-status-initial:{pr.id}:{live_sha}",
            {
                "repository_id": repo.id,
                "pull_request_id": pr.id,
                "installation_id": payload["installation_id"],
                "head_sha": live_sha,
            },
        )
        db.commit()
    run_analysis(
        db, repo, pr, payload["installation_id"],
        expected_head_sha=live_sha, trigger="durable_review_job",
    )


def _fetch_ci_checks(installation_id: str, owner: str, repo_name: str, head_sha: str):
    import concurrent.futures

    checks, suites, errors = [], [], []
    sources = (
        ("check_runs", github_client.get_check_runs),
        ("commit_status", github_client.get_commit_statuses),
        ("check_suite", github_client.get_check_suites),
    )
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="github-ci")
    futures = {
        name: pool.submit(getter, installation_id, owner, repo_name, head_sha)
        for name, getter in sources
    }
    deadline = time.monotonic() + 10
    try:
        for name, future in futures.items():
            try:
                result = future.result(timeout=max(0.01, deadline - time.monotonic()))
                if name == "check_suite":
                    suites.extend(result)
                else:
                    checks.extend(result)
            except Exception as exc:
                errors.append(f"{name}: {exc}")
        return checks, suites, errors
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _upsert_ci_execution_records(
    db: Session,
    revision: m.CIRevision,
    discovered: list[dict],
    head_sha: str,
    *,
    remove_missing: bool = True,
) -> None:
    discovered_keys = set()
    for item in discovered:
        if item.get("sha") not in (None, head_sha):
            continue
        kind = item.get("kind") or "check_run"
        identity = str(item.get("identity") or item.get("id") or "")
        if not identity:
            identity = hashlib.sha256(
                json.dumps(item, sort_keys=True).encode("utf-8")
            ).hexdigest()
        discovered_keys.add((kind, identity))
        row = db.query(m.CIExecution).filter(
            m.CIExecution.ci_revision_id == revision.id,
            m.CIExecution.kind == kind,
            m.CIExecution.identity == identity,
        ).first()
        if row is None:
            row = m.CIExecution(
                ci_revision_id=revision.id,
                kind=kind,
                identity=identity,
                head_sha=head_sha,
            )
            db.add(row)
        row.provider = item.get("provider")
        row.context = item.get("context")
        row.name = item.get("name")
        row.status = item.get("status")
        row.conclusion = item.get("conclusion")
        row.external_url = item.get("url") or item.get("html_url")
    if remove_missing:
        existing = db.query(m.CIExecution).filter(
            m.CIExecution.ci_revision_id == revision.id
        ).all()
        for row in existing:
            if (row.kind, row.identity) not in discovered_keys:
                db.delete(row)


def process_ci_refresh_job(db: Session, payload: dict) -> None:
    repo = db.query(m.Repository).filter(m.Repository.id == payload["repository_id"]).first()
    pr = db.query(m.PullRequest).filter(m.PullRequest.id == payload["pull_request_id"]).first()
    if not repo or not pr or pr.head_sha != payload["head_sha"] or pr.state != "open":
        return
    revision = _ci_revision(db, repo, pr, payload["head_sha"])
    reason = payload.get("reason", "webhook")
    if reason != "poll":
        revision.retry_count = 0
    db.commit()
    try:
        owner, repo_name = repo.full_name.split("/", 1)
        checks, suites, errors = _fetch_ci_checks(
            payload["installation_id"], owner, repo_name, payload["head_sha"]
        )
    except Exception as exc:
        checks, suites, errors = [], [], [str(exc)]

    if errors:
        _upsert_ci_execution_records(
            db, revision, checks + suites, payload["head_sha"], remove_missing=False
        )
        revision.error = "; ".join(errors)[:4000]
        revision.retry_count += 1
        revision.state = "UNAVAILABLE"
        db.commit()
        _queue_review_status_update(db, repo, pr, payload["head_sha"])
        db.commit()
        _queue_ci_notifications(db, repo, pr, revision)
        if revision.retry_count < settings.CI_API_MAX_RETRIES:
            delay = min(5 * (2 ** (revision.retry_count - 1)), 120)
            enqueue_job(
                db, "CI_REFRESH",
                f"ci-retry:{revision.id}:{revision.retry_count}",
                {**payload, "reason": "retry"},
                datetime.utcnow() + timedelta(seconds=delay),
            )
            db.commit()
        return

    revision.error = None
    revision.retry_count = 0
    discovered = checks + suites
    _upsert_ci_execution_records(db, revision, discovered, payload["head_sha"])
    db.flush()
    records = _ci_records(db, revision)
    evaluated = ci_status.evaluate_ci_status(records)
    revision.state = evaluated.state
    revision.updated_at = datetime.utcnow()
    db.commit()
    _queue_review_status_update(db, repo, pr, payload["head_sha"])
    db.commit()

    if evaluated.state == "IN_PROGRESS" and revision.poll_count < settings.CI_FALLBACK_MAX_POLLS:
        revision.poll_count += 1
        delay = min(settings.CI_FALLBACK_POLL_INTERVAL_SECONDS * (2 ** (revision.poll_count - 1)), 300)
        enqueue_job(
            db, "CI_REFRESH",
            f"ci-poll:{revision.id}:{revision.poll_count}",
            {**payload, "reason": "poll"},
            datetime.utcnow() + timedelta(seconds=delay),
        )
        db.commit()

    _queue_ci_notifications(db, repo, pr, revision)


def process_review_status_job(db: Session, payload: dict) -> None:
    repo = db.query(m.Repository).filter(m.Repository.id == payload["repository_id"]).first()
    pr = db.query(m.PullRequest).filter(m.PullRequest.id == payload["pull_request_id"]).first()
    if not repo or not pr:
        raise ValueError("Review status job references a missing repository or pull request.")
    with _revision_finalization_lock(db, repo.id, pr.github_pr_number):
        db.expire_all()
        repo = db.query(m.Repository).filter(m.Repository.id == payload["repository_id"]).first()
        pr = db.query(m.PullRequest).filter(m.PullRequest.id == payload["pull_request_id"]).first()
        if not repo or not pr:
            return
        _publish_review_status_comment(db, payload, repo, pr)


def _publish_review_status_comment(
    db: Session, payload: dict, repo: m.Repository, pr: m.PullRequest
) -> None:
    if pr.head_sha != payload["head_sha"] or pr.state != "open":
        return
    revision = db.query(m.CIRevision).filter(
        m.CIRevision.repository_id == repo.id,
        m.CIRevision.pull_request_id == pr.id,
        m.CIRevision.head_sha == payload["head_sha"],
    ).first()
    records = _ci_records(db, revision) if revision else []
    ci_summary = ci_status.evaluate_ci_status(
        records,
        fetch_error=revision.error
        if revision and revision.state == "UNAVAILABLE" else None,
        discovery_complete=bool(revision and revision.state != "UNKNOWN"),
    )
    executions = [record for record in records if record["kind"] != "check_suite"]
    review = db.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == payload["head_sha"],
        m.Review.is_reevaluation == False,  # noqa: E712
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
    ).first()
    findings = (
        [
            {
                "severity": finding.severity,
                "title": finding.title,
                "description": finding.description,
                "file_path": finding.file_path,
                "line_reference": finding.line_reference,
            }
            for finding in db.query(m.Finding).filter(m.Finding.review_id == review.id).all()
        ]
        if review else []
    )
    marker = f"<!-- securepr-status:{pr.id}:{payload['head_sha']} -->"
    body = format_review_status_comment(
        pr_number=pr.github_pr_number,
        head_sha=payload["head_sha"],
        ci_state=ci_summary.state,
        ci_checks=executions,
        ci_error=ci_summary.error,
        review=review,
        findings=findings,
        marker=marker,
    )
    owner, repo_name = repo.full_name.split("/", 1)
    waiting_publication = _publication(
        db, repo, pr, payload["head_sha"], "WAITING", marker
    )
    result = github_client.post_issue_comment(
        payload["installation_id"],
        owner,
        repo_name,
        pr.github_pr_number,
        body,
        idempotency_marker=marker,
    )
    _mark_publication_published(db, waiting_publication, result)
    db.add(m.GithubAction(
        review_id=review.id if review else None,
        action_type="update_review_status_comment",
        request_payload={"head_sha": payload["head_sha"], "marker": marker},
        response_status=result.get("status_code"),
        response_payload=result.get("payload")
        if isinstance(result.get("payload"), dict) else None,
        succeeded=result.get("succeeded", False),
    ))
    db.commit()
    if not result.get("succeeded", False):
        raise RuntimeError(
            f"GitHub rejected review status publication for PR #{pr.github_pr_number}."
        )


def process_ci_notification_job(db: Session, payload: dict) -> None:
    # Kept as a compatibility no-op: CI failure updates are not separate bot messages.
    return
    execution = db.query(m.CIExecution).filter(
        m.CIExecution.ci_revision_id == revision.id,
        m.CIExecution.identity == payload["identity"],
        m.CIExecution.kind != "check_suite",
    ).first()
    if not execution:
        return
    review = db.query(m.Review).filter(
        m.Review.pull_request_id == pr.id,
        m.Review.head_sha == payload["head_sha"],
        m.Review.is_ci_wait_placeholder == False,  # noqa: E712
        m.Review.github_action_confirmed == True,  # noqa: E712
    ).first()
    if not review:
        return
    owner, repo_name = repo.full_name.split("/", 1)
    check_name = execution.name or execution.context or "Unnamed check"
    url = execution.external_url or ""
    marker = f"<!-- securepr-ci-failure:{revision.id}:{execution.identity} -->"
    body = (
        f"## SecurePR CI update — failure detected\n\n"
        f"The AI review for revision `{payload['head_sha']}` is already published. "
        f"CI monitoring discovered a terminal failure; the AI analysis was not rerun.\n\n"
        f"- **Check:** `{check_name}`\n"
        f"- **Provider:** `{execution.provider or 'Unknown provider'}`\n"
        f"- **Conclusion:** `{execution.conclusion or 'unknown'}`\n"
        f"- **Details:** {url if url.startswith('https://') else 'No external check URL provided.'}\n\n"
        f"CI state and review state remain separately available in the SecurePR dashboard.\n\n{marker}"
    )
    result = github_client.post_issue_comment(
        repo.installation_id,
        owner,
        repo_name,
        pr.github_pr_number,
        body,
        idempotency_marker=marker,
    )
    db.add(m.GithubAction(
        review_id=review.id, action_type="post_ci_failure_notification",
        request_payload={"identity": execution.identity},
        response_status=result.get("status_code"),
        response_payload=result.get("payload")
        if isinstance(result.get("payload"), dict) else None,
        succeeded=result.get("succeeded", False),
    ))
    db.commit()
    if not result.get("succeeded", False):
        raise RuntimeError(
            f"GitHub rejected CI failure notification for execution {execution.identity}."
        )
