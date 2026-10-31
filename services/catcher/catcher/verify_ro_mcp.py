"""Read-only verification MCP server (stdio) for the chat in an app channel ~<repo> (D74).

Lets the chat Claude answer "did it work?" from evidence instead of guessing. Standalone: stdlib + the
`mcp` package only, run with the plane-mcp venv's Python. Every tool is scoped to ONE repo (REPO env),
the app of the channel; there is no repo parameter to widen it.

Read-only by construction:
- GitHub: GET only, with an installation token the catcher DOWN-SCOPED to read on [REPO].
- Argo CD: GET of the single Application `app-<REPO>` through the Kubernetes API, with the catcher's
  service account (RBAC: get applications in argocd, nothing else).
- The app: GET /version and /healthz on its in-cluster Service (no auth, cluster-internal).
- New Relic: fixed NRQL templates (span count, error rate, top errors) on one account, filtered to
  service.name = REPO, through NerdGraph `nrql` queries only (never mutations). The user key is read
  from a mounted file and never returned.
Results are size-capped plain data; the model treats them as untrusted.

Env: GITHUB_TOKEN, GITHUB_ORG, REPO, NR_KEY_FILE, NEW_RELIC_ACCOUNT_ID, NEW_RELIC_REGION (US|EU),
K8S_SA_DIR (default /var/run/secrets/kubernetes.io/serviceaccount), ARGO_NAMESPACE (default argocd).
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # unit tests import the pure helpers without the mcp package

    class FastMCP:  # type: ignore[no-redef]
        def __init__(self, _name: str) -> None:
            pass

        def tool(self):  # noqa: ANN201
            return lambda f: f

        def run(self) -> None:
            raise SystemExit("mcp package missing")


API = "https://api.github.com"
ORG = os.environ.get("GITHUB_ORG", "")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("REPO", "")
NR_KEY_FILE = os.environ.get("NR_KEY_FILE", "")
NR_ACCOUNT = os.environ.get("NEW_RELIC_ACCOUNT_ID", "")
NR_URL = {"EU": "https://api.eu.newrelic.com/graphql", "US": "https://api.newrelic.com/graphql"}.get(
    os.environ.get("NEW_RELIC_REGION", "EU").upper(), "https://api.eu.newrelic.com/graphql"
)
SA_DIR = os.environ.get("K8S_SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
ARGO_NS = os.environ.get("ARGO_NAMESPACE", "argocd")
K8S_API = "https://kubernetes.default.svc"
_REPO_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
MAX_PATCH_CHARS = 1500
MAX_PATCH_TOTAL = 12_000

mcp = FastMCP("verify")


class ToolError(Exception):
    pass


def _scope() -> str:
    if not _REPO_RE.match(REPO) or REPO in ("release", "papl", "homelab-gitops"):
        raise ToolError("no app repo in scope for this channel")
    return REPO


def _http(url: str, headers: dict, data: bytes | None = None, ctx: ssl.SSLContext | None = None, timeout: int = 20):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310 - fixed hosts
            raw = resp.read().decode(errors="replace")
            try:
                return resp.status, json.loads(raw or "null")
            except ValueError:
                return resp.status, raw[:2000]
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"unreachable: {type(exc).__name__}") from None


def _gh(path: str, params: dict | None = None):
    url = f"{API}/repos/{ORG}/{_scope()}{path}" + ("?" + urllib.parse.urlencode(params) if params else "")
    status, body = _http(url, {"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "homie-catcher-verify"})  # fmt: skip
    if status != 200:
        raise ToolError(f"GitHub returned HTTP {status} for {path}")
    return body


def _login(u: dict | None) -> str | None:
    return (u or {}).get("login")


def _pr_summary(p: dict) -> dict:
    return {"number": p.get("number"), "title": p.get("title"), "state": p.get("state"), "merged_at": p.get("merged_at"),
            "merge_commit_sha": p.get("merge_commit_sha") if p.get("merged_at") else None, "author": _login(p.get("user")),
            "head_sha": (p.get("head") or {}).get("sha"), "created_at": p.get("created_at"), "url": p.get("html_url")}  # fmt: skip


@mcp.tool()
def list_pull_requests(state: str = "all", limit: int = 15) -> list[dict]:
    """Pull requests of this channel's app, newest first: number, title, state, merged_at, merge_commit_sha, url."""
    if state not in ("open", "closed", "all"):
        raise ToolError("state must be open, closed or all")
    limit = max(1, min(int(limit), 50))
    body = _gh("/pulls", {"state": state, "per_page": limit, "sort": "created", "direction": "desc"})
    return [_pr_summary(p) for p in body or []]


