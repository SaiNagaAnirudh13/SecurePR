"""
GitHub App integration.

Handles: JWT-based App authentication, installation token exchange, fetching
PR diffs/CI checks, posting review comments, and submitting (never merging)
PR reviews, re-validating PR state, and (only through the guarded pipeline)
merging eligible pull requests.
"""
import time
import logging

import jwt
import requests

from app.core.config import get_settings

logger = logging.getLogger("securepr.github")

GITHUB_API = "https://api.github.com"


class GitHubClient:
    def __init__(self):
        self.settings = get_settings()

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
        app_jwt = self._app_jwt()
        resp = requests.post(
            f"{GITHUB_API}/app/installations/{installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {app_jwt}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["token"]

    def _headers(self, installation_id: str) -> dict:
        token = self._installation_token(installation_id)
        return {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}

    def get_pr_files(self, installation_id: str, owner: str, repo: str, pr_number: int) -> list[dict]:
        headers = self._headers(installation_id)
        files, page = [], 1
        while True:
            resp = requests.get(
                f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files",
                headers=headers, params={"per_page": 100, "page": page}, timeout=20,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            for f in batch:
                files.append({
                    "file_path": f["filename"],
                    "change_type": f["status"],
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                    "patch_excerpt": (f.get("patch") or "")[:4000],
                })
            page += 1
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
        resp = requests.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{head_sha}/check-runs",
            headers=headers, timeout=20,
        )
        resp.raise_for_status()
        return [{"name": c["name"], "status": c.get("status"), "conclusion": c.get("conclusion"),
                 "html_url": c.get("html_url"), "completed_at": c.get("completed_at"),
                 "started_at": c.get("started_at"), "id": c.get("id")} for c in resp.json().get("check_runs", [])]

    def get_commit_statuses(self, installation_id: str, owner: str, repo: str, head_sha: str) -> list[dict]:
        """Legacy commit-status API (used by some non-Actions CI providers).
        Normalized into the same {name, status, conclusion} shape as check runs:
        state "pending" -> status "in_progress"/conclusion None; "success"/"failure"/
        "error" map straight to both status "completed" and that conclusion."""
        headers = self._headers(installation_id)
        resp = requests.get(
            f"{GITHUB_API}/repos/{owner}/{repo}/commits/{head_sha}/status",
            headers=headers, timeout=20,
        )
        resp.raise_for_status()
        out = []
        for s in resp.json().get("statuses", []):
            state = s.get("state")
            if state == "pending":
                out.append({"name": s.get("context"), "status": "in_progress", "conclusion": None,
                            "html_url": s.get("target_url"), "completed_at": None,
                            "started_at": s.get("created_at"), "id": s.get("id")})
            else:
                conclusion = "success" if state == "success" else "failure"
                out.append({"name": s.get("context"), "status": "completed", "conclusion": conclusion,
                            "html_url": s.get("target_url"), "completed_at": s.get("updated_at") or s.get("created_at"),
                            "started_at": s.get("created_at"), "id": s.get("id")})
        return out

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
        resp = requests.get(f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}",
                            headers=self._headers(installation_id), timeout=20)
        resp.raise_for_status()
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
