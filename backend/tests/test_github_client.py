from app.core.config import Settings
from app.integrations import github_client as github_module


def test_github_app_jwt_uses_inline_private_key_and_normalizes_newlines(monkeypatch):
    settings = Settings(
        _env_file=None,
        GITHUB_APP_ID="test-app-id",
        GITHUB_PRIVATE_KEY="first-line\\nsecond-line",
        GITHUB_PRIVATE_KEY_PATH="missing-key.pem",
    )
    monkeypatch.setattr(github_module, "get_settings", lambda: settings)
    encoded = {}

    def fake_encode(payload, key, algorithm):
        encoded.update(payload=payload, key=key, algorithm=algorithm)
        return "test-token"

    monkeypatch.setattr(github_module.jwt, "encode", fake_encode)

    token = github_module.GitHubClient()._app_jwt()

    assert token == "test-token"
    assert encoded["key"] == "first-line\nsecond-line"
    assert encoded["algorithm"] == "RS256"


def test_github_app_jwt_uses_file_when_inline_key_is_unset(monkeypatch, tmp_path):
    key_path = tmp_path / "github-app.pem"
    key_path.write_text("test-only-file-key", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        GITHUB_APP_ID="test-app-id",
        GITHUB_PRIVATE_KEY="",
        GITHUB_PRIVATE_KEY_PATH=str(key_path),
    )
    monkeypatch.setattr(github_module, "get_settings", lambda: settings)
    encoded = {}
    monkeypatch.setattr(
        github_module.jwt,
        "encode",
        lambda payload, key, algorithm: encoded.update(key=key) or "test-token",
    )

    github_module.GitHubClient()._app_jwt()

    assert encoded["key"] == "test-only-file-key"
