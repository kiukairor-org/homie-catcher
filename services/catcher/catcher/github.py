"""GitHub access as the kiu-homie-bot App, behind an explicit endpoint allowlist.

``GitHub.request`` refuses any (method, path) not in ``ENDPOINTS``. The only
Administration-permission endpoints are the four marked ``admin=True``; their
bodies are pinned to constants and the generate call is pinned to the template repo.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import jwt

from .transport import HttpError, Response, Transport

log = logging.getLogger(__name__)

API = "https://api.github.com"
TEMPLATE_REPO = "app-template"
REVIEWER_LOGIN = "kiu-homie-reviewer[bot]"
TOKEN_REFRESH_MARGIN_S = 300

REPO_SETTINGS: dict[str, bool] = {
    "allow_auto_merge": True,
    "delete_branch_on_merge": True,
    "allow_squash_merge": True,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
}

BRANCH_PROTECTION: dict[str, Any] = {
    "required_status_checks": {"strict": False, "contexts": ["ci"]},
    "enforce_admins": False,
    "required_pull_request_reviews": {
        "dismiss_stale_reviews": True,
        "required_approving_review_count": 1,
    },
    "restrictions": None,
    "allow_force_pushes": False,
    "allow_deletions": False,
}

NEW_REPO_LABELS: tuple[tuple[str, str, str], ...] = (
    ("agent", "5319e7", "Work for the Claude agent"),
    ("needs-human", "d93f0b", "Escalated: a human must look"),
    ("hold", "b60205", "Kill switch: no auto-approve / auto-merge"),
    ("preview", "0e8a16", "Deploy a PR preview"),
    ("fix-round-1", "fbca04", "Agent fix round 1 used"),
    ("fix-round-2", "fbca04", "Agent fix round 2 used"),
)

_SEG = r"[A-Za-z0-9._-]+"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class Endpoint:
    method: str
    template: str
    admin: bool = False
    auth: str = "installation"  # or "app" (JWT)

    @property
    def regex(self) -> re.Pattern[str]:
        pattern = _PLACEHOLDER.sub(lambda m: f"(?P<{m.group(1)}>{_SEG})", re.escape(self.template).replace(r"\{", "{").replace(r"\}", "}"))
        return re.compile(f"^{pattern}$")


ENDPOINTS: tuple[Endpoint, ...] = (
    Endpoint("POST", "/app/installations/{installation_id}/access_tokens", auth="app"),
    Endpoint("GET", "/installation/repositories"),
    Endpoint("GET", "/repos/{owner}/{repo}"),
    Endpoint("GET", "/repos/{owner}/{repo}/branches/main"),
    Endpoint("GET", "/repos/{owner}/{repo}/labels/{name}"),
    Endpoint("POST", "/repos/{owner}/{repo}/labels"),
    Endpoint("POST", "/repos/{owner}/{repo}/issues"),
    Endpoint("GET", "/repos/{owner}/{repo}/pulls"),
    Endpoint("GET", "/repos/{owner}/{repo}/pulls/{number}/reviews"),
    Endpoint("GET", "/repos/{owner}/{repo}/issues/{number}/comments"),
    Endpoint("GET", "/repos/{owner}/{repo}/commits/{sha}/check-runs"),
    # --- Administration permission: these four only ---
    Endpoint("POST", "/repos/{owner}/{repo}/generate", admin=True),
    Endpoint("POST", "/orgs/{org}/repos", admin=True),
    Endpoint("PATCH", "/repos/{owner}/{repo}", admin=True),
    Endpoint("PUT", "/repos/{owner}/{repo}/branches/main/protection", admin=True),
)


class EndpointNotAllowed(PermissionError):
    pass


def match_endpoint(method: str, path: str) -> tuple[Endpoint, dict[str, str]] | None:
    for ep in ENDPOINTS:
        if ep.method != method.upper():
            continue
        m = ep.regex.match(path)
        if m:
            return ep, m.groupdict()
    return None


class GitHub:
    def __init__(
        self,
        transport: Transport,
        *,
        org: str,
        app_id: str,
        installation_id: str,
        private_key_pem: str,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.transport = transport
        self.org = org
        self.app_id = str(app_id)
        self.installation_id = str(installation_id)
        self._key = private_key_pem
        self._clock = clock
        self._sleep = sleep
        self._token: str | None = None
        self._token_exp: float = 0.0
        self._token_lock = asyncio.Lock()
        self.rate_remaining: int | None = None
        self.rate_reset: float | None = None

    # ---- guard -----------------------------------------------------------
    def check_allowed(self, method: str, path: str, body: Any = None) -> Endpoint:
        found = match_endpoint(method, path)
        if found is None:
            raise EndpointNotAllowed(f"GitHub endpoint not allowlisted: {method} {path}")
        ep, params = found
        if any(v.strip(".") == "" for v in params.values()):
            raise EndpointNotAllowed("path segments '.' and '..' are not allowed")
        if "owner" in params and params["owner"] != self.org:
            raise EndpointNotAllowed(f"owner {params['owner']!r} is not {self.org!r}")
        if "org" in params and params["org"] != self.org:
            raise EndpointNotAllowed(f"org {params['org']!r} is not {self.org!r}")
        if "installation_id" in params and params["installation_id"] != self.installation_id:
            raise EndpointNotAllowed("unexpected installation id")
        if ep.template == "/repos/{owner}/{repo}/generate" and params["repo"] != TEMPLATE_REPO:
            raise EndpointNotAllowed("repos may only be generated from the template repo")
        if ep.method == "PATCH" and body != REPO_SETTINGS:
            raise EndpointNotAllowed("PATCH /repos is limited to the fixed new-repo settings")
        if ep.template.endswith("/branches/main/protection") and body != BRANCH_PROTECTION:
            raise EndpointNotAllowed("branch protection body must be the fixed policy")
        if ep.template == "/orgs/{org}/repos" and (not isinstance(body, dict) or body.get("private") is not False):
            raise EndpointNotAllowed("new repos must be public")
        return ep

    # ---- auth ------------------------------------------------------------
    def _app_jwt(self) -> str:
        now = int(self._clock())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self._key, algorithm="RS256")

    async def _installation_token(self) -> str:
        async with self._token_lock:
            if self._token and self._clock() < self._token_exp - TOKEN_REFRESH_MARGIN_S:
                return self._token
            resp = await self.request("POST", f"/app/installations/{self.installation_id}/access_tokens")
            self._token = resp.body["token"]
            exp = resp.body.get("expires_at")
            self._token_exp = datetime.fromisoformat(exp.replace("Z", "+00:00")).timestamp() if exp else self._clock() + 3000
            return self._token

    # ---- raw request -----------------------------------------------------
    async def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None, ok404: bool = False) -> Response:
        ep = self.check_allowed(method, path, json)
        if ep.auth == "app":
            auth = f"Bearer {self._app_jwt()}"
        else:
            auth = f"token {await self._installation_token()}"
        headers = {
            "Authorization": auth,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "homie-catcher",
        }
        resp = await self.transport.send(method.upper(), API + path, headers=headers, json=json, params=params)
        self._track_rate(resp)
        if resp.ok or (ok404 and resp.status == 404):
            return resp
        raise HttpError(method, path, resp.status, resp.body)

    def _track_rate(self, resp: Response) -> None:
        try:
            if "x-ratelimit-remaining" in resp.headers:
                self.rate_remaining = int(resp.headers["x-ratelimit-remaining"])
            if "x-ratelimit-reset" in resp.headers:
                self.rate_reset = float(resp.headers["x-ratelimit-reset"])
        except ValueError:
            pass

    def rate_limited(self, floor: int = 100) -> bool:
        return (
            self.rate_remaining is not None
            and self.rate_remaining < floor
            and self.rate_reset is not None
            and self._clock() < self.rate_reset
        )

    # ---- read helpers ----------------------------------------------------
    async def list_installation_repos(self) -> list[str]:
        names: list[str] = []
        for page in range(1, 11):
            resp = await self.request("GET", "/installation/repositories", params={"per_page": 100, "page": page})
            repos = resp.body.get("repositories", [])
            names += [r["name"] for r in repos if r.get("owner", {}).get("login", self.org) == self.org and not r.get("archived")]
            if len(repos) < 100:
                break
        return names

    async def repo_exists(self, name: str) -> bool:
        resp = await self.request("GET", f"/repos/{self.org}/{name}", ok404=True)
        return resp.status != 404

    async def list_pulls(self, repo: str) -> list[dict]:
        resp = await self.request(
            "GET",
            f"/repos/{self.org}/{repo}/pulls",
            params={"state": "all", "per_page": 50, "sort": "created", "direction": "desc"},
        )
        return resp.body or []

    async def list_reviews(self, repo: str, number: int) -> list[dict]:
        resp = await self.request("GET", f"/repos/{self.org}/{repo}/pulls/{number}/reviews", params={"per_page": 100})
        return resp.body or []

    async def list_issue_comments(self, repo: str, number: int) -> list[dict]:
        resp = await self.request("GET", f"/repos/{self.org}/{repo}/issues/{number}/comments", params={"per_page": 100})
        return resp.body or []

    async def check_runs(self, repo: str, sha: str, name: str = "ci") -> list[dict]:
        resp = await self.request("GET", f"/repos/{self.org}/{repo}/commits/{sha}/check-runs", params={"check_name": name, "per_page": 50})
        return (resp.body or {}).get("check_runs", [])

    # ---- write helpers (Issues permission) -------------------------------
    async def ensure_label(self, repo: str, name: str, color: str = "5319e7", description: str = "") -> None:
        resp = await self.request("GET", f"/repos/{self.org}/{repo}/labels/{name}", ok404=True)
        if resp.status == 404:
            await self.request("POST", f"/repos/{self.org}/{repo}/labels", json={"name": name, "color": color, "description": description})

    async def create_issue(self, repo: str, title: str, body: str, labels: list[str]) -> dict:
        resp = await self.request("POST", f"/repos/{self.org}/{repo}/issues", json={"title": title, "body": body, "labels": labels})
        return {"number": resp.body["number"], "html_url": resp.body["html_url"]}

    # ---- new-repo path (the only Administration use) ---------------------
    async def generate_repo_from_template(self, name: str, description: str) -> None:
        await self.request(
            "POST",
            f"/repos/{self.org}/{TEMPLATE_REPO}/generate",
            json={
                "owner": self.org,
                "name": name,
                "description": description,
                "private": False,
                "include_all_branches": False,
            },
        )

    async def wait_for_main(self, name: str, attempts: int = 40, delay: float = 3.0) -> str:
        for _ in range(attempts):
            resp = await self.request("GET", f"/repos/{self.org}/{name}/branches/main", ok404=True)
            if resp.status == 200 and resp.body.get("commit", {}).get("sha"):
                return resp.body["commit"]["sha"]
            await self._sleep(delay)
        raise TimeoutError(f"{self.org}/{name} has no commit on main yet")

    async def configure_new_repo(self, name: str) -> None:
        await self.request("PATCH", f"/repos/{self.org}/{name}", json=dict(REPO_SETTINGS))
        await self.request("PUT", f"/repos/{self.org}/{name}/branches/main/protection", json=dict(BRANCH_PROTECTION))
        for label, color, desc in NEW_REPO_LABELS:
            await self.ensure_label(name, label, color, desc)
