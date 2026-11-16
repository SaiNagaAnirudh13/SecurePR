import pytest
from app.core.config import Settings
from app.integrations import github_client as github_module


class FakeResponse:
    def __init__(self, payload, error=None):
        self.payload = payload
        self.error = error

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


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


def test_installation_access_token_is_cached_until_near_expiry(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
    calls = []
    monkeypatch.setattr(
        github_module.requests,
        "post",
        lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or FakeResponse({"token": "installation-token"})
        ),
    )
    monkeypatch.setattr(github_module.time, "monotonic", lambda: 10)

    assert client._installation_token("42") == "installation-token"
    assert client._installation_token("42") == "installation-token"
    assert len(calls) == 1


def test_installation_access_token_is_refreshed_after_cache_expiry(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_app_jwt", lambda: "app-jwt")
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse({"token": f"installation-token-{len(calls)}"})

    monkeypatch.setattr(github_module.requests, "post", fake_post)
    now = [10]
    monkeypatch.setattr(github_module.time, "monotonic", lambda: now[0])

    assert client._installation_token("42") == "installation-token-1"
    now[0] += github_module.INSTALLATION_TOKEN_CACHE_SECONDS + 1
    assert client._installation_token("42") == "installation-token-2"
    assert len(calls) == 2


def test_check_runs_are_fetched_from_every_page(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id: {})
    checks = [
        {"name": f"check-{index}", "status": "completed", "conclusion": "success"}
        for index in range(101)
    ]
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params)
        start = (params["page"] - 1) * 100
        return FakeResponse({"check_runs": checks[start : start + 100]})

    monkeypatch.setattr(github_module.requests, "get", fake_get)

    result = client.get_check_runs("42", "octo", "widgets", "abc")

    assert len(result) == 101
    assert [call["page"] for call in calls] == [1, 2]
    assert all(call["per_page"] == 100 for call in calls)


def test_commit_statuses_are_fetched_from_every_page(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id: {})
    statuses = [
        {"context": f"status-{index}", "state": "success"} for index in range(101)
    ]
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params)
        start = (params["page"] - 1) * 100
        return FakeResponse({"statuses": statuses[start : start + 100]})

    monkeypatch.setattr(github_module.requests, "get", fake_get)

    result = client.get_commit_statuses("42", "octo", "widgets", "abc")

    assert len(result) == 101
    assert [call["page"] for call in calls] == [1, 2]
    assert all(status["conclusion"] == "success" for status in result)


def test_pr_file_response_is_paginated_and_preserves_readme_files(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id: {})
    files = [
        {"filename": f"file-{index}.txt", "status": "modified"} for index in range(100)
    ]
    files.append({"filename": "README.md", "status": "modified", "additions": 1})
    calls = []
    urls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params)
        urls.append(url)
        start = (params["page"] - 1) * 100
        return FakeResponse(files[start : start + 100])

    monkeypatch.setattr(github_module.requests, "get", fake_get)

    result = client.get_pr_files("42", "octo", "widgets", 7)

    assert len(result) == 101
    assert result[-1]["file_path"] == "README.md"
    assert [call["page"] for call in calls] == [1, 2]
    assert all(url.endswith("/repos/octo/widgets/pulls/7/files") for url in urls)


def test_pr_file_api_error_is_raised_not_returned_as_empty_list(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id: {})
    monkeypatch.setattr(
        github_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            [], github_module.requests.HTTPError("forbidden")
        ),
    )

    with pytest.raises(github_module.requests.HTTPError):
        client.get_pr_files("42", "octo", "widgets", 7)


def test_malformed_pr_file_payload_is_rejected(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id: {})
    monkeypatch.setattr(
        github_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"message": "unexpected response"}),
    )

    with pytest.raises(ValueError, match="list of objects"):
        client.get_pr_files("42", "octo", "widgets", 7)
