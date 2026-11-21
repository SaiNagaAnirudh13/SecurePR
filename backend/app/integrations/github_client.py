"""
GitHub App integration.

Handles: JWT-based App authentication, installation token exchange, fetching
PR diffs/CI checks, posting review comments, and submitting (never merging)
PR reviews, re-validating PR state, and (only through the guarded pipeline)
merging eligible pull requests.
"""

import logging
import threading
import time

import jwt
import requests

from app.core.config import get_settings

logger = logging.getLogger("securepr.github")

GITHUB_API = "https://api.github.com"
GITHUB_PAGE_SIZE = 100
INSTALLATION_TOKEN_CACHE_SECONDS = 45 * 60
GITHUB_GET_MAX_ATTEMPTS = 3
GITHUB_GET_MAX_RETRY_DELAY_SECONDS = 10


class GitHubClient:
    def __init__(self):
        self.settings = get_settings()
        self._installation_tokens: dict[str, tuple[str, float]] = {}
        self._installation_token_lock = threading.Lock()

    def _private_key(self) -> str:
        if self.settings.GITHUB_PRIVATE_KEY:
            return self.settings.GITHUB_PRIVATE_KEY.replace("\\n", "\n").strip()
        with open(self.settings.GITHUB_PRIVATE_KEY_PATH, "r", encoding="utf-8") as fh:
            return fh.read()

    def _app_jwt(self) -> str:
        now = int(time.time())
        payload = {
            "iat": now - 60,
            "exp": now + 9 * 60,
            "iss": self.settings.GITHUB_APP_ID,
        }
        return jwt.encode(payload, self._private_key(), algorithm="RS256")

    def _installation_token(
        self, installation_id: str, deadline: float | None = None
    ) -> str:
        with self._installation_token_lock:
            now = time.monotonic()
            cached = self._installation_tokens.get(installation_id)
            if cached and cached[1] > now:
                return cached[0]

            remaining = deadline - now if deadline is not None else 15
            if remaining <= 0:
                raise TimeoutError(
                    "GitHub installation-token request exceeded its deadline."
                )
            app_jwt = self._app_jwt()
            resp = requests.post(
                f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                },
                timeout=min(15, remaining),
            )
            resp.raise_for_status()
            token = resp.json().get("token")
            if not isinstance(token, str) or not token:
                raise ValueError("GitHub did not return an installation access token.")
            self._installation_tokens[installation_id] = (
                token,
                time.monotonic() + INSTALLATION_TOKEN_CACHE_SECONDS,
            )
            return token

    def _headers(self, installation_id: str, deadline: float | None = None) -> dict:
        token = self._installation_token(installation_id, deadline)
        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }

    def _get_with_retry(
        self,
        url: str,
        headers: dict,
        params: dict | None = None,
        *,
        timeout: float = 20,
        max_attempts: int = GITHUB_GET_MAX_ATTEMPTS,
        deadline: float | None = None,
        allowed_statuses: tuple[int, ...] = (),
    ):
        for attempt in range(max_attempts):
            remaining = deadline - time.monotonic() if deadline is not None else timeout
            if remaining <= 0:
                raise TimeoutError(
                    "GitHub API request exceeded its discovery deadline."
                )
            try:
                resp = requests.get(
                    url,
                    headers=headers,
                    params=params,
                    timeout=min(timeout, remaining),
                )
            except requests.RequestException:
                if attempt + 1 == max_attempts:
                    raise
                delay = min(0.25 * (2**attempt), GITHUB_GET_MAX_RETRY_DELAY_SECONDS)
                if deadline is not None:
                    delay = min(delay, max(0, deadline - time.monotonic()))
                time.sleep(delay)
                continue

            status = resp.status_code
            rate_limited = status == 403 and (
                resp.headers.get("X-RateLimit-Remaining") == "0"
                or "Retry-After" in resp.headers
            )
            retryable = status in (429, 500, 502, 503, 504) or rate_limited
            if not retryable or attempt + 1 == max_attempts:
                if status in allowed_statuses:
                    return resp
                resp.raise_for_status()
                return resp

            retry_after = resp.headers.get("Retry-After")
            try:
                if retry_after is not None:
                    delay = float(retry_after)
                elif rate_limited and resp.headers.get("X-RateLimit-Reset"):
                    delay = float(resp.headers["X-RateLimit-Reset"]) - time.time()
                else:
                    delay = 0.25 * (2**attempt)
            except ValueError:
                delay = 0.25 * (2**attempt)
            delay = min(max(delay, 0), GITHUB_GET_MAX_RETRY_DELAY_SECONDS)
            if deadline is not None:
                delay = min(delay, max(0, deadline - time.monotonic()))
            time.sleep(delay)

    def _get_paginated_items(
        self,
        url: str,
        headers: dict,
        collection_key: str | None = None,
        *,
        timeout: float = 20,
        max_attempts: int = GITHUB_GET_MAX_ATTEMPTS,
        deadline: float | None = None,
    ) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            resp = self._get_with_retry(
                url,
                headers,
                params={"per_page": GITHUB_PAGE_SIZE, "page": page},
                timeout=timeout,
                max_attempts=max_attempts,
                deadline=deadline,
            )
            payload = resp.json()
            raw_batch: object
            if isinstance(payload, list):
                raw_batch = payload
            elif collection_key is not None and isinstance(payload, dict):
                raw_batch = payload.get(collection_key)
            else:
                raw_batch = None
            if not isinstance(raw_batch, list):
                field = collection_key or "response"
                raise ValueError(
                    f"GitHub API response must contain a list of objects in '{field}'."
                )
            batch = [item for item in raw_batch if isinstance(item, dict)]
            if len(batch) != len(raw_batch):
                field = collection_key or "response"
                raise ValueError(
                    f"GitHub API response must contain a list of objects in '{field}'."
                )
            items.extend(batch)
            if len(batch) < GITHUB_PAGE_SIZE:
                return items
            page += 1

    def get_pr_files(
        self, installation_id: str, owner: str, repo: str, pr_number: int
    ) -> list[dict]:
        headers = self._headers(installation_id)
        payload = self._get_paginated_items(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files",
            headers,
        )
        files = []
        for item in payload:
            filename = item.get("filename")
            if not isinstance(filename, str) or not filename:
                raise ValueError(
                    "GitHub pull-request file response is missing a filename."
                )
            files.append(
                {
                    "file_path": filename,
                    "change_type": item.get("status"),
                    "additions": item.get("additions", 0),
                    "deletions": item.get("deletions", 0),
                    "patch_excerpt": (item.get("patch") or "")[:4000],
                }
            )
        return files

    def get_check_runs(
        self, installation_id: str, owner: str, repo: str, head_sha: str
    ) -> list[dict]:
        """
        Returns each check run's name, status (queued|in_progress|completed) and
        conclusion (success|failure|neutral|cancelled|timed_out|action_required|
        skipped|stale|None). status is what lets callers tell "still running"
        apart from "finished and failed" -- conclusion alone cannot, since it's
        None for anything not yet completed.
        """
        deadline = time.monotonic() + 8
        headers = self._headers(installation_id, deadline)
        runs = self._get_paginated_items(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            headers,
            "check_runs",
            timeout=3,
            max_attempts=2,
            deadline=deadline,
        )
        normalized = []
        for check in runs:
            check_sha = check.get("head_sha") or head_sha
            if check_sha != head_sha:
                continue
            app = check.get("app") or {}
            identity = check.get("id")
            normalized.append(
                {
                    "identity": f"check_run:{identity}"
                    if identity is not None
                    else None,
                    "id": identity,
                    "kind": "check_run",
                    "name": check.get("name"),
                    "provider": app.get("slug")
                    or app.get("name")
                    or "GitHub Check Run",
                    "app_id": str(app.get("id")) if app.get("id") is not None else None,
                    "status": check.get("status"),
                    "conclusion": check.get("conclusion"),
                    "url": check.get("html_url"),
                    "html_url": check.get("html_url"),
                    "sha": check_sha,
                    "completed_at": check.get("completed_at"),
                    "started_at": check.get("started_at"),
                }
            )
        return normalized

    def get_check_suites(
        self, installation_id: str, owner: str, repo: str, head_sha: str
    ) -> list[dict]:
        deadline = time.monotonic() + 8
        headers = self._headers(installation_id, deadline)
        suites = self._get_paginated_items(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{head_sha}/check-suites",
            headers,
            "check_suites",
            timeout=3,
            max_attempts=2,
            deadline=deadline,
        )
        normalized = []
        for suite in suites:
            suite_sha = suite.get("head_sha") or head_sha
            if suite_sha != head_sha:
                continue
            app = suite.get("app") or {}
            identity = suite.get("id")
            normalized.append(
                {
                    "identity": f"check_suite:{identity}"
                    if identity is not None
                    else None,
                    "id": identity,
                    "kind": "check_suite",
                    "name": app.get("name") or app.get("slug") or "GitHub Check Suite",
                    "provider": app.get("slug")
                    or app.get("name")
                    or "GitHub Check Suite",
                    "status": suite.get("status"),
                    "conclusion": suite.get("conclusion"),
                    "url": suite.get("html_url"),
                    "html_url": suite.get("html_url"),
                    "sha": suite_sha,
                    "completed_at": suite.get("completed_at"),
                    "started_at": suite.get("created_at"),
                }
            )
        return normalized

    def get_commit_statuses(
        self, installation_id: str, owner: str, repo: str, head_sha: str
    ) -> list[dict]:
        """Legacy commit-status API (used by some non-Actions CI providers).
        Normalized into the same {name, status, conclusion} shape as check runs:
        state "pending" -> status "in_progress"/conclusion None; "success"/"failure"/
        "error" map straight to both status "completed" and that conclusion."""
        deadline = time.monotonic() + 8
        headers = self._headers(installation_id, deadline)
        statuses = self._get_paginated_items(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{head_sha}/status",
            headers,
            "statuses",
            timeout=3,
            max_attempts=2,
            deadline=deadline,
        )
        latest_by_context: dict[tuple[str, str | None], dict] = {}
        for s in statuses:
            state = s.get("state")
            context = s.get("context")
            creator = s.get("creator") or {}
            provider = creator.get("login") or "Commit Status"
            previous = latest_by_context.get((provider, context))
            created_at = s.get("created_at") or ""
            if previous and (previous.get("created_at") or "") >= created_at:
                continue
            if state == "pending":
                status = "in_progress"
                conclusion = None
            elif state in ("success", "failure", "error"):
                status = "completed"
                conclusion = "success" if state == "success" else state
            else:
                status = "unknown"
                conclusion = state
            identity = s.get("id")
            latest_by_context[(provider, context)] = {
                "identity": f"commit_status:{identity}"
                if identity is not None
                else None,
                "id": identity,
                "kind": "commit_status",
                "name": context,
                "context": context,
                "provider": provider,
                "app_id": str(creator.get("id"))
                if creator.get("id") is not None
                else None,
                "status": status,
                "conclusion": conclusion,
                "url": s.get("target_url"),
                "html_url": s.get("target_url"),
                "sha": head_sha,
                "completed_at": s.get("updated_at") or s.get("created_at")
                if status == "completed"
                else None,
                "started_at": s.get("created_at"),
                "created_at": s.get("created_at"),
            }
        return list(latest_by_context.values())

    def get_required_status_checks(
        self, installation_id: str, owner: str, repo: str, branch: str
    ) -> list[dict]:
        """Discover required checks from branch protection and applicable rulesets."""
        headers = self._headers(installation_id)
        branch_url = f"{GITHUB_API}/repos/{owner}/{repo}/branches/{branch}"
        branch_response = self._get_with_retry(
            branch_url, headers, allowed_statuses=(404,)
        )
        if branch_response.status_code == 404:
            raise ValueError(
                f"GitHub could not verify base branch protection for '{branch}'."
            )
        branch_data = branch_response.json()
        if not isinstance(branch_data, dict) or not isinstance(
            branch_data.get("protected"), bool
        ):
            raise TypeError("GitHub branch response has an invalid protection state.")
        required = []
        if branch_data["protected"]:
            checks_url = (
                f"{GITHUB_API}/repos/{owner}/{repo}/branches/{branch}"
                "/protection/required_status_checks"
            )
            response = self._get_with_retry(
                checks_url, headers, allowed_statuses=(404,)
            )
            if response.status_code != 404:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError(
                        "GitHub required-status-check response is malformed."
                    )
                contexts = payload.get("checks")
                if isinstance(contexts, list):
                    for item in contexts:
                        if not isinstance(item, dict) or not isinstance(
                            item.get("context"), str
                        ):
                            raise ValueError(
                                "GitHub returned a malformed required status check."
                            )
                        required.append(
                            {
                                "context": item["context"],
                                "app_id": str(item["app_id"])
                                if item.get("app_id") is not None
                                else None,
                            }
                        )
                else:
                    legacy_contexts = payload.get("contexts")
                    if not isinstance(legacy_contexts, list) or any(
                        not isinstance(context, str) for context in legacy_contexts
                    ):
                        raise ValueError(
                            "GitHub required-status-check response has no valid contexts."
                        )
                    required.extend(
                        {"context": context, "app_id": None}
                        for context in legacy_contexts
                    )

        rules_url = f"{GITHUB_API}/repos/{owner}/{repo}/rules/branches/{branch}"
        rules = self._get_paginated_items(rules_url, headers)
        for rule in rules:
            if rule.get("type") != "required_status_checks":
                continue
            parameters = rule.get("parameters") or {}
            checks = parameters.get("required_status_checks")
            if not isinstance(checks, list):
                raise ValueError(
                    "GitHub branch ruleset has malformed required status checks."
                )
            for item in checks:
                if not isinstance(item, dict) or not isinstance(
                    item.get("context"), str
                ):
                    raise ValueError(
                        "GitHub branch ruleset contains an invalid required check."
                    )
                required.append(
                    {
                        "context": item["context"],
                        "app_id": (
                            str(item["integration_id"])
                            if item.get("integration_id") is not None
                            else None
                        ),
                    }
                )

        unique = {(item["context"], item["app_id"]): item for item in required}
        return list(unique.values())

    def get_required_pull_request_reviews(
        self, installation_id: str, owner: str, repo: str, branch: str
    ) -> dict:
        """Require protected branches to invalidate approvals after every push."""
        headers = self._headers(installation_id)
        branch_url = f"{GITHUB_API}/repos/{owner}/{repo}/branches/{branch}"
        branch_response = self._get_with_retry(
            branch_url, headers, allowed_statuses=(404,)
        )
        if branch_response.status_code == 404:
            raise ValueError(
                f"GitHub could not verify branch protection for '{branch}'."
            )
        branch_data = branch_response.json()
        if not isinstance(branch_data, dict) or not isinstance(
            branch_data.get("protected"), bool
        ):
            raise ValueError("GitHub branch response is missing its protection state.")

        protections = []
        if branch_data["protected"]:
            reviews_url = (
                f"{GITHUB_API}/repos/{owner}/{repo}/branches/{branch}"
                "/protection/required_pull_request_reviews"
            )
            response = self._get_with_retry(
                reviews_url, headers, allowed_statuses=(404,)
            )
            if response.status_code != 404:
                protection = response.json()
                if not isinstance(protection, dict):
                    raise ValueError(
                        "GitHub required-review protection response is malformed."
                    )
                protections.append(
                    {
                        "required_approving_review_count": protection.get(
                            "required_approving_review_count"
                        ),
                        "dismiss_stale_reviews": protection.get(
                            "dismiss_stale_reviews"
                        ),
                    }
                )

        rules_url = f"{GITHUB_API}/repos/{owner}/{repo}/rules/branches/{branch}"
        for rule in self._get_paginated_items(rules_url, headers):
            if rule.get("type") != "pull_request":
                continue
            parameters = rule.get("parameters")
            if not isinstance(parameters, dict):
                raise TypeError("GitHub pull-request ruleset protection is malformed.")
            protections.append(
                {
                    "required_approving_review_count": parameters.get(
                        "required_approving_review_count"
                    ),
                    "dismiss_stale_reviews": parameters.get("dismiss_stale_reviews"),
                }
            )

        if not protections:
            raise ValueError(
                "GitHub branch protection must require pull-request reviews."
            )
        required_counts: list[int] = []
        for protection in protections:
            count = protection["required_approving_review_count"]
            if (
                isinstance(count, bool)
                or not isinstance(count, int)
                or count < 1
                or protection["dismiss_stale_reviews"] is not True
            ):
                raise ValueError(
                    "GitHub protection must require an approval and dismiss stale "
                    "approvals when new commits are pushed."
                )
            required_counts.append(count)
        return {
            "required_approving_review_count": max(required_counts),
            "dismiss_stale_reviews": True,
        }

    def _graphql(self, installation_id: str, query: str, variables: dict) -> dict:
        response = requests.post(
            f"{GITHUB_API}/graphql",
            headers=self._headers(installation_id),
            json={"query": query, "variables": variables},
            timeout=20,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {"message": response.text}
        if not response.ok:
            return {
                "succeeded": False,
                "status_code": response.status_code,
                "payload": payload,
            }
        if not isinstance(payload, dict) or payload.get("errors"):
            return {
                "succeeded": False,
                "status_code": response.status_code,
                "payload": payload,
            }
        return {
            "succeeded": True,
            "status_code": response.status_code,
            "payload": payload.get("data") or {},
        }

    def get_auto_merge_capabilities(
        self, installation_id: str, owner: str, repo: str, pr_number: int
    ) -> dict:
        query = """
        query($owner: String!, $name: String!, $number: Int!) {
          repository(owner: $owner, name: $name) {
            autoMergeAllowed
            mergeCommitAllowed
            squashMergeAllowed
            rebaseMergeAllowed
            pullRequest(number: $number) {
              id
              state
              isDraft
              headRefOid
              baseRefName
              mergeable
              mergeStateStatus
              reviewDecision
              autoMergeRequest { enabledAt mergeMethod }
            }
          }
        }
        """
        return self._graphql(
            installation_id,
            query,
            {"owner": owner, "name": repo, "number": pr_number},
        )

    def enable_pull_request_auto_merge(
        self, installation_id: str, pull_request_id: str, merge_method: str
    ) -> dict:
        mutation = """
        mutation($pullRequestId: ID!, $mergeMethod: PullRequestMergeMethod!) {
          enablePullRequestAutoMerge(input: {
            pullRequestId: $pullRequestId,
            mergeMethod: $mergeMethod
          }) {
            pullRequest {
              id
              autoMergeRequest { enabledAt mergeMethod }
            }
          }
        }
        """
        return self._graphql(
            installation_id,
            mutation,
            {"pullRequestId": pull_request_id, "mergeMethod": merge_method},
        )

    def disable_pull_request_auto_merge(
        self, installation_id: str, pull_request_id: str
    ) -> dict:
        mutation = """
        mutation($pullRequestId: ID!) {
          disablePullRequestAutoMerge(input: {
            pullRequestId: $pullRequestId
          }) {
            pullRequest {
              id
              autoMergeRequest { enabledAt mergeMethod }
            }
          }
        }
        """
        return self._graphql(
            installation_id, mutation, {"pullRequestId": pull_request_id}
        )

    def post_review(
        self,
        installation_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        event: str,
        *,
        idempotency_marker: str | None = None,
        commit_id: str | None = None,
    ) -> dict:
        """
        event must be one of APPROVE | COMMENT | REQUEST_CHANGES.
        This never calls the merge endpoint.
        """
        assert event in ("APPROVE", "COMMENT", "REQUEST_CHANGES")
        headers = self._headers(installation_id)
        if idempotency_marker:
            existing = self._find_marked_record(
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                headers,
                "reviews",
                idempotency_marker,
            )
            if existing:
                return {
                    "succeeded": True,
                    "status_code": 200,
                    "payload": existing,
                    "already_exists": True,
                }
        resp = requests.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers=headers,
            json={
                "body": body,
                "event": event,
                **({"commit_id": commit_id} if commit_id else {}),
            },
            timeout=20,
        )
        succeeded = resp.ok
        if not succeeded:
            logger.error(
                "GitHub review submission failed (%s): %s", resp.status_code, resp.text
            )
        return {
            "succeeded": succeeded,
            "status_code": resp.status_code,
            "payload": resp.json() if succeeded else resp.text,
        }

    def dismiss_review(
        self,
        installation_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        review_id: str,
        message: str,
    ) -> dict:
        """Ask GitHub to dismiss a now-invalid SecurePR approval or request-changes review."""
        resp = requests.put(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews/{review_id}/dismissals",
            headers=self._headers(installation_id),
            json={"message": message},
            timeout=20,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"message": resp.text}
        return {
            "succeeded": resp.ok,
            "status_code": resp.status_code,
            "payload": payload,
        }

    def get_pull_request(
        self, installation_id: str, owner: str, repo: str, pr_number: int
    ) -> dict:
        """Fetch live state used immediately before an auto-merge attempt."""
        resp = self._get_with_retry(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            self._headers(installation_id),
        )
        data = resp.json()
        return {
            "state": data.get("state"),
            "mergeable": data.get("mergeable"),
            "mergeable_state": data.get("mergeable_state"),
            "head_sha": (data.get("head") or {}).get("sha"),
            "base_ref": (data.get("base") or {}).get("ref"),
        }

    def _find_marked_record(
        self,
        url: str,
        headers: dict,
        collection_key: str,
        marker: str,
    ) -> dict | None:
        deadline = time.monotonic() + 8
        records = self._get_paginated_items(
            url,
            headers,
            collection_key,
            timeout=3,
            max_attempts=2,
            deadline=deadline,
        )
        return next(
            (record for record in records if marker in (record.get("body") or "")),
            None,
        )

    def post_issue_comment(
        self,
        installation_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        *,
        idempotency_marker: str | None = None,
    ) -> dict:
        headers = self._headers(installation_id)
        if idempotency_marker:
            existing = self._find_marked_record(
                f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
                headers,
                "comments",
                idempotency_marker,
            )
            if existing:
                if existing.get("body") == body:
                    return {
                        "succeeded": True,
                        "status_code": 200,
                        "payload": existing,
                        "already_exists": True,
                    }
                comment_id = existing.get("id")
                if not isinstance(comment_id, (int, str)) or not str(comment_id):
                    raise ValueError(
                        "GitHub issue comment with the idempotency marker has no valid ID."
                    )
                resp = requests.patch(
                    f"{GITHUB_API}/repos/{owner}/{repo}/issues/comments/{comment_id}",
                    headers=headers,
                    json={"body": body},
                    timeout=20,
                )
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {"message": resp.text}
                return {
                    "succeeded": resp.ok,
                    "status_code": resp.status_code,
                    "payload": payload,
                    "updated": resp.ok,
                }
        resp = requests.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            headers=headers,
            json={"body": body},
            timeout=20,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"message": resp.text}
        return {
            "succeeded": resp.ok,
            "status_code": resp.status_code,
            "payload": payload,
        }

    def notify_owner(
        self,
        installation_id: str,
        owner: str,
        repo: str,
        pr_number: int,
        message: str,
        *,
        idempotency_marker: str | None = None,
    ) -> dict:
        """Lightweight notification via an issue comment mentioning the owner."""
        body = f"@{owner} {message}"
        if idempotency_marker:
            return self.post_issue_comment(
                installation_id,
                owner,
                repo,
                pr_number,
                body,
                idempotency_marker=idempotency_marker,
            )
        headers = self._headers(installation_id)
        resp = requests.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            headers=headers,
            json={"body": body},
            timeout=20,
        )
        return {"succeeded": resp.ok, "status_code": resp.status_code}


github_client = GitHubClient()
