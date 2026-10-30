"""Plane CE REST v1 client (``/api/v1/workspaces/<slug>/...``, header ``X-API-Key``).

Notes from the Plane v1.4.2 source:
- Creating a project creates the default states Backlog/Todo/In Progress/Done/Cancelled
  plus a hidden ``Triage`` state in the special ``triage`` group. That group is reserved for
  Intake: the API refuses to create triage-group states ("Cannot create triage state"), hides
  them from listings, and won't accept them as a work item's state. So our "Triage" lives in
  the ``backlog`` group, and since the name "Triage" is already taken by the hidden state,
  ``create`` falls back to the next candidate name.
- ``/<slug>/projects/<project_id>/issues/<issue_id>`` is a valid CE web URL; it redirects to
  the canonical ``/<slug>/browse/<IDENT>-<seq>/``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .transport import HttpError, Response, Transport

log = logging.getLogger(__name__)

# key -> (candidate names, group). First name that exists or can be created wins.
STATES: dict[str, tuple[tuple[str, ...], str]] = {
    "triage": (("Needs triage",), "backlog"),  # "Triage" is Plane's hidden intake state (unlisted; duplicate -> 500)
    "ready": (("Ready for agent",), "unstarted"),
    "in_progress": (("In progress",), "started"),
    "in_review": (("In review",), "started"),
    "done": (("Done",), "completed"),
    "rejected": (("Rejected",), "cancelled"),
}
STATE_COLORS = {
    "triage": "#9AA4BC",
    "ready": "#3F76FF",
    "in_progress": "#F59E0B",
    "in_review": "#8B5CF6",
    "done": "#46A758",
    "rejected": "#DC2626",
}
LABELS = {"feature": "#3F76FF", "bug": "#DC2626", "chore": "#60646C"}


@dataclass
class Project:
    repo: str
    project_id: str
    identifier: str
    states: dict[str, str] = field(default_factory=dict)  # key -> state id
    labels: dict[str, str] = field(default_factory=dict)  # name -> label id


def project_name_for(repo: str) -> str:
    """Plane rejects names containing any of &+,:;$^}{*=?@#|'<>.()%!- (FORBIDDEN_IDENTIFIER_CHARS_PATTERN)."""
    return re.sub(r"[^A-Za-z0-9_ ]", "_", repo)


def identifier_for(repo: str) -> str:
    ident = re.sub(r"[^A-Za-z0-9]", "", repo).upper()[:12]
    return ident or "APP"


def _results(body: Any) -> list[dict]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("results") or []
    return []


class Plane:
    def __init__(self, transport: Transport, *, base_url: str, public_url: str, api_key: str, workspace: str) -> None:
        self.transport = transport
        self.api = f"{base_url.rstrip('/')}/api/v1/workspaces/{workspace}"
        self.public_url = public_url.rstrip("/")
        self.workspace = workspace
        self._key = api_key

    async def _req(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> Response:
        resp = await self.transport.send(
            method, self.api + path, headers={"X-API-Key": self._key, "Content-Type": "application/json"}, json=json, params=params
        )
        if not resp.ok:
            raise HttpError(method, path, resp.status, resp.body)
        return resp

    def item_url(self, project_id: str, item_id: str) -> str:
        return f"{self.public_url}/{self.workspace}/projects/{project_id}/issues/{item_id}"

    # ---- projects ----------------------------------------------------------
    async def find_project(self, name: str) -> dict | None:
        resp = await self._req("GET", "/projects/", params={"per_page": 100})
        for p in _results(resp.body):
            if p.get("name", "").lower() in (name.lower(), project_name_for(name).lower()):
                return p
        return None

    async def create_project(self, repo: str) -> dict:
        base = identifier_for(repo)
        last: Exception | None = None
        for attempt in range(10):
            ident = base if attempt == 0 else f"{base[:10]}{attempt + 1}"
            try:
                resp = await self._req(
                    "POST",
                    "/projects/",
                    json={
                        "name": project_name_for(repo),
                        "identifier": ident,
                        "description": f"kiukairor-org/{repo} (homie-catcher)",
                        "network": 2,
                    },
                )
                return resp.body
            except HttpError as exc:
                last = exc
                if exc.status not in (400, 409) or "identifier" not in str(exc.body).lower():
                    raise
        raise last  # type: ignore[misc]

    async def ensure_project(self, repo: str, cached: Project | None = None) -> Project:
        if cached and set(STATES) <= set(cached.states) and set(LABELS) <= set(cached.labels):
            return cached
        if cached:
            proj = Project(repo, cached.project_id, cached.identifier)
        else:
            found = await self.find_project(repo) or await self.create_project(repo)
            proj = Project(repo, found["id"], found.get("identifier", identifier_for(repo)))
        proj.states = await self._ensure_states(proj.project_id)
        proj.labels = await self._ensure_labels(proj.project_id)
        return proj

    async def _ensure_states(self, project_id: str) -> dict[str, str]:
        resp = await self._req("GET", f"/projects/{project_id}/states/")
        states = _results(resp.body)
        existing = {s["name"].lower(): s for s in states if s.get("group") != "triage"}
        # Plane's own intake state (group "triage") reserves its name; creating a duplicate is a 500, not a 409.
        reserved = {s["name"].lower() for s in states if s.get("group") == "triage"}
        out: dict[str, str] = {}
        for key, (names, group) in STATES.items():
            hit = next((existing[n.lower()] for n in names if n.lower() in existing), None)
            if hit:
                out[key] = hit["id"]
                continue
            for name in names:
                if name.lower() in reserved:
                    log.info("state %r is reserved by Plane, trying next name", name)
                    continue
                try:
                    created = await self._req(
                        "POST", f"/projects/{project_id}/states/", json={"name": name, "group": group, "color": STATE_COLORS[key]}
                    )
                    out[key] = created.body["id"]
                    break
                except HttpError as exc:
                    if exc.status not in (400, 409):
                        raise
                    log.info("state %r not creatable (%s), trying next name", name, exc.status)
            if key not in out:
                raise RuntimeError(f"could not create Plane state for {key}")
        return out

    async def _ensure_labels(self, project_id: str) -> dict[str, str]:
        resp = await self._req("GET", f"/projects/{project_id}/labels/")
        existing = {lbl["name"].lower(): lbl["id"] for lbl in _results(resp.body)}
        out: dict[str, str] = {}
        for name, color in LABELS.items():
            if name in existing:
                out[name] = existing[name]
            else:
                created = await self._req("POST", f"/projects/{project_id}/labels/", json={"name": name, "color": color})
                out[name] = created.body["id"]
        return out

    # ---- work items --------------------------------------------------------
    async def create_work_item(self, proj: Project, *, title: str, description_html: str, labels: list[str]) -> dict:
        resp = await self._req(
            "POST",
            f"/projects/{proj.project_id}/issues/",
            json={
                "name": title,
                "description_html": description_html,
                "state": proj.states["triage"],
                "labels": [proj.labels[n] for n in labels if n in proj.labels],
            },
        )
        return {"id": resp.body["id"], "sequence_id": resp.body.get("sequence_id")}

    async def set_state(self, proj: Project, item_id: str, key: str) -> None:
        await self._req("PATCH", f"/projects/{proj.project_id}/issues/{item_id}/", json={"state": proj.states[key]})