@mcp.tool()
def get_pull_request(number: int) -> dict:
    """One pull request: state, merged_at, merge commit, body, changed files with clipped diffs, reviews."""
    if not isinstance(number, int) or number < 1:
        raise ToolError("number must be a positive integer")
    p = _gh(f"/pulls/{number}")
    out = _pr_summary(p)
    out.update({"body": (p.get("body") or "")[:6000], "additions": p.get("additions"), "deletions": p.get("deletions"),
                "changed_files": p.get("changed_files"), "labels": [lb.get("name") for lb in p.get("labels") or []]})  # fmt: skip
    files, budget = [], MAX_PATCH_TOTAL
    for f in _gh(f"/pulls/{number}/files", {"per_page": 100}) or []:
        patch = (f.get("patch") or "")[: min(MAX_PATCH_CHARS, max(budget, 0))]
        budget -= len(patch)
        clipped = "…[clipped]" if len(f.get("patch") or "") > len(patch) else ""
        files.append({"file": f.get("filename"), "status": f.get("status"), "additions": f.get("additions"),
                      "deletions": f.get("deletions"), "patch": patch + clipped})  # fmt: skip
    out["files"] = files
    out["reviews"] = [{"user": _login(r.get("user")), "state": r.get("state"), "submitted_at": r.get("submitted_at"),
                       "body": (r.get("body") or "")[:1500]} for r in _gh(f"/pulls/{number}/reviews", {"per_page": 30}) or []]  # fmt: skip
    return out


@mcp.tool()
def list_workflow_runs(head_sha: str = "", branch: str = "", limit: int = 10) -> list[dict]:
    """GitHub Actions runs of this app (optionally for one commit or branch): workflow, event, status, conclusion."""
    params: dict = {"per_page": max(1, min(int(limit), 30))}
    if head_sha:
        if not _SHA.match(head_sha):
            raise ToolError("head_sha must be a hex commit SHA")
        params["head_sha"] = head_sha
    if branch:
        if not re.match(r"^[A-Za-z0-9._/-]{1,100}$", branch):
            raise ToolError("invalid branch")
        params["branch"] = branch
    body = _gh("/actions/runs", params) or {}
    return [{"workflow": r.get("name"), "event": r.get("event"), "status": r.get("status"), "conclusion": r.get("conclusion"),
             "head_sha": r.get("head_sha"), "branch": r.get("head_branch"), "created_at": r.get("created_at"),
             "url": r.get("html_url")} for r in body.get("workflow_runs") or []]  # fmt: skip


@mcp.tool()
def get_checks(ref: str) -> dict:
    """Check runs (CI, review, claude) for a commit SHA or branch of this app."""
    if not re.match(r"^[A-Za-z0-9._/-]{1,100}$", ref or ""):
        raise ToolError("invalid ref")
    body = _gh(f"/commits/{urllib.parse.quote(ref)}/check-runs", {"per_page": 50}) or {}
    runs = [{"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion"),
             "completed_at": c.get("completed_at"), "url": c.get("html_url")} for c in body.get("check_runs") or []]  # fmt: skip
    return {"ref": ref, "check_runs": runs}


@mcp.tool()
def commit_included(commit: str, in_version: str) -> dict:
    """Is `commit` (e.g. a PR's merge commit) contained in `in_version` (e.g. the deployed SHA)? Uses GitHub compare."""
    if not _SHA.match(commit or "") or not _SHA.match(in_version or ""):
        raise ToolError("both arguments must be hex commit SHAs")
    body = _gh(f"/compare/{commit}...{in_version}") or {}
    status = body.get("status")  # identical | ahead (in_version is newer) | behind | diverged
    return {"commit": commit, "in_version": in_version, "status": status, "included": status in ("identical", "ahead"),
            "commits_after": body.get("ahead_by"), "url": body.get("html_url")}  # fmt: skip


def _k8s_app() -> dict:
    ctx = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))
    with open(os.path.join(SA_DIR, "token")) as fh:
        token = fh.read().strip()
    name = f"app-{_scope()}"
    status, body = _http(f"{K8S_API}/apis/argoproj.io/v1alpha1/namespaces/{ARGO_NS}/applications/{name}",
                         {"Authorization": f"Bearer {token}", "Accept": "application/json"}, ctx=ctx)  # fmt: skip
    if status != 200 or not isinstance(body, dict):
        raise ToolError(f"Argo CD application {name}: HTTP {status}")
    return body


