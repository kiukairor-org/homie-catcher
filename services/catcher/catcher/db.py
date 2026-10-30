"""SQLite state (file at DB_PATH on a PVC).

threads        Mattermost thread root post -> remembered repo
items          one idea: Plane project/item, GitHub issue, status (the state machine)
clarifications questions asked in a thread and the owner's answers
run_queue      Claude runs (queued/running/done) - also the rate-limit ledger
plane_projects repo -> Plane project + state/label ids
pull_requests  PRs found for tracked issues
milestones     milestone keys already announced (each one exactly once)
seen_posts     websocket de-duplication
nr_issues      New Relic issue ids already turned into triage items
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any

from .plane import Project

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads(
  root_post_id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL,
  repo TEXT,
  is_new_repo INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  root_post_id TEXT NOT NULL REFERENCES threads(root_post_id),
  status TEXT NOT NULL,
  idea TEXT NOT NULL,
  spec_json TEXT,
  repo TEXT,
  new_repo_name TEXT,
  new_repo_desc TEXT,
  plane_project_id TEXT,
  plane_item_id TEXT,
  plane_seq INTEGER,
  gh_issue_number INTEGER,
  gh_issue_url TEXT,
  error TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS items_root ON items(root_post_id);
CREATE TABLE IF NOT EXISTS clarifications(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  question TEXT NOT NULL,
  answer TEXT,
  asked_at REAL NOT NULL,
  answered_at REAL
);
CREATE TABLE IF NOT EXISTS run_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id INTEGER NOT NULL REFERENCES items(id),
  status TEXT NOT NULL DEFAULT 'queued',
  enqueued_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);
CREATE TABLE IF NOT EXISTS plane_projects(
  repo TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  identifier TEXT NOT NULL,
  states_json TEXT NOT NULL,
  labels_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pull_requests(
  repo TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  item_id INTEGER NOT NULL REFERENCES items(id),
  head_sha TEXT,
  state TEXT,
  PRIMARY KEY(repo, pr_number)
);
CREATE TABLE IF NOT EXISTS milestones(
  item_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  announced_at REAL NOT NULL,
  PRIMARY KEY(item_id, key)
);
CREATE TABLE IF NOT EXISTS nr_issues(
  issue_id TEXT PRIMARY KEY,
  item_id INTEGER,
  received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_posts(
  post_id TEXT PRIMARY KEY,
  seen_at REAL NOT NULL
);
"""

ITEM_FIELDS = {
    "status",
    "idea",
    "spec_json",
    "repo",
    "new_repo_name",
    "new_repo_desc",
    "plane_project_id",
    "plane_item_id",
    "plane_seq",
    "gh_issue_number",
    "gh_issue_url",
    "error",
}


