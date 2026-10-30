"""One private Mattermost channel per org app repo, ~<repo> (DECISIONS D57-D59).

- Created when the new-idea flow creates a repo, and reconciled (backfill) at startup and every 30 min
  for every selectable org repo that is not archived/retired. An archived channel (retired app) is never
  recreated or restored here; only `restore <repo>` does that.
- Work-item progress for an existing repo moves out of #ideas into a new thread in ~<repo>
  (the item's root post is re-pointed, so buttons, milestones and replies all follow).
- Owner posts in ~<repo> are brainstorm-style chats with that repo's context (catcher/brainstorm.py).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from .routing import selectable_repos
from .text import defang

log = logging.getLogger(__name__)

RECONCILE_EVERY_S = 1800

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_channels(
  repo TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""


class AppChannels:
    def __init__(self, catcher: Any) -> None:
        self.c = catcher
        self.cfg = catcher.cfg
        catcher.db._conn.executescript(SCHEMA_SQL)
        self._team_id: str | None = None
        self._last_reconcile = 0.0

    def _x(self, sql: str, args: tuple = ()):  # noqa: ANN202
        return self.c.db._x(sql, args)

    def repo_for_channel(self, channel_id: str | None) -> str | None:
        if not channel_id:
            return None
        row = self._x("SELECT repo FROM app_channels WHERE channel_id=?", (channel_id,)).fetchone()
        return row["repo"] if row else None

    def channel_for_repo(self, repo: str) -> str | None:
        row = self._x("SELECT channel_id FROM app_channels WHERE repo=?", (repo,)).fetchone()
        return row["channel_id"] if row else None

    async def ensure(self, repo: str, intro: bool = True) -> str | None:
        """Channel id of ~<repo>, creating it if missing. None if it is archived (retired app) or the feature is off."""
        if not self.cfg.app_channels:
            return None
        if self.c.admin is not None and self.c.admin.is_retired(repo):
            return None
        ch = await self.c.mm.channel_by_name(self.cfg.mm_team, repo)
        if ch and ch.get("delete_at"):
            return None
        if ch is None:
            if self._team_id is None:
                self._team_id = await self.c.mm.team_id(self.cfg.mm_team)
            ch = await self.c.mm.create_private_channel(
                self._team_id, repo, repo, f"App channel for kiukairor-org/{repo}: chat with Claude about it, work-item progress."
            )
            log.info("created app channel ~%s", repo)
            for uid in self.cfg.owner_user_ids:
                try:
                    await self.c.mm.add_member(ch["id"], uid)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not add owner to ~%s: %s", repo, exc)
            if intro:
                await self.c.mm.post(ch["id"], (
                    f"👋 This is the channel for **kiukairor-org/{repo}**.\n"
                    "- Post a message to chat with Claude about this app (it reads the repo, issues, Plane and the web; "
                    "it never changes anything).\n"
                    "- Type `do it` (or press **Promote**) to turn the chat into a feature request: draft → **Send** → "
                    "**Approve**, already targeted at this repo.\n"
                    "- Progress of this app's work items is posted here."
                ))  # fmt: skip
        self._x("INSERT OR REPLACE INTO app_channels(repo, channel_id, created_at) VALUES(?,?,?)", (repo, ch["id"], self.c.clock()))
        return ch["id"]

    async def reconcile(self, force: bool = False) -> list[str]:
        """Backfill: make sure every active app repo has its channel. Returns repos that have one."""
        if not self.cfg.app_channels:
            return []
        now = time.time()
        if not force and now - self._last_reconcile < RECONCILE_EVERY_S:
            return []
        self._last_reconcile = now
        done = []
        for repo in selectable_repos(await self.c.org_repos()):
            try:
                if await self.ensure(repo):
                    done.append(repo)
            except Exception:
                log.exception("app channel for %s failed", repo)
        return done

    async def move_item(self, item_id: int, repo: str, heading: str) -> bool:
        """Re-point an #ideas item to a new thread in ~<repo>; leaves a pointer in the old thread."""
        ch = await self.ensure(repo)
        if ch is None:
            return False
        item = self.c.db.item(item_id)
        old_root = item["root_post_id"]
        old = self.c.db.thread(old_root)
        if old and old["channel_id"] == ch:
            return False
        post = await self.c.mm.post(ch, f"📌 {heading}\n_From #ideas: {self.c.permalink(old_root)}_")
        new_root = post["id"]
        self.c.db.upsert_thread(new_root, ch)
        self.c.db.set_thread_repo(new_root, repo, False)
        self.c.db.move_item(item_id, new_root)
        await self.c.mm.post(old["channel_id"] if old else self.c.ideas_channel_id,
                             f"➡️ Continues in ~{defang(repo)}: {self.c.permalink(new_root)}", root_id=old_root)  # fmt: skip
        return True
