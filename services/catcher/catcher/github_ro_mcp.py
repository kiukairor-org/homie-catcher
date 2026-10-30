"""Read-only GitHub MCP server (stdio) for #brainstorm. Standalone: stdlib + the `mcp` package only,
run with the plane-mcp venv's Python (/opt/plane-mcp/bin/python).

Defence in depth, three layers:
1. The token it gets is a kiu-homie-bot installation token DOWN-SCOPED by the catcher to
   contents/issues/pull_requests/metadata = read, and to an explicit repository list that excludes
   release, papl and homelab-gitops. GitHub itself refuses anything else.
2. Only GET requests exist in this file; every tool checks the repo against ALLOWED_REPOS.
3. Results are size-capped and returned as plain data (the model treats them as untrusted).

Env: GITHUB_TOKEN, GITHUB_ORG, ALLOWED_REPOS (comma-separated).
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

API = "https://api.github.com"
ORG = os.environ.get("GITHUB_ORG", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
ALLOWED = frozenset(r.strip() for r in os.environ.get("ALLOWED_REPOS", "").split(",") if r.strip())
MAX_FILE_CHARS = 60_000
MAX_LIST = 100
_PATH = re.compile(r"^[A-Za-z0-9._/@+-]{0,300}$")
_REF = re.compile(r"^[A-Za-z0-9._/-]{0,100}$")

mcp = FastMCP("github_ro")


class ToolError(Exception):
    pass


def _repo(repo: str) -> str:
    if repo not in ALLOWED:
        raise ToolError(f"repo {repo!r} is not readable here; readable repos: {', '.join(sorted(ALLOWED))}")
    return repo


def _get(path: str, params: dict | None = None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(  # noqa: S310 - fixed https://api.github.com host
        url,
        method="GET",
        headers={
            "Authorization": f"token {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "homie-catcher-brainstorm",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - fixed https host
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        raise ToolError(f"GitHub returned HTTP {exc.code} for {path}") from None


def _clean(path: str, ref: str) -> tuple[str, dict]:
    path = path.strip().lstrip("/")
    if not _PATH.match(path) or ".." in path.split("/"):
        raise ToolError("invalid path")
    if not _REF.match(ref or ""):
        raise ToolError("invalid ref")
    return urllib.parse.quote(path), ({"ref": ref} if ref else {})


@mcp.tool()
def list_repos() -> list[dict]:
    """List the org repositories you can read (name, description, default branch, visibility)."""
    out = []
    for name in sorted(ALLOWED):
        try:
            r = _get(f"/repos/{ORG}/{name}")
            out.append({"name": name, "description": r.get("description") or "", "default_branch": r.get("default_branch"),
                        "visibility": r.get("visibility"), "pushed_at": r.get("pushed_at")})  # fmt: skip
        except ToolError:
            continue
    return out


@mcp.tool()
def list_dir(repo: str, path: str = "", ref: str = "") -> list[dict]:
    """List a directory of a repo (default branch unless ref is given)."""
    p, params = _clean(path, ref)
    body = _get(f"/repos/{ORG}/{_repo(repo)}/contents/{p}", params)
    if not isinstance(body, list):
        raise ToolError("not a directory (use read_file)")
    return [{"name": e.get("name"), "type": e.get("type"), "path": e.get("path"), "size": e.get("size")} for e in body[:500]]


@mcp.tool()
def read_file(repo: str, path: str, ref: str = "") -> str:
    """Read a text file from a repo (default branch unless ref is given). Long files are truncated."""
    p, params = _clean(path, ref)
    body = _get(f"/repos/{ORG}/{_repo(repo)}/contents/{p}", params)
    if not isinstance(body, dict) or body.get("type") != "file":
        raise ToolError("not a file (use list_dir)")
    if body.get("encoding") != "base64":
        raise ToolError("file too large to read through this tool")
    text = base64.b64decode(body.get("content") or "").decode("utf-8", errors="replace")
    return text if len(text) <= MAX_FILE_CHARS else text[:MAX_FILE_CHARS] + "\n…[truncated]"


def _issue_summary(i: dict) -> dict:
    return {"number": i.get("number"), "title": i.get("title"), "state": i.get("state"),
            "is_pull_request": "pull_request" in i, "labels": [lb.get("name") for lb in i.get("labels") or []],
            "user": (i.get("user") or {}).get("login"), "updated_at": i.get("updated_at"), "url": i.get("html_url")}  # fmt: skip


@mcp.tool()
def list_issues(repo: str, state: str = "open") -> list[dict]:
    """List issues and pull requests of a repo. state: open, closed or all."""
    if state not in ("open", "closed", "all"):
        raise ToolError("state must be open, closed or all")
    body = _get(f"/repos/{ORG}/{_repo(repo)}/issues", {"state": state, "per_page": MAX_LIST})
    return [_issue_summary(i) for i in body or []]


@mcp.tool()
def get_issue(repo: str, number: int) -> dict:
    """Read one issue or pull request with its body and up to 30 comments."""
    if not isinstance(number, int) or number < 1:
        raise ToolError("number must be a positive integer")
    r = _repo(repo)
    i = _get(f"/repos/{ORG}/{r}/issues/{number}")
    comments = _get(f"/repos/{ORG}/{r}/issues/{number}/comments", {"per_page": 30}) or []
    out = _issue_summary(i)
    out["body"] = (i.get("body") or "")[:20_000]
    out["comments"] = [{"user": (c.get("user") or {}).get("login"), "body": (c.get("body") or "")[:4000]} for c in comments]
    return out


if __name__ == "__main__":
    mcp.run()
