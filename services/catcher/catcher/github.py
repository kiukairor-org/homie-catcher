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
FORBIDDEN_REPOS = frozenset({"release", "papl", "homelab-gitops"})
# Never retired, restored, deleted or re-topicked by the catcher (DECISIONS D53).
RETIRE_PROTECTED = frozenset({"homelab-gitops", "app-template", "homie-catcher", "mattermost-arm64", "release", "papl"})
RETIRED_TOPIC = "retired"
STATUS_REPO = "homie-catcher"  # holds the budget-status issue that workflows read (D62)
READONLY_TOKEN_PERMISSIONS: dict[str, str] = {"contents": "read", "issues": "read", "pull_requests": "read", "metadata": "read"}

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
    Endpoint("GET", "/repos/{owner}/{repo}/topics"),
    Endpoint("GET", "/repos/{owner}/{repo}/contents"),
    Endpoint("GET", "/repos/{owner}/{repo}/contents/{name}"),
    Endpoint("GET", "/repos/{owner}/{repo}/issues"),
    Endpoint("GET", "/repos/{owner}/{repo}/issues/comments"),
    Endpoint("POST", "/repos/{owner}/{repo}/issues/{number}/comments"),
    Endpoint("PATCH", "/repos/{owner}/{repo}/issues/{number}"),
    # --- Administration permission: these only (bodies/targets pinned in check_allowed) ---
    Endpoint("POST", "/repos/{owner}/{repo}/generate", admin=True),
    Endpoint("POST", "/orgs/{org}/repos", admin=True),
    Endpoint("PATCH", "/repos/{owner}/{repo}", admin=True),
    Endpoint("PUT", "/repos/{owner}/{repo}/branches/main/protection", admin=True),
    Endpoint("PUT", "/repos/{owner}/{repo}/topics", admin=True),
    Endpoint("DELETE", "/repos/{owner}/{repo}", admin=True),
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
        # Owner hard limit: never touch these repos, whatever the caller asks (defence in depth over routing).
        if params.get("repo", "").lower() in FORBIDDEN_REPOS:
            raise EndpointNotAllowed(f"repo {params['repo']!r} is off-limits")
        if "installation_id" in params and params["installation_id"] != self.installation_id:
            raise EndpointNotAllowed("unexpected installation id")
        if ep.template.endswith("/access_tokens") and body is not None:
            # Only down-scoped READ tokens may be requested with a body (the #brainstorm tools).
            if not isinstance(body, dict):
                raise EndpointNotAllowed("token request body must be an object")
            perms, repos = body.get("permissions"), body.get("repositories")
            if (
                set(body) - {"permissions", "repositories"}
                or not isinstance(perms, dict)
                or not perms
                or any(v != "read" for v in perms.values())
                or set(perms) - set(READONLY_TOKEN_PERMISSIONS)
                or not isinstance(repos, list)
                or any(not isinstance(r, str) or r.lower() in FORBIDDEN_REPOS for r in repos)
            ):
                raise EndpointNotAllowed("token requests may only ask for read permissions on allowed repos")
        if ep.template == "/repos/{owner}/{repo}/generate" and params["repo"] != TEMPLATE_REPO:
            raise EndpointNotAllowed("repos may only be generated from the template repo")
        repo = params.get("repo", "").lower()
        if ep.template == "/repos/{owner}/{repo}" and ep.method == "PATCH":
            archive_toggle = isinstance(body, dict) and set(body) == {"archived"} and isinstance(body["archived"], bool)
            if body != REPO_SETTINGS and not archive_toggle:
                raise EndpointNotAllowed("PATCH /repos is limited to the new-repo settings or {archived: bool}")
            if archive_toggle and repo in RETIRE_PROTECTED:
                raise EndpointNotAllowed(f"repo {repo!r} is protected from retire/restore")
        if ep.template.endswith("/topics") and ep.method == "PUT":
            names = body.get("names") if isinstance(body, dict) else None
            if repo in RETIRE_PROTECTED or not isinstance(names, list) or set(body) != {"names"}:
                raise EndpointNotAllowed("topics may only be replaced on non-protected repos")
        if ep.method == "DELETE" and repo in RETIRE_PROTECTED:
            raise EndpointNotAllowed(f"repo {repo!r} is protected from deletion")
        if ep.method == "PATCH" and ep.template.endswith("/issues/{number}"):
            if repo != STATUS_REPO or not isinstance(body, dict) or set(body) - {"body"}:
                raise EndpointNotAllowed("issues may only be edited for the budget-status issue body")
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

    async def readonly_token(self, repos: list[str]) -> str:
        """A short-lived installation token limited to READ on ``repos`` (never the forbidden ones).

        Used only by the #brainstorm read-only GitHub MCP server; GitHub enforces the scope."""
        allowed = sorted({r for r in repos if r.lower() not in FORBIDDEN_REPOS})
        if not allowed:
            raise EndpointNotAllowed("no readable repos")
        resp = await self.request(
            "POST",
            f"/app/installations/{self.installation_id}/access_tokens",
            json={"repositories": allowed, "permissions": dict(READONLY_TOKEN_PERMISSIONS)},
        )
        return resp.body["token"]

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

    # ---- read helpers (app-channel context) --------------------------------
    async def list_root(self, repo: str) -> list[str]:
        resp = await self.request("GET", f"/repos/{self.org}/{repo}/contents")
        return [e.get("name") for e in resp.body or [] if isinstance(e, dict)]

    async def read_text(self, repo: str, name: str) -> str:
        import base64

        resp = await self.request("GET", f"/repos/{self.org}/{repo}/contents/{name}")
        body = resp.body or {}
        return base64.b64decode(body.get("content") or "").decode("utf-8", errors="replace") if body.get("encoding") == "base64" else ""

    async def open_issues(self, repo: str) -> list[dict]:
        resp = await self.request("GET", f"/repos/{self.org}/{repo}/issues", params={"state": "open", "per_page": 20})
        return resp.body or []

    # ---- comments / status issue (Issues permission) ----------------------
    async def comment(self, repo: str, number: int, body: str) -> dict:
        resp = await self.request("POST", f"/repos/{self.org}/{repo}/issues/{number}/comments", json={"body": body})
        return resp.body

    async def repo_comments_since(self, repo: str, since_iso: str) -> list[dict]:
        resp = await self.request(
            "GET", f"/repos/{self.org}/{repo}/issues/comments", params={"since": since_iso, "per_page": 100, "sort": "updated"}
        )
        return resp.body or []

    async def find_issue(self, repo: str, label: str) -> dict | None:
        resp = await self.request("GET", f"/repos/{self.org}/{repo}/issues", params={"labels": label, "state": "open", "per_page": 5})
        return (resp.body or [None])[0]

    async def set_issue_body(self, repo: str, number: int, body: str) -> None:
        await self.request("PATCH", f"/repos/{self.org}/{repo}/issues/{number}", json={"body": body})

    # ---- retire / restore / delete (Administration; protected repos refused in check_allowed) --
    async def repo_info(self, name: str) -> dict | None:
        resp = await self.request("GET", f"/repos/{self.org}/{name}", ok404=True)
        return None if resp.status == 404 else resp.body

    async def get_topics(self, name: str) -> list[str]:
        resp = await self.request("GET", f"/repos/{self.org}/{name}/topics")
        return list((resp.body or {}).get("names") or [])

    async def set_topics(self, name: str, topics: list[str]) -> None:
        await self.request("PUT", f"/repos/{self.org}/{name}/topics", json={"names": list(topics)})

    async def set_archived(self, name: str, archived: bool) -> None:
        await self.request("PATCH", f"/repos/{self.org}/{name}", json={"archived": archived})

    async def delete_repo(self, name: str) -> None:
        await self.request("DELETE", f"/repos/{self.org}/{name}")

    # ---- new-repo path ----------------------------------------------------
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
