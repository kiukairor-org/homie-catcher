"""In-memory fakes. No real network: every HTTP call hits FakeServices.send."""

from __future__ import annotations

import itertools
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from catcher.bot import Catcher
from catcher.config import Config
from catcher.db import DB
from catcher.github import GitHub
from catcher.mattermost import Mattermost
from catcher.plane import Plane
from catcher.transport import Response

OWNER = "owneruserid000000000000000"
STRANGER = "strangeruserid000000000000"
BOT = "botuserid00000000000000000"
IDEAS = "ideaschannelid000000000000"
AGENT_LOG = "agentlogchannelid000000000"
BRAINSTORM = "brainstormchannelid0000000"
ORG = "kiukairor-org"
SECRET = "s" * 40

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_KEY_PEM = _KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def make_config(**over) -> Config:
    env = {
        "MM_URL": "http://mm.local",
        "MM_BOT_TOKEN": "mm-bot-token-123456",
        "MM_TEAM": "homie",
        "OWNER_USER_IDS": OWNER,
        "CATCHER_ACTION_SECRET": SECRET,
        "PLANE_BASE_URL": "http://plane.local",
        "PLANE_PUBLIC_URL": "https://plane.tail8e3e5b.ts.net",
        "PLANE_API_KEY": "plane_api_key_abcdef",
        "PLANE_WORKSPACE_SLUG": "homie",
        "GITHUB_ORG": ORG,
        "GITHUB_APP_ID": "5530482",
        "GITHUB_INSTALLATION_ID": "176718952",
        "GITHUB_APP_PRIVATE_KEY_PATH": "/dev/null",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-faketokenvalue",
        "DB_PATH": ":memory:",
    }
    env.update(over)
    return Config.from_env(env)


@dataclass
class Call:
    method: str
    url: str
    json: Any = None
    params: Any = None
    headers: dict = field(default_factory=dict)

    @property
    def service(self) -> str:
        if self.url.startswith("https://api.github.com"):
            return "github"
        if self.url.startswith("http://plane.local"):
            return "plane"
        if self.url.startswith("http://mm.local"):
            return "mm"
        if ".svc.cluster.local/" in self.url:
            return "app"
        return "other"

    @property
    def path(self) -> str:
        return re.sub(r"^https?://[^/]+", "", self.url)

    @property
    def is_write(self) -> bool:
        return self.method != "GET"


