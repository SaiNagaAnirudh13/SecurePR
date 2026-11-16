from types import SimpleNamespace

from app.integrations.gemini_client import GeminiClient


def test_gemini_generation_uses_configured_timeout():
    calls = {}

    class FakeModel:
        def generate_content(self, prompt, request_options):
            calls["prompt"] = prompt
            calls["request_options"] = request_options
            return SimpleNamespace(text="{}")

    client = GeminiClient()
    client.settings = SimpleNamespace(GEMINI_REQUEST_TIMEOUT_SECONDS=17)
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())

    assert client.score_dimensions([]) == {}
    assert calls["request_options"] == {"timeout": 17}


def test_gemini_timeout_returns_to_deterministic_fallback():
    class FakeModel:
        def generate_content(self, prompt, request_options):
            raise TimeoutError("test timeout")

    client = GeminiClient()
    client.settings = SimpleNamespace(GEMINI_REQUEST_TIMEOUT_SECONDS=17)
    client._configured = True
    client._model_name = "test-model"
    client._genai = SimpleNamespace(GenerativeModel=lambda name: FakeModel())

    assert client.score_dimensions([]) is None
