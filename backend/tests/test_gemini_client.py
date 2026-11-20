import json
from types import SimpleNamespace

import pytest

from app.integrations.gemini_client import GeminiAnalysisError, GeminiClient


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
    } | {"summary": "test summary"}


def test_gemini_generation_uses_configured_timeout():
    calls = {}
    expected = valid_output()

    class FakeModel:
        def generate_content(self, prompt, request_options):
            calls["prompt"] = prompt
            calls["request_options"] = request_options
            return SimpleNamespace(text=json.dumps(expected))

    client = GeminiClient()
    client.settings = SimpleNamespace(GEMINI_REQUEST_TIMEOUT_SECONDS=17)
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())

    assert client.score_dimensions([]) == expected
    assert calls["request_options"] == {"timeout": 17}


def test_gemini_timeout_is_reported_as_analysis_failure():
    class FakeModel:
        def generate_content(self, prompt, request_options):
            raise TimeoutError(
                "504 Deadline expired before operation could complete"
            )

    client = GeminiClient()
    client.settings = SimpleNamespace(GEMINI_REQUEST_TIMEOUT_SECONDS=17)
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())

    with pytest.raises(GeminiAnalysisError, match="TimeoutError: 504 Deadline expired"):
        client.score_dimensions([])


@pytest.mark.parametrize(
    "response",
    [
        "[]",
        "{}",
        json.dumps(valid_output() | {
            "data_security": {"score": 101, "rationale": "out of range"},
        }),
        json.dumps(valid_output() | {
            "data_security": {"score": float("nan"), "rationale": "not finite"},
        }),
        json.dumps(valid_output() | {
            "data_security": {"score": "high", "rationale": "wrong type"},
        }),
    ],
)
def test_gemini_malformed_output_is_reported_as_analysis_failure(response):
    class FakeModel:
        def generate_content(self, prompt, request_options):
            return SimpleNamespace(text=response)

    client = GeminiClient()
    client.settings = SimpleNamespace(GEMINI_REQUEST_TIMEOUT_SECONDS=17)
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())

    with pytest.raises(GeminiAnalysisError):
        client.score_dimensions([])
