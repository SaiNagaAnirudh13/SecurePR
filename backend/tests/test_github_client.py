import pytest

from app.core.config import Settings
from app.integrations import github_client as github_module


class FakeResponse:
    def __init__(self, payload, error=None, status_code=200, headers=None):
        self.payload = payload
        self.error = error
        self.status_code = status_code
        self.headers = headers or {}
        self.ok = status_code < 400
        self.text = str(payload)

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
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
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
    assert result[0]["kind"] == "check_run"
    assert result[0]["identity"] is None
    assert result[0]["sha"] == "abc"


def test_commit_statuses_are_fetched_from_every_page(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
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
    assert result[0]["kind"] == "commit_status"
    assert result[0]["sha"] == "abc"


def test_commit_statuses_keep_only_the_latest_context_result(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
    statuses = [
        {
            "id": 2,
            "context": "checks/security",
            "state": "success",
            "created_at": "2026-01-02T00:00:00Z",
        },
        {
            "id": 1,
            "context": "checks/security",
            "state": "pending",
            "created_at": "2026-01-01T00:00:00Z",
        },
    ]
    monkeypatch.setattr(
        github_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"statuses": statuses}),
    )

    result = client.get_commit_statuses("42", "octo", "widgets", "abc")

    assert len(result) == 1
    assert result[0]["identity"] == "commit_status:2"
    assert result[0]["conclusion"] == "success"


def test_check_suites_are_normalized_and_reject_other_revisions(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
    suites = [
        {
            "id": 9,
            "head_sha": "abc",
            "status": "in_progress",
            "conclusion": None,
            "html_url": "https://github.com/octo/widgets/runs/9",
            "app": {"name": "GitHub Actions", "slug": "github-actions"},
        },
        {
            "id": 10,
            "head_sha": "old-sha",
            "status": "completed",
            "conclusion": "failure",
        },
    ]
    monkeypatch.setattr(
        github_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"check_suites": suites}),
    )

    result = client.get_check_suites("42", "octo", "widgets", "abc")

    assert len(result) == 1
    assert result[0]["identity"] == "check_suite:9"
    assert result[0]["provider"] == "github-actions"
    assert result[0]["sha"] == "abc"
    assert result[0]["url"] == "https://github.com/octo/widgets/runs/9"


@pytest.mark.parametrize(
    ("branch_payload", "review_payload", "rules", "expected_count"),
    [
        (
            {"protected": True},
            {
                "required_approving_review_count": 2,
                "dismiss_stale_reviews": True,
            },
            [],
            2,
        ),
        (
            {"protected": False},
            None,
            [
                {
                    "type": "pull_request",
                    "parameters": {
                        "required_approving_review_count": 1,
                        "dismiss_stale_reviews": True,
                    },
                }
            ],
            1,
        ),
    ],
)
def test_required_pull_request_reviews_accepts_stale_dismissing_protection(
    monkeypatch, branch_payload, review_payload, rules, expected_count
):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda *args, **kwargs: {})
    responses = [FakeResponse(branch_payload)]
    if review_payload is not None:
        responses.append(FakeResponse(review_payload))
    monkeypatch.setattr(
        client, "_get_with_retry", lambda *args, **kwargs: responses.pop(0)
    )
    monkeypatch.setattr(client, "_get_paginated_items", lambda *args: rules)

    protection = client.get_required_pull_request_reviews(
        "42", "octo", "widgets", "main"
    )

    assert protection == {
        "required_approving_review_count": expected_count,
        "dismiss_stale_reviews": True,
    }


def test_required_pull_request_reviews_rejects_protection_without_stale_dismissal(
    monkeypatch,
):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda *args, **kwargs: {})
    responses = [
        FakeResponse({"protected": True}),
        FakeResponse(
            {
                "required_approving_review_count": 1,
                "dismiss_stale_reviews": False,
            }
        ),
    ]
    monkeypatch.setattr(
        client, "_get_with_retry", lambda *args, **kwargs: responses.pop(0)
    )
    monkeypatch.setattr(client, "_get_paginated_items", lambda *args: [])

    with pytest.raises(ValueError, match="dismiss stale approvals"):
        client.get_required_pull_request_reviews("42", "octo", "widgets", "main")