def argo_summary(app: dict) -> dict:
    """The parts of an Argo CD Application that say what is deployed (pure; unit-tested)."""
    spec, st = app.get("spec") or {}, app.get("status") or {}
    sources = spec.get("sources") or ([spec["source"]] if spec.get("source") else [])
    images = [i for s in sources for i in ((s.get("kustomize") or {}).get("images") or [])]
    op = st.get("operationState") or {}
    sync = st.get("sync") or {}
    return {"application": (app.get("metadata") or {}).get("name"), "sync": sync.get("status"),
            "health": (st.get("health") or {}).get("status"),
            "images": images, "last_sync": op.get("phase"), "last_sync_finished": op.get("finishedAt"),
            "last_sync_message": (op.get("message") or "")[:300]}  # fmt: skip


@mcp.tool()
def deploy_status() -> dict:
    """What is deployed: the Argo CD app app-<repo> (sync, health, image), and the live app's /version and /healthz."""
    repo = _scope()
    out: dict = {}
    try:
        out["argo"] = argo_summary(_k8s_app())
    except (ToolError, OSError) as exc:
        out["argo"] = {"error": str(exc)}
    base = f"http://app.app-{repo}.svc.cluster.local"
    for path in ("/version", "/healthz"):
        try:
            status, body = _http(base + path, {"Accept": "application/json"}, timeout=5)
            out[path.strip("/")] = {"http_status": status, "body": body if isinstance(body, dict) else str(body)[:300]}
        except ToolError as exc:
            out[path.strip("/")] = {"error": str(exc)}
    return out


def nrql_queries(service: str, minutes: int) -> dict[str, str]:
    """Fixed NRQL templates for one service (pure; unit-tested). Only a validated repo name and an int go in."""
    if not _REPO_RE.match(service) or not isinstance(minutes, int):
        raise ToolError("invalid service or window")
    w = f"SINCE {minutes} minutes ago"
    where = f"service.name = '{service}'"
    # NRQL, not SQL; only the validated repo name and an int are interpolated.
    return {
        "spans": f"SELECT count(*) AS spans, uniqueCount(trace.id) AS traces FROM Span WHERE {where} {w}",  # noqa: S608
        "server_errors": (
            "SELECT count(*) AS requests, filter(count(*), WHERE numeric(http.response.status_code) >= 500) AS http_5xx, "  # noqa: S608
            "filter(count(*), WHERE numeric(http.response.status_code) >= 400) AS http_4xx_or_5xx, "
            f"filter(count(*), WHERE otel.status_code = 'ERROR') AS error_spans FROM Span WHERE {where} AND span.kind = 'server' {w}"
        ),
        "top_errors": (
            f"SELECT count(*) FROM Span WHERE {where} AND (otel.status_code = 'ERROR' OR numeric(http.response.status_code) >= 400) "  # noqa: S608
            f"FACET name, http.response.status_code {w} LIMIT 10"
        ),
    }


@mcp.tool()
def newrelic_summary(since_minutes: int = 60) -> dict:
    """New Relic telemetry for this app (service.name = repo) over the last N minutes (5-10080): spans,
    traces, requests, HTTP 4xx/5xx and error spans, and the top error span names."""
    repo = _scope()
    minutes = max(5, min(int(since_minutes), 10080))
    if not NR_KEY_FILE or not os.path.exists(NR_KEY_FILE) or not NR_ACCOUNT.isdigit():
        raise ToolError("New Relic is not configured for the catcher")
    with open(NR_KEY_FILE) as fh:
        key = fh.read().strip()
    out: dict = {"account": int(NR_ACCOUNT), "service": repo, "window_minutes": minutes}
    for name, q in nrql_queries(repo, minutes).items():
        gql = "query($a:Int!,$q:Nrql!){actor{account(id:$a){nrql(query:$q){results}}}}"
        status, body = _http(NR_URL, {"API-Key": key, "Content-Type": "application/json"},
                             data=json.dumps({"query": gql, "variables": {"a": int(NR_ACCOUNT), "q": q}}).encode())  # fmt: skip
        if status != 200 or not isinstance(body, dict) or body.get("errors"):
            out[name] = {"error": f"NerdGraph HTTP {status}"}
            continue
        out[name] = ((((body.get("data") or {}).get("actor") or {}).get("account") or {}).get("nrql") or {}).get("results")
    return out


if __name__ == "__main__":
    mcp.run()