class DB:
    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.clock = clock

    def _x(self, sql: str, args: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(args))

    # ---- de-dup ------------------------------------------------------------
    def mark_seen(self, post_id: str) -> bool:
        """True the first time a post id is seen."""
        cur = self._x("INSERT OR IGNORE INTO seen_posts(post_id, seen_at) VALUES(?,?)", (post_id, self.clock()))
        return cur.rowcount == 1

    # ---- New Relic alerts ----------------------------------------------------
    def claim_nr_issue(self, issue_id: str) -> bool:
        """True the first time a New Relic issue id is seen."""
        cur = self._x("INSERT OR IGNORE INTO nr_issues(issue_id, received_at) VALUES(?,?)", (issue_id, self.clock()))
        return cur.rowcount == 1

    def release_nr_issue(self, issue_id: str) -> None:
        """Forget an issue whose processing failed before anything was created, so a later run retries it."""
        self._x("DELETE FROM nr_issues WHERE issue_id=? AND item_id IS NULL", (issue_id,))

    def link_nr_issue(self, issue_id: str, item_id: int) -> None:
        self._x("UPDATE nr_issues SET item_id=? WHERE issue_id=?", (item_id, issue_id))

    # ---- threads -----------------------------------------------------------
    def upsert_thread(self, root: str, channel_id: str) -> None:
        self._x(
            "INSERT OR IGNORE INTO threads(root_post_id, channel_id, created_at) VALUES(?,?,?)",
            (root, channel_id, self.clock()),
        )

    def thread(self, root: str) -> sqlite3.Row | None:
        return self._x("SELECT * FROM threads WHERE root_post_id=?", (root,)).fetchone()

    def set_thread_repo(self, root: str, repo: str | None, is_new: bool) -> None:
        self._x("UPDATE threads SET repo=?, is_new_repo=? WHERE root_post_id=?", (repo, int(is_new), root))

    # ---- items -------------------------------------------------------------
    def create_item(self, root: str, idea: str, status: str) -> int:
        now = self.clock()
        cur = self._x(
            "INSERT INTO items(root_post_id, status, idea, created_at, updated_at) VALUES(?,?,?,?,?)",
            (root, status, idea, now, now),
        )
        return int(cur.lastrowid)

    def item(self, item_id: int) -> sqlite3.Row | None:
        return self._x("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()

    def latest_item(self, root: str) -> sqlite3.Row | None:
        return self._x("SELECT * FROM items WHERE root_post_id=? ORDER BY id DESC LIMIT 1", (root,)).fetchone()

    def update_item(self, item_id: int, **fields: Any) -> None:
        bad = set(fields) - ITEM_FIELDS
        if bad:
            raise ValueError(f"unknown item fields {bad}")
        cols = ", ".join(f"{k}=?" for k in fields)
        # column names come from the ITEM_FIELDS allowlist above; values are bound parameters
        self._x(f"UPDATE items SET {cols}, updated_at=? WHERE id=?", (*fields.values(), self.clock(), item_id))  # noqa: S608

    def transition(self, item_id: int, from_statuses: Iterable[str], to_status: str) -> bool:
        """Atomic compare-and-set on status: the basis of button idempotency."""
        froms = list(from_statuses)
        marks = ",".join("?" * len(froms))
        cur = self._x(
            f"UPDATE items SET status=?, updated_at=? WHERE id=? AND status IN ({marks})",  # noqa: S608 - only "?" marks
            (to_status, self.clock(), item_id, *froms),
        )
        return cur.rowcount == 1

    def move_item(self, item_id: int, new_root: str) -> None:
        """Re-point an item to another thread (its progress continues there)."""
        self._x("UPDATE items SET root_post_id=?, updated_at=? WHERE id=?", (new_root, self.clock(), item_id))

    def forget_plane_project(self, repo: str) -> None:
        self._x("DELETE FROM plane_projects WHERE repo=?", (repo,))

    def tracked_items(self) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM items WHERE status='approved' AND gh_issue_number IS NOT NULL AND repo IS NOT NULL").fetchall()

    # ---- clarifications ----------------------------------------------------
    def add_clarification(self, item_id: int, question: str) -> None:
        self._x("INSERT INTO clarifications(item_id, question, asked_at) VALUES(?,?,?)", (item_id, question, self.clock()))

    def answer_clarification(self, item_id: int, answer: str) -> bool:
        row = self._x("SELECT id FROM clarifications WHERE item_id=? AND answer IS NULL ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
        if not row:
            return False
        self._x("UPDATE clarifications SET answer=?, answered_at=? WHERE id=?", (answer, self.clock(), row["id"]))
        return True

    def clarifications(self, item_id: int) -> list[sqlite3.Row]:
        return self._x("SELECT * FROM clarifications WHERE item_id=? ORDER BY id", (item_id,)).fetchall()

    # ---- run queue ---------------------------------------------------------
    def enqueue(self, item_id: int) -> None:
        self._x("INSERT INTO run_queue(item_id, enqueued_at) VALUES(?,?)", (item_id, self.clock()))

    def next_queued(self) -> sqlite3.Row | None:
        return self._x("SELECT * FROM run_queue WHERE status='queued' ORDER BY id LIMIT 1").fetchone()

    def queued_count(self) -> int:
        return self._x("SELECT COUNT(*) FROM run_queue WHERE status='queued'").fetchone()[0]

    def record_run(self, item_id: int) -> None:
        now = self.clock()
        self._x(
            "INSERT INTO run_queue(item_id, status, enqueued_at, started_at, finished_at) VALUES(?,?,?,?,?)",
            (item_id, "done", now, now, now),
        )

    def start_run(self, run_id: int) -> None:
        self._x("UPDATE run_queue SET status='running', started_at=? WHERE id=?", (self.clock(), run_id))

    def finish_run(self, run_id: int) -> None:
        self._x("UPDATE run_queue SET status='done', finished_at=? WHERE id=?", (self.clock(), run_id))

    def runs_since(self, since: float) -> int:
        return self._x("SELECT COUNT(*) FROM run_queue WHERE started_at IS NOT NULL AND started_at>=?", (since,)).fetchone()[0]

    def oldest_run_start_since(self, since: float) -> float | None:
        row = self._x("SELECT MIN(started_at) FROM run_queue WHERE started_at>=?", (since,)).fetchone()
        return row[0]

    def requeue_interrupted(self) -> int:
        cur = self._x("UPDATE run_queue SET status='queued' WHERE status='running'")
        self._x("UPDATE items SET status='queued' WHERE status='running'")
        return cur.rowcount

    # ---- plane projects ----------------------------------------------------
    def plane_project(self, repo: str) -> Project | None:
        row = self._x("SELECT * FROM plane_projects WHERE repo=?", (repo,)).fetchone()
        if not row:
            return None
        return Project(repo, row["project_id"], row["identifier"], json.loads(row["states_json"]), json.loads(row["labels_json"]))

    def save_plane_project(self, p: Project) -> None:
        self._x(
            "INSERT OR REPLACE INTO plane_projects(repo, project_id, identifier, states_json, labels_json) VALUES(?,?,?,?,?)",
            (p.repo, p.project_id, p.identifier, json.dumps(p.states), json.dumps(p.labels)),
        )

    # ---- PRs and milestones --------------------------------------------------
    def upsert_pr(self, repo: str, number: int, item_id: int, head_sha: str | None, state: str) -> None:
        self._x(
            "INSERT INTO pull_requests(repo, pr_number, item_id, head_sha, state) VALUES(?,?,?,?,?) "
            "ON CONFLICT(repo, pr_number) DO UPDATE SET head_sha=excluded.head_sha, state=excluded.state",
            (repo, number, item_id, head_sha, state),
        )

    def claim_milestone(self, item_id: int, key: str) -> bool:
        """True exactly once per (item, key)."""
        cur = self._x("INSERT OR IGNORE INTO milestones(item_id, key, announced_at) VALUES(?,?,?)", (item_id, key, self.clock()))
        return cur.rowcount == 1

    def has_milestone(self, item_id: int, key: str) -> bool:
        return self._x("SELECT 1 FROM milestones WHERE item_id=? AND key=?", (item_id, key)).fetchone() is not None