def test_disable_auto_merge_uses_graphql_pull_request_node_id(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda *args, **kwargs: {})
    calls = []
    monkeypatch.setattr(
        github_module.requests,
        "post",
        lambda *args, **kwargs: (
            calls.append((args, kwargs))
            or FakeResponse({"data": {"disablePullRequestAutoMerge": {}}})
        ),
    )

    result = client.disable_pull_request_auto_merge("42", "PR_node_id")

    assert result["succeeded"] is True
    assert calls[0][1]["json"]["variables"] == {"pullRequestId": "PR_node_id"}
    assert "disablePullRequestAutoMerge" in calls[0][1]["json"]["query"]


def test_immediate_merge_uses_expected_head_sha(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda *args, **kwargs: {})
    calls = []

    def fake_put(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse({"merged": True, "sha": "merge-commit"})

    monkeypatch.setattr(github_module.requests, "put", fake_put)

    result = client.merge_pull_request(
        "42", "octo", "widgets", 4, "head1", "SQUASH"
    )

    assert result["succeeded"] is True
    assert result["payload"]["merged"] is True
    assert calls[0][1]["json"] == {
        "sha": "head1",
        "merge_method": "squash",
    }


def test_required_checks_combine_branch_protection_and_rulesets(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda *args, **kwargs: {})

    def fake_get(url, headers, params=None, **kwargs):
        if url.endswith("/branches/main"):
            return FakeResponse({"protected": True})
        if url.endswith("/protection/required_status_checks"):
            return FakeResponse(
                {
                    "checks": [
                        {"context": "build", "app_id": 12},
                        {"context": "unit-tests", "app_id": None},
                    ],
                }
            )
        raise AssertionError(f"Unexpected endpoint: {url}")

    monkeypatch.setattr(client, "_get_with_retry", fake_get)
    monkeypatch.setattr(
        client,
        "_get_paginated_items",
        lambda *args, **kwargs: [
            {
                "type": "required_status_checks",
                "parameters": {
                    "required_status_checks": [
                        {"context": "build", "integration_id": 12},
                        {"context": "release-gate", "integration_id": 45},
                    ],
                },
            },
        ],
    )

    result = client.get_required_status_checks("42", "octo", "widgets", "main")

    assert result == [
        {"context": "build", "app_id": "12"},
        {"context": "unit-tests", "app_id": None},
        {"context": "release-gate", "app_id": "45"},
    ]


def test_required_check_discovery_errors_are_not_empty_success(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda *args, **kwargs: {})

    def fail_request(*args, **kwargs):
        raise github_module.requests.HTTPError("forbidden")

    monkeypatch.setattr(
        client,
        "_get_with_retry",
        fail_request,
    )

    with pytest.raises(github_module.requests.HTTPError):
        client.get_required_status_checks("42", "octo", "widgets", "main")


def test_github_get_retries_rate_limits_with_bounded_retry_after(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
    responses = iter(
        [
            FakeResponse(
                {"message": "rate limited"},
                status_code=429,
                headers={"Retry-After": "2"},
            ),
            FakeResponse({"statuses": []}),
        ]
    )
    sleeps = []
    monkeypatch.setattr(github_module.requests, "get", lambda *a, **kw: next(responses))
    monkeypatch.setattr(github_module.time, "sleep", sleeps.append)

    assert client.get_commit_statuses("42", "octo", "widgets", "abc") == []
    assert sleeps == [2.0]


def test_pr_file_response_is_paginated_and_preserves_readme_files(monkeypatch):
    client = github_module.GitHubClient()
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
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
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
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
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
    monkeypatch.setattr(
        github_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"message": "unexpected response"}),
    )

    with pytest.raises(ValueError, match="list of objects"):
        client.get_pr_files("42", "octo", "widgets", 7)


def test_review_and_comment_collection_endpoints_accept_raw_arrays(monkeypatch):
    client = github_module.GitHubClient()
    pages = iter(
        [
            FakeResponse([{"id": index, "body": "review"} for index in range(100)]),
            FakeResponse([]),
        ]
    )
    calls = []

    def fake_get(url, headers, params, timeout):
        calls.append(params)
        return next(pages)

    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
    monkeypatch.setattr(github_module.requests, "get", fake_get)

    reviews = client._get_paginated_items(
        "https://api.github.com/repos/octo/widgets/pulls/7/reviews", {}, "reviews"
    )

    assert len(reviews) == 100
    assert [call["page"] for call in calls] == [1, 2]


