from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class ReviewSummary(BaseModel):
    id: str
    pull_request_id: str
    overall_score: float
    risk_category: str
    decision: str
    is_reevaluation: bool
    created_at: datetime

    class Config:
        from_attributes = True


class EligibilityUpdate(BaseModel):
    repository_id: str
    user_login: str
    is_enabled: bool
    max_risk_threshold: int = 39
    updated_by: str = "dashboard"


class CircuitBreakerTripRequest(BaseModel):
    scope: str  # GLOBAL | REPOSITORY | USER
    repository_id: Optional[str] = None
    user_login: Optional[str] = None
    reason: str
    actor: str = "dashboard"
    suspend_minutes: Optional[int] = None


class CircuitBreakerResumeRequest(BaseModel):
    scope: str
    repository_id: Optional[str] = None
    user_login: Optional[str] = None
    actor: str = "dashboard"


class ReevaluateRequest(BaseModel):
    pull_request_id: str
    actor: str = "dashboard"


class RepositoryAutoMergeUpdate(BaseModel):
    repository_id: str
    is_enabled: bool
