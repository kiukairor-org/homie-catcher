"""Owner-only app lifecycle in #ideas: `retire <repo>`, `restore <repo>`, `delete <repo>` (DECISIONS D53-D56).

retire  = topic `retired` (both ApplicationSets drop the repo -> Argo CD prunes app, namespace, previews),
          then archive the GitHub repo (never deleted), archive its Plane project and Mattermost channel.
restore = the exact reverse (unarchive, original topics back, Plane + channel restored).
delete  = permanent, only for an already-retired repo: deletes the GitHub repo and the Plane project
          (the Mattermost channel stays archived: history is kept).

Confirmation: retire and delete need a signed owner-only button AND typing the repo name in the thread
within 10 minutes; restore needs the button. Protected repos are refused here and again in GitHub.check_allowed.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import buttons
from .github import RETIRE_PROTECTED, RETIRED_TOPIC
from .text import defang

log = logging.getLogger(__name__)

CONFIRM_TTL_S = 600
_CMD = re.compile(r"^\s*(retire|restore|delete)\s+([A-Za-z0-9._-]{1,100})\s*$", re.I)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS admin_ops(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  op TEXT NOT NULL,
  repo TEXT NOT NULL,
  root_post_id TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  log TEXT
);
CREATE TABLE IF NOT EXISTS repo_state(
  repo TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  topics_json TEXT NOT NULL DEFAULT '[]',
  plane_project_id TEXT,
  channel_id TEXT,
  updated_at REAL NOT NULL
);
"""


def parse_command(text: str) -> tuple[str, str] | None:
    m = _CMD.match(text or "")
    return (m.group(1).lower(), m.group(2).lower()) if m else None


