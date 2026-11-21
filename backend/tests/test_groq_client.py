import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from app.integrations import groq_client as groq_module
from app.integrations.ai_provider import (
    AIAnalysisError,
    AIInvalidResponse,
    AIUnavailable,
)
from app.integrations.groq_client import GroqClient, _retry_after_seconds

UTC_TZ = timezone(timedelta(0))


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


class FakeResponse:
    def __init__(self, status_code=200, *, content=None, headers=None, error=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._content = content
        self._error = error

    def json(self):
        if self._error:
            return {"error": self._error}
        return {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps(self._content)},
                }
            ],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45},
        }


def configure(monkeypatch, response, *, api_key="test-key", model="test-model"):
    calls = []

    class FakeClient:
        def __init__(self, timeout):
            calls.append({"timeout": timeout})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url, *, headers, json):
            calls[-1].update(url=url, headers=headers, json=json)
            return response

    monkeypatch.setattr(groq_module.httpx, "Client", FakeClient)
    client = GroqClient()
    client.settings = SimpleNamespace(GROQ_API_KEY=api_key, GROQ_MODEL=model)
    return client, calls


def test_groq_uses_configured_model_strict_schema_and_records_usage(monkeypatch):
    client, calls = configure(monkeypatch, FakeResponse(content=valid_output()))

    result = client.score_dimensions(
        [{"file_path": "app.py", "change_type": "modified", "patch_excerpt": "+x"}]
    )

    assert result.analysis == valid_output()
    assert (result.provider, result.model) == ("groq", "test-model")
    assert (result.input_tokens, result.output_tokens) == (123, 45)
    request = calls[0]["json"]
    assert calls[0]["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert request["model"] == "test-model"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert (
        request["response_format"]["json_schema"]["schema"]["additionalProperties"]
        is False
    )
    assert calls[0]["headers"] == {"Authorization": "Bearer test-key"}


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(content=[]),
        FakeResponse(content={}),
        FakeResponse(
            content=valid_output()
            | {"data_security": {"score": 101, "rationale": "out of range"}}
        ),
        FakeResponse(
            content=valid_output()
            | {"data_security": {"score": float("nan"), "rationale": "not finite"}}
        ),
        FakeResponse(
            content=valid_output()
            | {"data_security": {"score": "high", "rationale": "wrong type"}}
        ),
    ],
)
def test_groq_rejects_malformed_or_out_of_range_output(monkeypatch, response):
    client, _ = configure(monkeypatch, response)

    with pytest.raises(AIAnalysisError):
        client.score_dimensions([])


def test_groq_rejects_finding_outside_changed_files(monkeypatch):
    output = valid_output()
    output["findings"] = [
        {
            "dimension": "data_security",
            "severity": "HIGH",
            "title": "Finding",
            "description": "A meaningful finding.",
            "file_path": "other.py",
            "line_reference": None,
            "is_mandatory_blocker": False,
        }
    ]
    client, _ = configure(monkeypatch, FakeResponse(content=output))

    with pytest.raises(AIInvalidResponse):
        client.score_dimensions([{"file_path": "actual.py"}])


def test_groq_retries_short_rate_limit_and_honors_retry_after(monkeypatch):
    responses = iter(
        [
            FakeResponse(
                status_code=429,
                headers={"Retry-After": "1"},
                error={"message": "rate limited"},
            ),
            FakeResponse(content=valid_output()),
        ]
    )
    calls = []

    class FakeClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            calls.append(True)
            return next(responses)

    sleeps = []
    monkeypatch.setattr(groq_module.httpx, "Client", FakeClient)
    monkeypatch.setattr(groq_module.time, "sleep", sleeps.append)
    client = GroqClient()
    client.settings = SimpleNamespace(GROQ_API_KEY="test-key", GROQ_MODEL="test-model")

    client.score_dimensions([])

    assert len(calls) == 2
    assert sleeps == [1.0]


def test_groq_does_not_retry_daily_quota_exhaustion(monkeypatch):
    calls = []
    client, _ = configure(
        monkeypatch,
        FakeResponse(
            status_code=429,
            error={"code": "rate_limit_exceeded", "message": "Daily limit reached"},
        ),
    )
    original = groq_module.httpx.Client

    class OnceClient(original):
        def post(self, *args, **kwargs):
            calls.append(True)
            return super().post(*args, **kwargs)

    monkeypatch.setattr(groq_module.httpx, "Client", OnceClient)

    with pytest.raises(AIUnavailable) as error:
        client.score_dimensions([])

    assert error.value.error_code == "GROQ_QUOTA_EXCEEDED"
    assert error.value.retryable_at is None
    assert len(calls) == 1


def test_groq_long_retry_after_is_deferred(monkeypatch):
    client, calls = configure(
        monkeypatch,
        FakeResponse(
            status_code=429,
            headers={"Retry-After": "3600"},
            error={"message": "rate limited"},
        ),
    )

    with pytest.raises(AIUnavailable) as error:
        client.score_dimensions([])

    assert error.value.retryable_at is not None
    assert (
        3599
        <= (
            error.value.retryable_at - datetime.now(UTC_TZ).replace(tzinfo=None)
        ).total_seconds()
        <= 3600
    )
    assert len(calls) == 1


def test_retry_after_accepts_http_date_and_groq_reset_header():
    class Headers(dict):
        pass

    http_date = format_datetime(datetime.now(UTC_TZ) + timedelta(seconds=90))
    retry_after = _retry_after_seconds(Headers({"Retry-After": http_date}))
    groq_reset = _retry_after_seconds(Headers({"x-ratelimit-reset-requests": "1m30s"}))

    assert retry_after is not None and 85 <= retry_after <= 90
    assert groq_reset == 90


def test_groq_missing_credentials_fails_closed(monkeypatch):
    client, calls = configure(
        monkeypatch, FakeResponse(content=valid_output()), api_key=""
    )

    with pytest.raises(AIUnavailable) as error:
        client.score_dimensions([])

    assert error.value.error_code == "GROQ_CONFIGURATION_MISSING"
    assert calls == []


def test_groq_network_timeout_is_classified(monkeypatch):
    class FailingClient:
        def __init__(self, timeout):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(groq_module.httpx, "Client", FailingClient)
    monkeypatch.setattr(groq_module.time, "sleep", lambda _: None)
    client = GroqClient()
    client.settings = SimpleNamespace(GROQ_API_KEY="test-key", GROQ_MODEL="test-model")

    with pytest.raises(AIUnavailable) as error:
        client.score_dimensions([])

    assert error.value.error_code == "GROQ_REQUEST_TIMEOUT"
    assert error.value.retryable_at is not None