def test_wrapped_collections_and_empty_raw_arrays_are_valid(monkeypatch):
    client = github_module.GitHubClient()

    monkeypatch.setattr(
        client,
        "_get_with_retry",
        lambda *args, **kwargs: FakeResponse({"reviews": []}),
    )
    assert client._get_paginated_items("url", {}, "reviews") == []

    monkeypatch.setattr(
        client,
        "_get_with_retry",
        lambda *args, **kwargs: FakeResponse([]),
    )
    assert client._get_paginated_items("url", {}, "reviews") == []


def test_marked_review_retries_after_ambiguous_post_without_duplicate(monkeypatch):
    client = github_module.GitHubClient()
    marker = "<!-- securepr-review:pr:sha -->"
    get_responses = iter(
        [
            FakeResponse([]),
            FakeResponse([{"id": 9, "body": f"review {marker}"}]),
        ]
    )
    post_calls = []
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
    monkeypatch.setattr(
        github_module.requests,
        "get",
        lambda *args, **kwargs: next(get_responses),
    )
    monkeypatch.setattr(
        github_module.requests,
        "post",
        lambda *args, **kwargs: (
            post_calls.append((args, kwargs))
            or FakeResponse({"message": "request outcome unknown"}, status_code=503)
        ),
    )

    first = client.post_review(
        "42",
        "octo",
        "widgets",
        7,
        f"review {marker}",
        "COMMENT",
        idempotency_marker=marker,
    )
    retry = client.post_review(
        "42",
        "octo",
        "widgets",
        7,
        f"review {marker}",
        "COMMENT",
        idempotency_marker=marker,
    )

    assert first["succeeded"] is False
    assert retry["already_exists"] is True
    assert len(post_calls) == 1


def test_review_lookup_api_error_is_not_treated_as_an_empty_list(monkeypatch):
    client = github_module.GitHubClient()
    post_calls = []
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
    monkeypatch.setattr(
        github_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(
            {"message": "forbidden"},
            github_module.requests.HTTPError("forbidden"),
            status_code=403,
        ),
    )
    monkeypatch.setattr(
        github_module.requests,
        "post",
        lambda *args, **kwargs: post_calls.append((args, kwargs)),
    )

    with pytest.raises(github_module.requests.HTTPError):
        client.post_review(
            "42",
            "octo",
            "widgets",
            7,
            "body <!-- marker -->",
            "COMMENT",
            idempotency_marker="<!-- marker -->",
        )
    assert post_calls == []


def test_marked_issue_comment_is_updated_in_place(monkeypatch):
    client = github_module.GitHubClient()
    marker = "<!-- securepr-status:pr:sha -->"
    patch_calls = []
    get_responses = iter(
        [
            FakeResponse([{"id": 19, "body": f"Waiting {marker}"}]),
            FakeResponse([{"id": 19, "body": f"Final {marker}"}]),
        ]
    )
    monkeypatch.setattr(client, "_headers", lambda installation_id, deadline=None: {})
    monkeypatch.setattr(
        github_module.requests,
        "get",
        lambda *args, **kwargs: next(get_responses),
    )
    monkeypatch.setattr(
        github_module.requests,
        "patch",
        lambda *args, **kwargs: (
            patch_calls.append((args, kwargs))
            or FakeResponse({"id": 19, "body": f"Final {marker}"})
        ),
    )

    updated = client.post_issue_comment(
        "42",
        "octo",
        "widgets",
        7,
        f"Final {marker}",
        idempotency_marker=marker,
    )
    unchanged = client.post_issue_comment(
        "42",
        "octo",
        "widgets",
        7,
        f"Final {marker}",
        idempotency_marker=marker,
    )

    assert updated["updated"] is True
    assert patch_calls[0][0][0].endswith("/issues/comments/19")
    assert unchanged["already_exists"] is True
    assert len(patch_calls) == 1
