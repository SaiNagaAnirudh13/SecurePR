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
        payload = {"iat": now - 60, "exp": now + 9 * 60, "iss": self.settings.GITHUB_APP_ID}
        return jwt.encode(payload, self._private_key(), algorithm="RS256")

    def _installation_token(self, installation_id: str) -> str:
        with self._installation_token_lock:
            now = time.monotonic()
            cached = self._installation_tokens.get(installation_id)
            if cached and cached[1] > now:
                return cached[0]

            app_jwt = self._app_jwt()
            resp = requests.post(
                f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
                timeout=15,
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

    def _headers(self, installation_id: str) -> dict:
        token = self._installation_token(installation_id)
        return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    def _get_with_retry(self, url: str, headers: dict, params: dict | None = None):
        for attempt in range(GITHUB_GET_MAX_ATTEMPTS):
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=20)
            except requests.RequestException:
                if attempt + 1 == GITHUB_GET_MAX_ATTEMPTS:
                    raise
                time.sleep(min(0.25 * (2 ** attempt), GITHUB_GET_MAX_RETRY_DELAY_SECONDS))
                continue

            status = resp.status_code
            rate_limited = status == 403 and (
                resp.headers.get("X-RateLimit-Remaining") == "0"
                or "Retry-After" in resp.headers
            )
            retryable = status in (429, 500, 502, 503, 504) or rate_limited
            if not retryable or attempt + 1 == GITHUB_GET_MAX_ATTEMPTS:
                resp.raise_for_status()
                return resp

            retry_after = resp.headers.get("Retry-After")
            try:
                if retry_after is not None:
                    delay = float(retry_after)
                elif rate_limited and resp.headers.get("X-RateLimit-Reset"):
                    delay = float(resp.headers["X-RateLimit-Reset"]) - time.time()
                else:
                    delay = 0.25 * (2 ** attempt)
            except ValueError:
                delay = 0.25 * (2 ** attempt)
            time.sleep(min(max(delay, 0), GITHUB_GET_MAX_RETRY_DELAY_SECONDS))

    def _get_paginated_items(self, url: str, headers: dict, collection_key: str | None = None) -> list[dict]:
        items: list[dict] = []
        page = 1
        while True:
            resp = self._get_with_retry(
                url,
                headers,
                params={"per_page": GITHUB_PAGE_SIZE, "page": page},
            )
            payload = resp.json()
            if collection_key is None:
                batch = payload
            elif isinstance(payload, dict):
                batch = payload.get(collection_key)
            else:
                batch = None
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                field = collection_key or "response"
                raise ValueError(f"GitHub API response must contain a list of objects in '{field}'.")
            items.extend(batch)
            if len(batch) < GITHUB_PAGE_SIZE:
                return items
            page += 1

    def get_pr_files(self, installation_id: str, owner: str, repo: str, pr_number: int) -> list[dict]:
        headers = self._headers(installation_id)
        payload = self._get_paginated_items(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files",
            headers,
        )
        files = []
        for item in payload:
            filename = item.get("filename")
            if not isinstance(filename, str) or not filename:
                raise ValueError("GitHub pull-request file response is missing a filename.")
            files.append({
                "file_path": filename,
                "change_type": item.get("status"),
                "additions": item.get("additions", 0),
                "deletions": item.get("deletions", 0),
                "patch_excerpt": (item.get("patch") or "")[:4000],
            })
        return files

    def get_check_runs(self, installation_id: str, owner: str, repo: str, head_sha: str) -> list[dict]:
        """
        Returns each check run's name, status (queued|in_progress|completed) and
        conclusion (success|failure|neutral|cancelled|timed_out|action_required|
        skipped|stale|None). status is what lets callers tell "still running"
        apart from "finished and failed" -- conclusion alone cannot, since it's
        None for anything not yet completed.
        """
        headers = self._headers(installation_id)
        runs = self._get_paginated_items(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            headers,
            "check_runs",
        )
        normalized = []
        for check in runs:
            check_sha = check.get("head_sha") or head_sha
            if check_sha != head_sha:
                continue
            app = check.get("app") or {}
            identity = check.get("id")
            normalized.append({
                "identity": f"check_run:{identity}" if identity is not None else None,
                "id": identity,
                "kind": "check_run",
                "name": check.get("name"),
                "provider": app.get("slug") or app.get("name") or "GitHub Check Run",
                "status": check.get("status"),
                "conclusion": check.get("conclusion"),
                "url": check.get("html_url"),
                "html_url": check.get("html_url"),
                "sha": check_sha,
                "completed_at": check.get("completed_at"),
                "started_at": check.get("started_at"),
            })
        return normalized

    def get_check_suites(self, installation_id: str, owner: str, repo: str, head_sha: str) -> list[dict]:
        headers = self._headers(installation_id)
        suites = self._get_paginated_items(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{head_sha}/check-suites",
            headers,
            "check_suites",
        )
        normalized = []
        for suite in suites:
            suite_sha = suite.get("head_sha") or head_sha
            if suite_sha != head_sha:
                continue
            app = suite.get("app") or {}
            identity = suite.get("id")
            normalized.append({
                "identity": f"check_suite:{identity}" if identity is not None else None,
                "id": identity,
                "kind": "check_suite",
                "name": app.get("name") or app.get("slug") or "GitHub Check Suite",
                "provider": app.get("slug") or app.get("name") or "GitHub Check Suite",
                "status": suite.get("status"),
                "conclusion": suite.get("conclusion"),
                "url": suite.get("html_url"),
                "html_url": suite.get("html_url"),
                "sha": suite_sha,
                "completed_at": suite.get("completed_at"),
                "started_at": suite.get("created_at"),
            })
        return normalized

    def get_commit_statuses(self, installation_id: str, owner: str, repo: str, head_sha: str) -> list[dict]:
        """Legacy commit-status API (used by some non-Actions CI providers).
        Normalized into the same {name, status, conclusion} shape as check runs:
        state "pending" -> status "in_progress"/conclusion None; "success"/"failure"/
        "error" map straight to both status "completed" and that conclusion."""
        headers = self._headers(installation_id)
        statuses = self._get_paginated_items(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{head_sha}/status",
            headers,
            "statuses",
        )
        latest_by_context = {}
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
                status = "completed"
                conclusion = "error"
            identity = s.get("id")
            latest_by_context[(provider, context)] = {
                "identity": f"commit_status:{identity}" if identity is not None else None,
                "id": identity,
                "kind": "commit_status",
                "name": context,
                "provider": provider,
                "status": status,
                "conclusion": conclusion,
                "url": s.get("target_url"),
                "html_url": s.get("target_url"),
                "sha": head_sha,
                "completed_at": s.get("updated_at") or s.get("created_at") if status == "completed" else None,
                "started_at": s.get("created_at"),
                "created_at": s.get("created_at"),
            }
        return list(latest_by_context.values())

    def post_review(self, installation_id: str, owner: str, repo: str, pr_number: int,
                     body: str, event: str) -> dict:
        """
        event must be one of APPROVE | COMMENT | REQUEST_CHANGES.
        This never calls the merge endpoint.
        """
        assert event in ("APPROVE", "COMMENT", "REQUEST_CHANGES")
        headers = self._headers(installation_id)
        resp = requests.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
            headers=headers, json={"body": body, "event": event}, timeout=20,
        )
        succeeded = resp.ok
        if not succeeded:
            logger.error("GitHub review submission failed (%s): %s", resp.status_code, resp.text)
        return {"succeeded": succeeded, "status_code": resp.status_code, "payload": resp.json() if succeeded else resp.text}

    def get_pull_request(self, installation_id: str, owner: str, repo: str, pr_number: int) -> dict:
        """Fetch live state used immediately before an auto-merge attempt."""
        resp = self._get_with_retry(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
            self._headers(installation_id),
        )
        data = resp.json()
        return {
            "state": data.get("state"), "mergeable": data.get("mergeable"),
            "mergeable_state": data.get("mergeable_state"),
            "head_sha": (data.get("head") or {}).get("sha"),
            "base_ref": (data.get("base") or {}).get("ref"),
        }

    def merge_pull_request(self, installation_id: str, owner: str, repo: str, pr_number: int,
                           expected_head_sha: str) -> dict:
        """Use GitHub's merge endpoint with the reviewed head SHA as a CAS guard."""
        resp = requests.put(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            headers=self._headers(installation_id),
            json={"sha": expected_head_sha}, timeout=20,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"message": resp.text}
        if not resp.ok or not payload.get("merged", False):
            logger.error("GitHub merge failed (%s): %s", resp.status_code, payload)
        return {"succeeded": bool(resp.ok and payload.get("merged", False)),
                "status_code": resp.status_code, "payload": payload}

    def post_issue_comment(self, installation_id: str, owner: str, repo: str, pr_number: int, body: str) -> dict:
        resp = requests.post(f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
                             headers=self._headers(installation_id), json={"body": body}, timeout=20)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"message": resp.text}
        return {"succeeded": resp.ok, "status_code": resp.status_code, "payload": payload}

    def notify_owner(self, installation_id: str, owner: str, repo: str, pr_number: int, message: str) -> dict:
        """Lightweight notification via an issue comment mentioning the owner."""
        headers = self._headers(installation_id)
        resp = requests.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pr_number}/comments",
            headers=headers, json={"body": f"@{owner} {message}"}, timeout=20,
        )
        return {"succeeded": resp.ok, "status_code": resp.status_code}


github_client = GitHubClient()
