"""SQLAlchemy source-of-truth models for PostgreSQL and the SQL Server fallback."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def gen_uuid() -> str:
    return str(uuid.uuid4())


class DecisionStatus(str, enum.Enum):
    APPROVED = "APPROVED"
    BLOCKED = "BLOCKED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    REVIEW_UNAVAILABLE = "REVIEW_UNAVAILABLE"
    WAITING_FOR_AI = "WAITING_FOR_AI"
    WAITING_FOR_CI = "WAITING_FOR_CI"
    PENDING = "PENDING"


class ImpactConfidence(str, enum.Enum):
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    UNRESOLVED = "UNRESOLVED"


class CircuitScope(str, enum.Enum):
    GLOBAL = "GLOBAL"
    REPOSITORY = "REPOSITORY"
    USER = "USER"


class User(Base):
    __tablename__ = "users"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    github_login = Column(String(255), unique=True, nullable=False)
    github_user_id = Column(String(64), unique=True, nullable=True)
    display_name = Column(String(255))
    email = Column(String(255))
    is_repo_owner = Column(
        Boolean, default=False, nullable=False, server_default=false()
    )
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )

    approval_eligibility = relationship(
        "AutoApprovalEligibility", back_populates="user"
    )


class Repository(Base):
    __tablename__ = "repositories"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    github_repo_id = Column(String(64), unique=True, nullable=False)
    owner_login = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)
    full_name = Column(String(512), unique=True, nullable=False)
    default_branch = Column(
        String(255), default="main", nullable=False, server_default=text("'main'")
    )
    installation_id = Column(String(64))
    is_active = Column(Boolean, default=True, nullable=False, server_default=true())
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )

    pull_requests = relationship("PullRequest", back_populates="repository")
    policies = relationship("Policy", back_populates="repository")
    circuit_breakers = relationship("CircuitBreaker", back_populates="repository")


class PullRequest(Base):
    __tablename__ = "pull_requests"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    repository_id = Column(String(36), ForeignKey("repositories.id"), nullable=False)
    github_pr_number = Column(Integer, nullable=False)
    title = Column(String(1024))
    author_login = Column(String(255))
    head_sha = Column(String(64))
    base_branch = Column(String(255))
    head_branch = Column(String(255))
    state = Column(
        String(32), default="open", nullable=False, server_default=text("'open'")
    )
    ai_state = Column(
        String(32),
        default="AI_UNAVAILABLE",
        nullable=False,
        server_default=text("'AI_UNAVAILABLE'"),
    )
    ai_error_code = Column(String(64), nullable=True)
    ai_retryable_at = Column(DateTime, nullable=True)
    ai_attempt_id = Column(String(36), nullable=True)
    merged_at = Column(DateTime, nullable=True)
    merge_commit_sha = Column(String(64), nullable=True)
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("repository_id", "github_pr_number", name="uq_repo_pr_number"),
        Index("ix_pull_requests_repository_id", "repository_id"),
    )

    repository = relationship("Repository", back_populates="pull_requests")
    reviews = relationship("Review", back_populates="pull_request")
    commits = relationship("Commit", back_populates="pull_request")


class Commit(Base):
    __tablename__ = "commits"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    pull_request_id = Column(String(36), ForeignKey("pull_requests.id"), nullable=False)
    sha = Column(String(64), nullable=False)
    message = Column(Text)
    author_login = Column(String(255))
    committed_at = Column(DateTime)

    __table_args__ = (Index("ix_commits_pr", "pull_request_id"),)

    pull_request = relationship("PullRequest", back_populates="commits")


class Policy(Base):
    __tablename__ = "policies"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    repository_id = Column(
        String(36), ForeignKey("repositories.id"), nullable=True
    )  # NULL = global
    name = Column(String(255), nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default=text("1"))
    content_json = Column(JSON, nullable=False)  # full policy document snapshot
    is_active = Column(
        Boolean, default=True, nullable=False, server_default=text("true")
    )
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )

    repository = relationship("Repository", back_populates="policies")


class CircuitBreaker(Base):
    __tablename__ = "circuit_breakers"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    scope = Column(
        SAEnum(
            CircuitScope,
            native_enum=False,
            create_constraint=True,
            name="ck_circuit_breakers_scope",
        ),
        nullable=False,
    )
    repository_id = Column(String(36), ForeignKey("repositories.id"), nullable=True)
    user_login = Column(String(255), nullable=True)
    is_tripped = Column(Boolean, default=False, nullable=False, server_default=false())
    reason = Column(String(1024))
    tripped_by = Column(String(255))
    tripped_at = Column(DateTime)
    auto_resume_at = Column(DateTime, nullable=True)  # temporary suspension
    resumed_at = Column(DateTime, nullable=True)

    repository = relationship("Repository", back_populates="circuit_breakers")


class CircuitBreakerEvent(Base):
    """Audit history of every trip/resume action."""

    __tablename__ = "circuit_breaker_events"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    circuit_breaker_id = Column(
        String(36), ForeignKey("circuit_breakers.id"), nullable=False
    )
    action = Column(String(32))  # TRIP | RESUME | AUTO_RESUME
    actor = Column(String(255))
    reason = Column(String(1024))
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )


class AutoApprovalEligibility(Base):
    __tablename__ = "auto_approval_eligibility"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    repository_id = Column(String(36), ForeignKey("repositories.id"), nullable=False)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False)
    is_enabled = Column(Boolean, default=False, nullable=False, server_default=false())
    max_risk_threshold = Column(
        Integer, default=39, nullable=False, server_default=text("39")
    )
    policy_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    updated_by = Column(String(255))
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("repository_id", "user_id", name="uq_repo_user_eligibility"),
    )

    user = relationship("User", back_populates="approval_eligibility")


class Review(Base):
    """One immutable review record per analysis run (re-evaluations create new rows)."""

    __tablename__ = "reviews"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    pull_request_id = Column(String(36), ForeignKey("pull_requests.id"), nullable=False)
    policy_id = Column(String(36), ForeignKey("policies.id"), nullable=True)
    head_sha = Column(String(64), nullable=False)
    is_reevaluation = Column(
        Boolean, default=False, nullable=False, server_default=false()
    )
    superseded_review_id = Column(String(36), ForeignKey("reviews.id"), nullable=True)
    # Legacy columns remain readable for historical reviews; new executions never
    # create CI-wait placeholders.
    is_ci_wait_placeholder = Column(
        Boolean, default=False, nullable=False, server_default=false()
    )
    ci_wait_started_at = Column(DateTime, nullable=True)
    ci_wait_pending_checks = Column(
        JSON, nullable=True
    )  # list[str] snapshot at wait time

    score_change_surface = Column(
        Float, default=0, nullable=False, server_default=text("0")
    )
    score_reversibility = Column(
        Float, default=0, nullable=False, server_default=text("0")
    )
    score_data_security = Column(
        Float, default=0, nullable=False, server_default=text("0")
    )
    score_operational = Column(
        Float, default=0, nullable=False, server_default=text("0")
    )
    score_verification = Column(
        Float, default=0, nullable=False, server_default=text("0")
    )
    score_policy_compliance = Column(
        Float, default=0, nullable=False, server_default=text("0")
    )
    overall_score = Column(Float, default=0, nullable=False, server_default=text("0"))
    risk_category = Column(
        String(16),
        default="NOT_EVALUATED",
        nullable=False,
        server_default=text("'NOT_EVALUATED'"),
    )

    decision = Column(
        SAEnum(
            DecisionStatus,
            native_enum=False,
            create_constraint=True,
            name="ck_reviews_decision",
            length=40,
        ),
        default=DecisionStatus.PENDING,
        nullable=False,
        server_default=text("'PENDING'"),
    )
    decision_reasons = Column(JSON)  # list[str]
    lifecycle_state = Column(
        String(40),
        default="RECEIVED",
        nullable=False,
        server_default=text("'RECEIVED'"),
    )
    ai_state = Column(
        String(32),
        default="AI_UNAVAILABLE",
        nullable=False,
        server_default=text("'AI_UNAVAILABLE'"),
    )
    ai_error_code = Column(String(64), nullable=True)
    ai_retryable_at = Column(DateTime, nullable=True)
    ai_attempt_id = Column(String(36), nullable=True)
    ai_provider = Column(String(32), nullable=True)
    ai_model = Column(String(128), nullable=True)
    ai_input_tokens = Column(Integer, nullable=True)
    ai_output_tokens = Column(Integer, nullable=True)
    ci_verified_at = Column(DateTime, nullable=True)
    circuit_breaker_active = Column(
        Boolean, default=False, nullable=False, server_default=false()
    )
    github_comment_url = Column(String(1024))
    github_review_state = Column(String(32))  # APPROVE | COMMENT | REQUEST_CHANGES
    github_action_confirmed = Column(
        Boolean, default=False, nullable=False, server_default=false()
    )
    # Kept separate from the immutable SecurePR decision: GitHub may reject a
    # merge for branch-protection, permissions, or a race after approval.
    auto_merge_status = Column(String(48), nullable=True)
    auto_merge_policy_revision: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    auto_merge_attempted_at = Column(DateTime, nullable=True)
    auto_merged_at = Column(DateTime, nullable=True)
    merge_commit_sha = Column(String(64), nullable=True)
    auto_merge_failure_reason = Column(Text, nullable=True)

    analysis_started_at = Column(DateTime, default=datetime.utcnow)
    analysis_completed_at = Column(DateTime)
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )

    # One webhook-driven wait row and one webhook-driven final row are allowed
    # per PR revision. Explicit dashboard re-evaluations use
    # is_reevaluation=True and retain their immutable history separately.
    __table_args__ = (
        UniqueConstraint(
            "pull_request_id",
            "head_sha",
            "is_ci_wait_placeholder",
            "is_reevaluation",
            name="uq_review_pr_sha_phase",
        ),
        CheckConstraint(
            "risk_category IN ('NOT_EVALUATED', 'LOW', 'MEDIUM', 'HIGH')",
            name="ck_reviews_category",
        ),
        Index("ix_reviews_pr", "pull_request_id"),
        Index("ix_reviews_decision", "decision"),
    )

    pull_request = relationship("PullRequest", back_populates="reviews")
    findings = relationship("Finding", back_populates="review")
    evidence_items = relationship("Evidence", back_populates="review")
    changed_files = relationship("ChangedFile", back_populates="review")
    impacts = relationship("ImpactPrediction", back_populates="review")


class BackgroundJob(Base):
    """Durable work queue for review and CI tasks."""

    __tablename__ = "background_jobs"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    job_type = Column(String(32), nullable=False)
    idempotency_key = Column(String(512), unique=True, nullable=False)
    payload = Column(JSON, nullable=False)
    state = Column(
        String(16), nullable=False, default="QUEUED", server_default=text("'QUEUED'")
    )
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    lease_token = Column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    available_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=func.now()
    )
    lease_until = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=func.now()
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_background_jobs_dispatch", "job_type", "state", "available_at"),
    )


class RevisionOperation(Base):
    """Lease for one operation on an immutable pull-request revision."""

    __tablename__ = "revision_operations"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    repository_id = Column(
        String(36), ForeignKey("repositories.id"), nullable=False
    )
    pull_request_id = Column(
        String(36), ForeignKey("pull_requests.id"), nullable=False
    )
    head_sha = Column(String(64), nullable=False)
    operation = Column(String(128), nullable=False)
    state = Column(
        String(16), nullable=False, default="QUEUED", server_default=text("'QUEUED'")
    )
    attempts = Column(Integer, nullable=False, default=0, server_default=text("0"))
    owner_token = Column(String(36), nullable=True)
    available_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, server_default=func.now()
    )
    lease_until = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "pull_request_id",
            "head_sha",
            "operation",
            name="uq_revision_operation_identity",
        ),
        Index("ix_revision_operations_lease", "state", "available_at", "lease_until"),
    )


class CIRevision(Base):
    """Latest CI discovery state for one immutable PR revision."""

    __tablename__ = "ci_revisions"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    repository_id = Column(String(36), ForeignKey("repositories.id"), nullable=False)
    pull_request_id = Column(String(36), ForeignKey("pull_requests.id"), nullable=False)
    head_sha = Column(String(64), nullable=False)
    state = Column(
        String(24), nullable=False, default="UNKNOWN", server_default=text("'UNKNOWN'")
    )
    error = Column(Text, nullable=True)
    required_checks = Column(
        JSON, nullable=False, default=list, server_default=text("'[]'")
    )
    discovery_complete = Column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    last_verified_at = Column(DateTime, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    poll_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_id", "pull_request_id", "head_sha", name="uq_ci_revision"
        ),
        Index("ix_ci_revision_pr_sha", "pull_request_id", "head_sha"),
    )


class CIExecution(Base):
    """A discovered check, suite, or legacy commit-status record."""

    __tablename__ = "ci_executions"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    ci_revision_id = Column(String(36), ForeignKey("ci_revisions.id"), nullable=False)
    identity = Column(String(255), nullable=False)
    kind = Column(String(24), nullable=False)
    provider = Column(String(255), nullable=True)
    app_id = Column(String(64), nullable=True)
    context = Column(String(512), nullable=True)
    name = Column(String(512), nullable=True)
    status = Column(String(32), nullable=True)
    conclusion = Column(String(32), nullable=True)
    external_url = Column(String(2048), nullable=True)
    head_sha = Column(String(64), nullable=False)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "ci_revision_id", "kind", "identity", name="uq_ci_execution_identity"
        ),
        Index("ix_ci_execution_revision", "ci_revision_id"),
    )


class ChangedFile(Base):
    __tablename__ = "changed_files"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    review_id = Column(String(36), ForeignKey("reviews.id"), nullable=False)
    file_path = Column(String(1024), nullable=False)
    change_type = Column(String(16))  # added|modified|removed|renamed
    additions = Column(Integer, default=0, nullable=False, server_default=text("0"))
    deletions = Column(Integer, default=0, nullable=False, server_default=text("0"))
    is_blocked_path = Column(
        Boolean, default=False, nullable=False, server_default=false()
    )
    patch_excerpt = Column(Text)  # truncated diff stored as evidence

    __table_args__ = (Index("ix_changed_files_review", "review_id"),)

    review = relationship("Review", back_populates="changed_files")


class Finding(Base):
    __tablename__ = "findings"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    review_id = Column(String(36), ForeignKey("reviews.id"), nullable=False)
    dimension = Column(String(64))  # which of the 6 risk dimensions
    severity = Column(String(16))  # LOW|MEDIUM|HIGH|CRITICAL
    title = Column(String(512))
    description = Column(Text)
    file_path = Column(String(1024))
    line_reference = Column(String(64))
    is_mandatory_blocker = Column(
        Boolean, default=False, nullable=False, server_default=false()
    )

    __table_args__ = (
        CheckConstraint(
            "severity IS NULL OR severity IN ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')",
            name="ck_findings_severity",
        ),
        Index("ix_findings_review", "review_id"),
    )

    review = relationship("Review", back_populates="findings")


class Evidence(Base):
    """Raw supporting evidence captured at analysis time (immutable)."""

    __tablename__ = "evidence"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    review_id = Column(String(36), ForeignKey("reviews.id"), nullable=False)
    evidence_type = Column(
        String(64)
    )  # code_snippet|ci_result|diff|policy_match|ai_rationale
    source = Column(String(255))
    content = Column(Text)
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_evidence_review", "review_id"),)

    review = relationship("Review", back_populates="evidence_items")


class ImpactPrediction(Base):
    __tablename__ = "impact_predictions"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    review_id = Column(String(36), ForeignKey("reviews.id"), nullable=False)
    component_type = Column(
        String(64)
    )  # function|api|service|module|database|dependency|test
    component_name = Column(String(512))
    confidence = Column(
        SAEnum(
            ImpactConfidence,
            native_enum=False,
            create_constraint=True,
            name="ck_impact_predictions_confidence",
        ),
        default=ImpactConfidence.INFERRED,
        nullable=False,
        server_default=text("'INFERRED'"),
    )
    evidence_summary = Column(Text)
    potential_consequence = Column(Text)

    __table_args__ = (Index("ix_impact_review", "review_id"),)

    review = relationship("Review", back_populates="impacts")


class GithubAction(Base):
    """Audit log of every outbound GitHub API call SecurePR makes."""

    __tablename__ = "github_actions"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    review_id = Column(String(36), ForeignKey("reviews.id"), nullable=True)
    action_type = Column(String(64))  # post_comment|submit_review|...
    request_payload = Column(JSON)
    response_status = Column(Integer)
    response_payload = Column(JSON)
    succeeded = Column(Boolean, default=False, nullable=False, server_default=false())
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_github_actions_review", "review_id"),)


class ReviewPublication(Base):
    """Durable, revision-scoped idempotency record for visible GitHub messages."""

    __tablename__ = "review_publications"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    repository_id = Column(String(36), ForeignKey("repositories.id"), nullable=False)
    pull_request_id = Column(String(36), ForeignKey("pull_requests.id"), nullable=False)
    head_sha = Column(String(64), nullable=False)
    message_type = Column(String(64), nullable=False)
    state = Column(
        String(16), nullable=False, default="PENDING", server_default=text("'PENDING'")
    )
    github_message_id = Column(String(64), nullable=True)
    marker = Column(String(255), nullable=True)
    published_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "repository_id",
            "pull_request_id",
            "head_sha",
            "message_type",
            name="uq_review_publication_revision_type",
        ),
        Index("ix_review_publications_pr_sha", "pull_request_id", "head_sha"),
    )


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    review_id = Column(String(36), ForeignKey("reviews.id"), nullable=True)
    recipient = Column(String(255))
    channel = Column(String(32))  # github|webhook
    subject = Column(String(512))
    body = Column(Text)
    delivered = Column(Boolean, default=False, nullable=False, server_default=false())
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(String(36), primary_key=True, default=gen_uuid)
    actor = Column(String(255))
    action = Column(String(255))
    target_type = Column(String(64))
    target_id = Column(String(36))
    details_json = Column(JSON)
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False, server_default=func.now()
    )
