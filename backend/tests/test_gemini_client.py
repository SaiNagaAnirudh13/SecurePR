import json
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import ModuleType, SimpleNamespace

import pytest

from app.integrations.gemini_client import (
    GeminiAnalysisError,
    GeminiClient,
    GeminiInvalidResponse,
    GeminiUnavailable,
    _retry_after_seconds,
)


def valid_output():
    return {
        dimension: {"score": 20, "rationale": "test rationale"}
        for dimension in (
            "change_surface",
            "reversibility",
            "data_security",
            "operational",
            "verification",
            "policy_compliance",
        )
    } | {"summary": "test summary", "findings": []}


def test_gemini_generation_uses_configured_timeout():
    calls = {}
    expected = valid_output()

    class FakeModel:
        def generate_content(self, prompt, request_options):
            calls["prompt"] = prompt
            calls["request_options"] = request_options
            return SimpleNamespace(text=json.dumps(expected))

    client = GeminiClient()
    client.settings = SimpleNamespace(
        GEMINI_REQUEST_TIMEOUT_SECONDS=17, GEMINI_MAX_ATTEMPTS=1
    )
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())

    assert client.score_dimensions([]) == expected
    assert calls["request_options"] == {"timeout": 17}


def test_gemini_timeout_is_reported_as_analysis_failure():
    class FakeModel:
        def generate_content(self, prompt, request_options):
            raise TimeoutError("504 Deadline expired before operation could complete")

    client = GeminiClient()
    client.settings = SimpleNamespace(
        GEMINI_REQUEST_TIMEOUT_SECONDS=17, GEMINI_MAX_ATTEMPTS=1
    )
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())

    with pytest.raises(GeminiAnalysisError) as error:
        client.score_dimensions([])
    assert error.value.error_code == "GEMINI_REQUEST_TIMEOUT"


@pytest.mark.parametrize(
    "response",
    [
        "[]",
        "{}",
        json.dumps(
            valid_output()
            | {
                "data_security": {"score": 101, "rationale": "out of range"},
            }
        ),
        json.dumps(
            valid_output()
            | {
                "data_security": {"score": float("nan"), "rationale": "not finite"},
            }
        ),
        json.dumps(
            valid_output()
            | {
                "data_security": {"score": "high", "rationale": "wrong type"},
            }
        ),
    ],
)
def test_gemini_malformed_output_is_reported_as_analysis_failure(response):
    class FakeModel:
        def generate_content(self, prompt, request_options):
            return SimpleNamespace(text=response)

    client = GeminiClient()
    client.settings = SimpleNamespace(
        GEMINI_REQUEST_TIMEOUT_SECONDS=17, GEMINI_MAX_ATTEMPTS=1
    )
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())

    with pytest.raises(GeminiAnalysisError):
        client.score_dimensions([])


@pytest.mark.parametrize(
    "response",
    [
        json.dumps(valid_output() | {"findings": None}),
        json.dumps(valid_output() | {"findings": [{"severity": "URGENT"}]}),
        json.dumps(
            valid_output()
            | {
                "findings": [
                    {
                        "dimension": "data_security",
                        "severity": "HIGH",
                        "title": "Finding",
                        "description": "A meaningful finding.",
                        "file_path": "not-in-the-pr.txt",
                        "line_reference": None,
                        "is_mandatory_blocker": False,
                    }
                ]
            }
        ),
        json.dumps(
            valid_output() | {"data_security": {"score": 20, "rationale": "   "}}
        ),
    ],
)
def test_gemini_rejects_incomplete_findings_and_explanations(response):
    class FakeModel:
        def generate_content(self, prompt, request_options):
            return SimpleNamespace(text=response)

    client = GeminiClient()
    client.settings = SimpleNamespace(
        GEMINI_REQUEST_TIMEOUT_SECONDS=17, GEMINI_MAX_ATTEMPTS=1
    )
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())

    with pytest.raises(GeminiInvalidResponse):
        client.score_dimensions([{"file_path": "actual.py"}])


def test_gemini_quota_retries_are_bounded_and_classified(monkeypatch):
    attempts = []

    class QuotaError(Exception):
        status_code = 429

    class FakeModel:
        def generate_content(self, prompt, request_options):
            attempts.append(True)
            raise QuotaError("provider quota exhausted")

    client = GeminiClient()
    client.settings = SimpleNamespace(
        GEMINI_REQUEST_TIMEOUT_SECONDS=17, GEMINI_MAX_ATTEMPTS=2
    )
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())
    monkeypatch.setattr("app.integrations.gemini_client.time.sleep", lambda _: None)

    with pytest.raises(GeminiUnavailable) as error:
        client.score_dimensions([])

    assert error.value.error_code == "GEMINI_QUOTA_EXCEEDED"
    assert error.value.retryable_at is not None
    assert len(attempts) == 2
    assert "provider quota exhausted" not in str(error.value)


def test_gemini_long_retry_after_stops_retries_and_records_retry_time(monkeypatch):
    attempts = []

    class QuotaError(Exception):
        status_code = 429

        def __init__(self, message):
            super().__init__(message)
            self.headers = {"Retry-After": "3600"}

    class FakeModel:
        def generate_content(self, prompt, request_options):
            attempts.append(True)
            raise QuotaError("provider quota exhausted")

    client = GeminiClient()
    client.settings = SimpleNamespace(
        GEMINI_REQUEST_TIMEOUT_SECONDS=17, GEMINI_MAX_ATTEMPTS=3
    )
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())
    monkeypatch.setattr("app.integrations.gemini_client.time.sleep", lambda _: None)

    with pytest.raises(GeminiUnavailable) as error:
        client.score_dimensions([])

    assert error.value.error_code == "GEMINI_QUOTA_EXCEEDED"
    assert error.value.retryable_at is not None
    now = datetime.fromtimestamp(time.time(), timezone.utc).replace(  # noqa: UP017
        tzinfo=None
    )
    assert 3599 <= (error.value.retryable_at - now).total_seconds() <= 3600
    assert len(attempts) == 1


def test_gemini_retry_after_accepts_http_date():
    class QuotaError(Exception):
        def __init__(self):
            self.headers = {
                "Retry-After": format_datetime(
                    datetime.fromtimestamp(time.time(), timezone.utc)  # noqa: UP017
                    + timedelta(seconds=90)
                )
            }

    retry_after = _retry_after_seconds(QuotaError())

    assert retry_after is not None
    assert 85 <= retry_after <= 90


def test_gemini_does_not_fall_back_to_another_model(monkeypatch):
    configured = []
    fake_genai = SimpleNamespace(
        configure=lambda **kwargs: configured.append(kwargs),
        GenerativeModel=lambda name: name,
    )
    fake_google = ModuleType("google")
    fake_google.__path__ = []
    fake_google.generativeai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)
    client = GeminiClient()
    client.settings = SimpleNamespace(
        GEMINI_API_KEY="test-key",
        GEMINI_MODEL="approved-model",
        GEMINI_REQUEST_TIMEOUT_SECONDS=17,
    )

    client._ensure_configured()

    assert client._model_name == "approved-model"
    assert configured == [{"api_key": "test-key"}]


def test_gemini_missing_credentials_are_a_configuration_failure():
    client = GeminiClient()
    client.settings = SimpleNamespace(GEMINI_API_KEY="")

    with pytest.raises(GeminiUnavailable) as error:
        client.score_dimensions([])

    assert error.value.error_code == "GEMINI_CONFIGURATION_MISSING"
