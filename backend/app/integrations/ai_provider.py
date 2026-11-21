"""Provider-neutral types for validated pull-request analysis."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class AIAnalysisResult:
    analysis: dict[str, Any]
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class AIAnalysisError(Exception):
    """A safe, categorized error that must prevent an AI review decision."""

    ai_state = "AI_FAILED"
    error_code = "AI_ANALYSIS_FAILED"


class AIUnavailable(AIAnalysisError):
    ai_state = "AI_UNAVAILABLE"

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "GROQ_UNAVAILABLE",
        retryable_at: datetime | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.retryable_at = retryable_at


class AIInvalidResponse(AIAnalysisError):
    ai_state = "AI_INVALID_RESPONSE"
    error_code = "GROQ_INVALID_RESPONSE"


class ReviewProvider(Protocol):
    def score_dimensions(self, changed_files: list[dict]) -> AIAnalysisResult: ...