class FakeServices:
    def __init__(self, repos=("agentic-demo", "papl", "release", "app-template", "homie-catcher", "notes")) -> None:
        self.calls: list[Call] = []
        self.repos = set(repos)
        self.posts: list[dict] = []
        self.plane_projects: list[dict] = []
        self.plane_states: dict[str, list[dict]] = {}
        self.plane_labels: dict[str, list[dict]] = {}
        self.plane_items: dict[str, dict] = {}
        self.gh_labels: dict[str, set[str]] = {}
        self.issues: list[dict] = []
        self.pulls: dict[str, list[dict]] = {}
        self.app_versions: dict[str, str] = {}  # in-cluster /version URL -> version
        self.files: dict[str, dict] = {}  # Mattermost file id -> {"info": {...}, "data": bytes}
        self.reviews: dict[int, list[dict]] = {}
        self.comments: dict[int, list[dict]] = {}
        self.check_runs: dict[str, list[dict]] = {}
        self._seq = itertools.count(1)

    # writes by service, excluding Mattermost chat traffic
    def writes(self, service: str) -> list[Call]:
        return [c for c in self.calls if c.service == service and c.is_write and not c.path.endswith("/access_tokens")]

    async def fetch_bytes(self, url, *, headers=None, max_bytes, timeout=30) -> bytes:
        self.calls.append(Call("GET", url, None, None, dict(headers or {})))
        fid = url.rsplit("/", 1)[1]
        return self.files[fid]["data"][: max_bytes + 1]

    async def send(self, method, url, *, headers=None, json=None, params=None, timeout=30) -> Response:
        call = Call(method, url, json, params, dict(headers or {}))
        self.calls.append(call)
        svc, path = call.service, call.path
        if svc == "app":  # in-cluster app GET /version probes
            v = self.app_versions.get(url)
            return Response(200, {"app": "x", "version": v}) if v else Response(503, None)
        handler = getattr(self, f"_{svc}")
        return handler(method, path, json, params)

    # ---- Mattermost ------------------------------------------------------
    def _mm(self, method, path, body, params):
        if path == "/api/v4/users/me":
            return Response(200, {"id": BOT})
        m = re.match(r"^/api/v4/teams/name/[^/]+/channels/name/([^/]+)$", path)
        if m:
            cid = {"ideas": IDEAS, "agent-log": AGENT_LOG, "brainstorm": BRAINSTORM}.get(m.group(1))
            return Response(200, {"id": cid}) if cid else Response(404, {"message": "not found"})
        m = re.match(r"^/api/v4/files/([^/]+)/info$", path)
        if m and m.group(1) in self.files:
            return Response(200, self.files[m.group(1)]["info"])
        if path == "/api/v4/posts" and method == "POST":
            post = {"id": f"post{next(self._seq)}", **body}
            self.posts.append(post)
            return Response(201, post)
        m = re.match(r"^/api/v4/posts/([^/]+)/patch$", path)
        if m and method == "PUT":
            post = next(p for p in self.posts if p["id"] == m.group(1))
            post.update(body)
            return Response(200, post)
        if path in ("/api/v4/reactions", "/api/v4/posts/ephemeral"):
            return Response(201, {})
        return Response(404, {"message": "not found"})

    # ---- Plane -----------------------------------------------------------
    def _plane(self, method, path, body, params):
        p = path.replace("/api/v1/workspaces/homie", "")
        if p == "/projects/" and method == "GET":
            return Response(200, {"results": self.plane_projects})
        if p == "/projects/" and method == "POST":
            proj = {"id": str(uuid.uuid4()), "name": body["name"], "identifier": body["identifier"]}
            self.plane_projects.append(proj)
            # Plane creates defaults incl. a hidden triage-group "Triage"
            self.plane_states[proj["id"]] = [
                {"id": str(uuid.uuid4()), "name": n, "group": g}
                for n, g in [("Backlog", "backlog"), ("Todo", "unstarted"), ("In Progress", "started"),
                             ("Done", "completed"), ("Cancelled", "cancelled")]
            ]  # fmt: skip
            self.plane_labels[proj["id"]] = []
            return Response(201, proj)
        m = re.match(r"^/projects/([^/]+)/states/$", p)
        if m:
            pid = m.group(1)
            if method == "GET":
                return Response(200, self.plane_states[pid])
            if body["name"] == "Triage":  # name taken by the hidden triage-group state
                return Response(409, {"error": "State with the same name already exists in the project"})
            st = {"id": str(uuid.uuid4()), "name": body["name"], "group": body["group"]}
            self.plane_states[pid].append(st)
            return Response(201, st)
        m = re.match(r"^/projects/([^/]+)/labels/$", p)
        if m:
            pid = m.group(1)
            if method == "GET":
                return Response(200, self.plane_labels[pid])
            lbl = {"id": str(uuid.uuid4()), "name": body["name"]}
            self.plane_labels[pid].append(lbl)
            return Response(201, lbl)
        m = re.match(r"^/projects/([^/]+)/issues/$", p)
        if m and method == "POST":
            iid = str(uuid.uuid4())
            self.plane_items[iid] = {"id": iid, "project": m.group(1), "sequence_id": len(self.plane_items) + 1, **body}
            return Response(201, self.plane_items[iid])
        m = re.match(r"^/projects/([^/]+)/issues/([^/]+)/$", p)
        if m and method == "PATCH":
            self.plane_items[m.group(2)].update(body)
            return Response(200, self.plane_items[m.group(2)])
        return Response(404, {"detail": "not found"})

    def plane_state_name(self, item_id: str) -> str:
        item = self.plane_items[item_id]
        return next(s["name"] for s in self.plane_states[item["project"]] if s["id"] == item["state"])

    # ---- GitHub ----------------------------------------------------------
    def _github(self, method, path, body, params):
        h = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "9999999999"}
        if re.match(r"^/app/installations/\d+/access_tokens$", path):
            return Response(201, {"token": "ghs_" + "x" * 36, "expires_at": "2099-01-01T00:00:00Z"}, h)
        if path == "/installation/repositories":
            return Response(200, {"repositories": [{"name": r, "owner": {"login": ORG}} for r in sorted(self.repos)]}, h)
        m = re.match(rf"^/repos/{ORG}/([^/]+)$", path)
        if m and method == "GET":
            return Response(200 if m.group(1) in self.repos else 404, {"name": m.group(1)}, h)
        if m and method == "PATCH":
            return Response(200, {}, h)
        m = re.match(rf"^/repos/{ORG}/app-template/generate$", path)
        if m:
            self.repos.add(body["name"])
            return Response(201, {"name": body["name"]}, h)
        m = re.match(rf"^/repos/{ORG}/([^/]+)/branches/main$", path)
        if m:
            return Response(200, {"commit": {"sha": "abc123"}}, h) if m.group(1) in self.repos else Response(404, {}, h)
        if re.match(rf"^/repos/{ORG}/[^/]+/branches/main/protection$", path):
            return Response(200, {}, h)
        m = re.match(rf"^/repos/{ORG}/([^/]+)/labels/([^/]+)$", path)
        if m:
            return Response(200 if m.group(2) in self.gh_labels.get(m.group(1), set()) else 404, {}, h)
        m = re.match(rf"^/repos/{ORG}/([^/]+)/labels$", path)
        if m:
            self.gh_labels.setdefault(m.group(1), set()).add(body["name"])
            return Response(201, {}, h)
        m = re.match(rf"^/repos/{ORG}/([^/]+)/issues$", path)
        if m:
            n = len(self.issues) + 1
            issue = {"number": n, "html_url": f"https://github.com/{ORG}/{m.group(1)}/issues/{n}", "repo": m.group(1), **body}
            self.issues.append(issue)
            return Response(201, issue, h)
        m = re.match(rf"^/repos/{ORG}/([^/]+)/pulls$", path)
        if m:
            return Response(200, self.pulls.get(m.group(1), []), h)
        m = re.match(rf"^/repos/{ORG}/[^/]+/pulls/(\d+)/reviews$", path)
        if m:
            return Response(200, self.reviews.get(int(m.group(1)), []), h)
        m = re.match(rf"^/repos/{ORG}/[^/]+/issues/(\d+)/comments$", path)
        if m:
            return Response(200, self.comments.get(int(m.group(1)), []), h)
        m = re.match(rf"^/repos/{ORG}/[^/]+/commits/([^/]+)/check-runs$", path)
        if m:
            return Response(200, {"check_runs": self.check_runs.get(m.group(1), [])}, h)
        return Response(404, {"message": "Not Found"}, h)


