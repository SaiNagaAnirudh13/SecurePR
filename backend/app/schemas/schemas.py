from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


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
    user_login: str = Field(min_length=1, max_length=39)
    is_enabled: bool
    max_risk_threshold: int = Field(default=39, ge=0, le=100)


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