class Admin:
    def __init__(self, catcher: Any) -> None:
        self.c = catcher
        self.cfg = catcher.cfg
        catcher.db._conn.executescript(SCHEMA_SQL)

    # ---------------------------------------------------------------- db
    def _x(self, sql: str, args: tuple = ()):  # noqa: ANN202
        return self.c.db._x(sql, args)

    def op(self, op_id: int):  # noqa: ANN201
        return self._x("SELECT * FROM admin_ops WHERE id=?", (op_id,)).fetchone()

    def open_op(self, root: str):  # noqa: ANN201
        return self._x(
            "SELECT * FROM admin_ops WHERE root_post_id=? AND status IN ('planned','confirm_name') ORDER BY id DESC LIMIT 1", (root,)
        ).fetchone()

    def _transition(self, op_id: int, froms: tuple[str, ...], to: str) -> bool:
        marks = ",".join("?" * len(froms))
        cur = self._x(f"UPDATE admin_ops SET status=? WHERE id=? AND status IN ({marks})", (to, op_id, *froms))  # noqa: S608
        return cur.rowcount == 1

    def state(self, repo: str):  # noqa: ANN201
        return self._x("SELECT * FROM repo_state WHERE repo=?", (repo,)).fetchone()

    def _save_state(self, repo: str, state: str, **kw: Any) -> None:
        cur = self.state(repo)
        topics = kw.get("topics_json", cur["topics_json"] if cur else "[]")
        plane = kw.get("plane_project_id", cur["plane_project_id"] if cur else None)
        chan = kw.get("channel_id", cur["channel_id"] if cur else None)
        self._x(
            "INSERT INTO repo_state(repo, state, topics_json, plane_project_id, channel_id, updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(repo) DO UPDATE SET state=excluded.state, topics_json=excluded.topics_json, "
            "plane_project_id=excluded.plane_project_id, channel_id=excluded.channel_id, updated_at=excluded.updated_at",
            (repo, state, topics, plane, chan, self.c.clock()),
        )

    def is_retired(self, repo: str) -> bool:
        st = self.state(repo)
        return bool(st and st["state"] in ("retired", "deleted"))

    # ---------------------------------------------------------------- chat
    async def handle_command(self, root: str, op: str, repo: str) -> None:
        say = self.c.say
        if repo in RETIRE_PROTECTED:
            await say(root, f"🛡️ `{defang(repo)}` is protected and can never be retired, restored or deleted.")
            return
        info = await self.c.gh.repo_info(repo)
        if info is None:
            await say(root, f"There is no repo `kiukairor-org/{defang(repo)}`.")
            return
        topics = await self.c.gh.get_topics(repo) if not info.get("archived") else None
        retired = bool(info.get("archived")) and self.is_retired(repo)
        if op == "retire" and info.get("archived"):
            await say(root, f"`{repo}` is already archived{' (retired)' if retired else ''}. Use `restore {repo}` or `delete {repo}`.")
            return
        if op == "restore" and not info.get("archived"):
            await say(root, f"`{repo}` is not retired, nothing to restore.")
            return
        if op == "delete" and not retired:
            await say(root, f"`delete` only works on a retired repo. Run `retire {repo}` first.")
            return
        proj = await self._plane_project(repo)
        chan = await self.c.mm.channel_by_name(self.cfg.mm_team, repo)
        plan = self._plan(op, repo, topics, proj, chan)
        now = self.c.clock()
        cur = self._x(
            "INSERT INTO admin_ops(op, repo, root_post_id, status, created_at, expires_at) VALUES(?,?,?,?,?,?)",
            (op, repo, root, "planned", now, now + CONFIRM_TTL_S),
        )
        op_id = cur.lastrowid
        label = {"retire": "Retire", "restore": "Restore", "delete": "Delete permanently"}[op]
        style = "primary" if op == "restore" else "danger"
        b = self.cfg.action_base_url, self.cfg.action_secret
        actions = [
            buttons.button(*b, label, "adm_confirm", {"op": op_id, "repo": repo}, style),
            buttons.button(*b, "Cancel", "adm_cancel", {"op": op_id, "repo": repo}),
        ]
        await say(root, plan, attachments=[{"text": "Only the owner can confirm.", "actions": actions}])

    def _plan(self, op: str, repo: str, topics: list[str] | None, proj: dict | None, chan: dict | None) -> str:
        p = f"Plane project `{proj['identifier']}`" if proj else "no Plane project"
        c = f"channel ~{repo}" if chan else "no Mattermost channel"
        if op == "retire":
            return "\n".join([
                f"**Retire `kiukairor-org/{repo}`?** Plan:",
                f"1. GitHub: replace topics {topics or '[]'} with `{RETIRED_TOPIC}` → the ApplicationSets stop generating "
                f"`app-{repo}` and its previews → Argo CD prunes the app, its namespace and previews (within ~5–10 min).",
                "2. GitHub: **archive** the repo (read-only; never deleted).",
                f"3. Plane: archive {p}.",
                f"4. Mattermost: archive {c}.",
                f"Reversible with `restore {repo}`. Confirm with the button, then type `{repo}`.",
            ])  # fmt: skip
        if op == "restore":
            return "\n".join([
                f"**Restore `kiukairor-org/{repo}`?** Plan:",
                "1. GitHub: unarchive the repo, then put its original topics back (drops `retired`) → Argo CD deploys it again.",
                f"2. Plane: unarchive {p}.",
                f"3. Mattermost: restore {c}.",
            ])  # fmt: skip
        return "\n".join([
            f"**Permanently delete `kiukairor-org/{repo}`?** This cannot be undone.",
            "1. GitHub: **delete** the repo (code, issues and PRs are gone).",
            f"2. Plane: **delete** {p}.",
            f"3. Mattermost: {c} stays archived (history is kept).",
            "Container images in GHCR are not deleted (the bot has no packages permission).",
            f"Confirm with the button, then type `{repo}`.",
        ])  # fmt: skip

    async def handle_reply(self, root: str, text: str) -> bool:
        """True if the reply belonged to an admin op (consumed)."""
        op = self.open_op(root)
        if op is None:
            return False
        if op["status"] != "confirm_name":
            await self.c.say(root, "Use the buttons above first.")
            return True
        if self.c.clock() > op["expires_at"]:
            self._transition(op["id"], ("confirm_name",), "expired")
            await self.c.say(root, f"⌛ Confirmation expired. Send `{op['op']} {op['repo']}` again.")
            return True
        if text.strip().lower() != op["repo"]:
            await self.c.say(root, f"That doesn't match. Type exactly `{op['repo']}` to confirm, or press Cancel.")
            return True
        if self._transition(op["id"], ("confirm_name",), "running"):
            await self._run(op["id"])
        return True

    # ---------------------------------------------------------------- buttons
    async def handle_action(self, name: str, ctx: dict) -> dict:
        op_id = ctx.get("op")
        op = self.op(op_id) if isinstance(op_id, int) else None
        if op is None or ctx.get("repo") != op["repo"]:
            return {"ephemeral_text": "not allowed"}
        if self.c.clock() > op["expires_at"]:
            self._transition(op_id, ("planned", "confirm_name"), "expired")
            return {"ephemeral_text": "Expired. Send the command again."}
        if name == "adm_cancel":
            if self._transition(op_id, ("planned", "confirm_name"), "cancelled"):
                self.c.spawn(self.c.say(op["root_post_id"], "Cancelled. Nothing was changed."))
                return {"ephemeral_text": "Cancelled."}
            return {"ephemeral_text": "Already handled."}
        if op["op"] == "restore":
            if not self._transition(op_id, ("planned",), "running"):
                return {"ephemeral_text": "Already handled."}
            self.c.spawn(self._run(op_id))
            return {"ephemeral_text": f"Restoring {op['repo']}…"}
        if not self._transition(op_id, ("planned",), "confirm_name"):
            return {"ephemeral_text": "Already handled."}
        self.c.spawn(self.c.say(op["root_post_id"], f"Step 2 of 2: type `{op['repo']}` in this thread to confirm (10 min)."))
        return {"ephemeral_text": "Now type the repo name."}

    # ---------------------------------------------------------------- execution
    async def _plane_project(self, repo: str) -> dict | None:
        cached = self.c.db.plane_project(repo)
        st = self.state(repo)
        pid = (cached.project_id if cached else None) or (st["plane_project_id"] if st else None)
        if pid:
            return {"id": pid, "identifier": cached.identifier if cached else pid[:8]}
        found = await self.c.plane.find_project(repo)
        return {"id": found["id"], "identifier": found.get("identifier", "?")} if found else None

    async def _run(self, op_id: int) -> None:
        op = self.op(op_id)
        repo, root, kind = op["repo"], op["root_post_id"], op["op"]
        steps: list[str] = []

        async def step(label: str, coro) -> bool:  # noqa: ANN001
            try:
                await coro
                steps.append(f"✅ {label}")
                return True
            except Exception as exc:  # noqa: BLE001 - report every step
                log.exception("%s %s: %s failed", kind, repo, label)
                steps.append(f"⚠️ {label}: {type(exc).__name__}")
                return False

        proj = await self._plane_project(repo)
        chan = await self.c.mm.channel_by_name(self.cfg.mm_team, repo)
        ok = True
        if kind == "retire":
            topics = await self.c.gh.get_topics(repo)
            self._save_state(repo, "retiring", topics_json=json.dumps(topics), plane_project_id=proj["id"] if proj else None,
                             channel_id=chan["id"] if chan else None)  # fmt: skip
            ok = await step(f"GitHub topics {topics or '[]'} → [`{RETIRED_TOPIC}`] (Argo CD prunes app-{repo} + previews)",
                            self.c.gh.set_topics(repo, [RETIRED_TOPIC]))  # fmt: skip
            ok = ok and await step("GitHub repo archived", self.c.gh.set_archived(repo, True))
            if ok:
                if proj:
                    await step(f"Plane project {proj['identifier']} archived", self.c.plane.archive_project(proj["id"]))
                if chan and not chan.get("delete_at"):
                    await step(f"Mattermost ~{repo} archived", self.c.mm.archive_channel(chan["id"]))
                self._save_state(repo, "retired")
            self.c.forget_repo_cache()
        elif kind == "restore":
            st = self.state(repo)
            topics = [t for t in json.loads(st["topics_json"]) if t != RETIRED_TOPIC] if st else []
            ok = await step("GitHub repo unarchived", self.c.gh.set_archived(repo, False))
            ok = ok and await step(f"GitHub topics → {topics or '[]'} (Argo CD deploys it again)", self.c.gh.set_topics(repo, topics))
            if ok:
                if proj:
                    await step(f"Plane project {proj['identifier']} unarchived", self.c.plane.unarchive_project(proj["id"]))
                if chan and chan.get("delete_at"):
                    await step(f"Mattermost ~{repo} restored", self.c.mm.restore_channel(chan["id"]))
                self._save_state(repo, "active")
            self.c.forget_repo_cache()
        else:  # delete
            ok = await step("GitHub repo deleted", self.c.gh.delete_repo(repo))
            if ok:
                if proj:
                    await step(f"Plane project {proj['identifier']} deleted", self.c.plane.delete_project(proj["id"]))
                    self.c.db.forget_plane_project(repo)
                steps.append(f"ℹ️ Mattermost ~{repo} stays archived (history kept)")
                self._save_state(repo, "deleted")
        self._transition(op_id, ("running",), "done" if ok else "failed")
        self._x("UPDATE admin_ops SET log=? WHERE id=?", ("\n".join(steps), op_id))
        head = {"retire": "🗄️ Retired", "restore": "♻️ Restored", "delete": "🗑️ Deleted"}[kind] if ok else f"⚠️ {kind} incomplete"
        await self.c.say(root, f"{head} `kiukairor-org/{repo}`\n" + "\n".join(steps))
        if self.c.agent_log_channel_id:
            await self.c.mm.post(self.c.agent_log_channel_id, f"[{repo}] {head} by owner ({len(steps)} steps)")