class FakeClaude:
    def __init__(self, outputs: list[Any] | None = None) -> None:
        self.outputs = list(outputs or [])
        self.prompts: list[str] = []

    async def run(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class FakeBrainstormRunner:
    """Records every brainstorm run; returns queued outputs (dicts) or raises queued exceptions."""

    def __init__(self, outputs: list[Any] | None = None) -> None:
        self.outputs = list(outputs or [])
        self.runs: list[dict] = []

    async def run(self, **kw) -> tuple[Any, list[str]]:
        self.runs.append(kw)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out, ["WebSearch"]


def spec(**over) -> dict:
    base = {
        "action": "create",
        "title": "Add dark mode",
        "user_story": "As a user, I want dark mode, so that my eyes rest.",
        "acceptance_criteria": ["A toggle switches themes", "Choice persists"],
        "labels": ["feature"],
    }
    base.update(over)
    return base


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


async def make_bot(outputs=None, services: FakeServices | None = None, **cfg_over):
    services = services or FakeServices()
    cfg = make_config(**cfg_over)
    clock = Clock()
    db = DB(":memory:", clock=clock)
    mm = Mattermost(services, url=cfg.mm_url, token=cfg.mm_bot_token)
    plane = Plane(services, base_url=cfg.plane_base_url, public_url=cfg.plane_public_url,
                  api_key=cfg.plane_api_key, workspace=cfg.plane_workspace_slug)  # fmt: skip

    async def no_sleep(_s):
        return None

    gh = GitHub(services, org=ORG, app_id=cfg.github_app_id, installation_id=cfg.github_installation_id,
                private_key_pem=PRIVATE_KEY_PEM, clock=clock, sleep=no_sleep)  # fmt: skip
    claude = FakeClaude(outputs)
    bot = Catcher(cfg, db, mm, plane, gh, claude, clock=clock)
    await bot.startup()
    return bot, services, claude


_post_ids = itertools.count(1)


def post(message: str, user: str = OWNER, root: str = "", channel: str = IDEAS, **extra) -> dict:
    pid = f"userpost{next(_post_ids):018d}"
    return {"id": pid, "user_id": user, "channel_id": channel, "message": message, "root_id": root, "type": "", **extra}


def last_buttons(services: FakeServices) -> list[dict]:
    for p in reversed(services.posts):
        atts = (p.get("props") or {}).get("attachments") or []
        if atts and atts[0].get("actions"):
            return atts[0]["actions"]
    return []


def click(btn: dict, user: str = OWNER) -> tuple[str, dict]:
    name = btn["integration"]["url"].rsplit("/", 1)[1]
    return name, {"user_id": user, "context": dict(btn["integration"]["context"])}
