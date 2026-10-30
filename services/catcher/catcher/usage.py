"""Claude usage meter + owner budget gate (DECISIONS D60-D66).

Honest scope: the Max subscription does not expose its remaining quota through any API. This is a
SELF-IMPOSED proxy: every headless Claude run reports `total_cost_usd` (Claude Code's API-equivalent
cost, not money charged to the subscription), tokens, turns and duration; we sum them per day/week
against owner budgets. On top, real "usage limit" errors are detected and pause everything until reset.

Sources (verified 2026-10-30):
- catcher runs: `claude -p --output-format json` / stream-json result object: total_cost_usd, usage.{input,
  output,cache_read_input,cache_creation_input}_tokens, duration_ms, num_turns, is_error, api_error_status.
- GitHub Actions runs (claude-code-action v1.0.251): the last element of `execution_file` (same fields),
  posted by the workflows as a hidden `<!-- homie-usage {...} -->` comment that the catcher polls (no inbound path).

States: ok | paused (manual pause, or a budget reached and no override) | limit (real usage limit until reset).
Published for the workflows in the `budget-status` issue of kiukairor-org/homie-catcher (the bot App cannot
write Actions variables: no `variables` permission).
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .github import STATUS_REPO
from .routing import selectable_repos

log = logging.getLogger(__name__)

DEFAULT_DAILY_USD = 25.0
DEFAULT_WEEKLY_USD = 100.0
WARN_FRACTION = 0.8
NONESSENTIAL = frozenset({"spec", "brainstorm", "channel_chat", "promote"})
STATUS_LABEL = "budget-status"
PUBLISH_HEARTBEAT_S = 600
LIMIT_FALLBACK_S = 3600
GHA_FEATURES = {"agent": "agent_run", "review": "review"}
BOT_LOGIN = "kiu-homie-bot[bot]"
_USAGE_MARK = re.compile(r"<!-- homie-usage (\{.*?\}) -->", re.S)
_SKIP_MARK = "<!-- homie-budget-skipped -->"
_LIMIT_TEXT = re.compile(r"usage limit|hit your (?:usage )?limit|limit reached|rate.?limit", re.I)
_LIMIT_EPOCH = re.compile(r"limit reached\|(\d{10})")
_RESETS_AT = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.I)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS claude_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL, day TEXT NOT NULL, week TEXT NOT NULL,
  feature TEXT NOT NULL, source TEXT NOT NULL, repo TEXT, ref TEXT UNIQUE,
  cost_usd REAL NOT NULL DEFAULT 0, input_tokens INTEGER, output_tokens INTEGER,
  cache_read INTEGER, cache_creation INTEGER, duration_ms INTEGER, num_turns INTEGER,
  ok INTEGER NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS budget_kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage_events(key TEXT PRIMARY KEY, ts REAL NOT NULL, data TEXT);
"""


def usd(v: float) -> str:
    """$12.34, or 4 decimals under $1 (small test budgets stay readable)."""
    return f"${v:.2f}" if abs(v) >= 1 or v == 0 else f"${v:.4f}"


def dur(ms: float) -> str:
    return f"{ms / 1000:.0f} s" if ms < 120_000 else f"{ms / 60000:.1f} min"


def _periods(ts: float) -> tuple[str, str]:
    d = datetime.fromtimestamp(ts, timezone.utc)
    iso = d.isocalendar()
    return d.strftime("%Y-%m-%d"), f"{iso[0]}-W{iso[1]:02d}"


def detect_limit(result: dict | None, text: str | None, now: float) -> float | None:
    """Reset time (epoch) if this run hit a real usage limit, else None."""
    blob = " ".join(str(x) for x in ((result or {}).get("result"), (result or {}).get("terminal_reason"), text) if x)
    status = (result or {}).get("api_error_status")
    hit = status == 429 or (bool(_LIMIT_TEXT.search(blob)) and ((result or {}).get("is_error") or text))
    if not hit:
        return None
    m = _LIMIT_EPOCH.search(blob)
    if m:
        return float(m.group(1))
    m = _RESETS_AT.search(blob)
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        hour = hour % 12 + (12 if ampm == "pm" else 0) if ampm else hour
        if 0 <= hour < 24 and 0 <= minute < 60:
            base = datetime.fromtimestamp(now, timezone.utc)
            cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if cand.timestamp() <= now:
                cand += timedelta(days=1)
            return cand.timestamp()  # times without a zone are read as UTC (documented)
    return now + LIMIT_FALLBACK_S


class Usage:
    def __init__(self, catcher: Any) -> None:
        self.c = catcher
        catcher.db._conn.executescript(SCHEMA_SQL)

    # ------------------------------------------------------------- storage
    def _x(self, sql: str, args: tuple = ()):  # noqa: ANN202
        return self.c.db._x(sql, args)

    def get(self, key: str, default: Any = None) -> Any:
        row = self._x("SELECT value FROM budget_kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        self._x("INSERT OR REPLACE INTO budget_kv(key, value) VALUES(?,?)", (key, json.dumps(value)))

    def _once(self, key: str, data: Any = None) -> bool:
        cur = self._x("INSERT OR IGNORE INTO usage_events(key, ts, data) VALUES(?,?,?)", (key, self.c.clock(), json.dumps(data)))
        return cur.rowcount == 1

    @property
    def daily(self) -> float:
        return float(self.get("daily_usd", DEFAULT_DAILY_USD))

    @property
    def weekly(self) -> float:
        return float(self.get("weekly_usd", DEFAULT_WEEKLY_USD))

    # ------------------------------------------------------------- meter
    def record(self, feature: str, result: dict | None, *, ok: bool, source: str = "catcher", repo: str | None = None,
               ref: str | None = None, error: str | None = None) -> None:  # fmt: skip
        now = self.c.clock()
        r = result or {}
        u = r.get("usage") or {}
        day, week = _periods(now)
        self._x(
            "INSERT OR IGNORE INTO claude_usage(ts, day, week, feature, source, repo, ref, cost_usd, input_tokens, output_tokens,"
            " cache_read, cache_creation, duration_ms, num_turns, ok, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, day, week, feature, source, repo, ref, float(r.get("total_cost_usd") or 0), u.get("input_tokens"),
             u.get("output_tokens"), u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"),
             r.get("duration_ms"), r.get("num_turns"), 1 if ok else 0, (error or "")[:300] or None),
        )  # fmt: skip
        reset = detect_limit(result, error, now)
        if reset:
            self.set_limit(reset, f"{feature} run reported a usage limit")
        self.c.spawn(self.evaluate())

    def spent(self, period: str) -> float:
        day, week = _periods(self.c.clock())
        col, val = ("day", day) if period == "day" else ("week", week)
        return float(self._x(f"SELECT COALESCE(SUM(cost_usd),0) AS s FROM claude_usage WHERE {col}=?", (val,)).fetchone()["s"])  # noqa: S608

    def breakdown(self, period: str) -> list[tuple[str, int, float, int, int, int]]:
        """(feature, runs, cost, input+output tokens, cache tokens, duration ms) for the current day/week."""
        day, week = _periods(self.c.clock())
        col, val = ("day", day) if period == "day" else ("week", week)
        sql = (
            "SELECT feature, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS c,"  # noqa: S608 - col is a fixed literal
            " COALESCE(SUM(COALESCE(input_tokens,0)+COALESCE(output_tokens,0)),0) AS t,"
            " COALESCE(SUM(COALESCE(cache_read,0)+COALESCE(cache_creation,0)),0) AS k,"
            f" COALESCE(SUM(duration_ms),0) AS ms FROM claude_usage WHERE {col}=? GROUP BY feature ORDER BY c DESC"
        )
        rows = self._x(sql, (val,)).fetchall()
        return [(r["feature"], r["n"], r["c"], r["t"], r["k"], r["ms"]) for r in rows]

    # ------------------------------------------------------------- state
    def set_limit(self, until: float, reason: str) -> None:
        if until > float(self.get("limit_until", 0)):
            self.put("limit_until", until)
            self.put("limit_reason", reason)
            self.c.spawn(self._notify_limit(until, reason))

    def state(self) -> tuple[str, str]:
        now = self.c.clock()
        if float(self.get("limit_until", 0)) > now:
            until = datetime.fromtimestamp(float(self.get("limit_until")), timezone.utc).strftime("%H:%M UTC")
            return "limit", f"real usage limit reached; paused until {until}"
        if self.get("paused", False):
            return "paused", "paused by the owner (`budget pause`)"
        if float(self.get("override_until", 0)) > now:
            return "ok", "budget override active"
        d, w = self.spent("day"), self.spent("week")
        if d >= self.daily:
            return "paused", f"daily budget reached ({usd(d)} of {usd(self.daily)})"
        if w >= self.weekly:
            return "paused", f"weekly budget reached ({usd(w)} of {usd(self.weekly)})"
        return "ok", "within budget"

    def allow(self, feature: str) -> bool:
        return feature not in NONESSENTIAL or self.state()[0] == "ok"

    def paused_text(self) -> str:
        st, why = self.state()
        return (f"⏸️ Claude work is **{st}**: {why}. In-flight work finishes and the reviewer keeps running; "
                "queued items wait. Owner: `usage`, `budget resume`, `budget override 1h`, `budget daily <n>`.")  # fmt: skip

    async def evaluate(self) -> None:
        """80% warnings, 100% pause notice, publish the state for the workflows."""
        day, week = _periods(self.c.clock())
        for period, spent, budget, key in (
            ("daily", self.spent("day"), self.daily, day),
            ("weekly", self.spent("week"), self.weekly, week),
        ):
            if budget <= 0:
                continue
            frac = spent / budget
            if WARN_FRACTION <= frac < 1 and self._once(f"warn:{period}:{key}:{budget}"):
                await self._log(f"⚠️ Claude usage at **{frac:.0%}** of the {period} budget ({usd(spent)} of {usd(budget)}, "
                                "API-equivalent cost; Max quota itself is not visible). Owner: `usage`.")  # fmt: skip
            if frac >= 1 and self._once(f"pause:{period}:{key}:{budget}"):
                await self._log(f"⏸️ {self.c.cfg.owner_mention} {period} Claude budget reached ({usd(spent)} of {usd(budget)}): "
                                "brainstorm/chat replies, new specs and agent PR runs are paused. In-flight work finishes; "
                                "the reviewer keeps running. `budget override 1h`, `budget daily <n>` or `budget weekly <n>`.")  # fmt: skip
        await self.publish()

    async def _notify_limit(self, until: float, reason: str) -> None:
        when = datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        await self._log(f"🛑 {self.c.cfg.owner_mention} **Claude usage limit reached** ({reason}). Everything that needs Claude "
                        f"is paused until **{when}** (reviewer included). `budget resume` lifts it early.")  # fmt: skip
        await self.publish(force=True)

    async def _log(self, text: str) -> None:
        if self.c.agent_log_channel_id:
            await self.c.mm.post(self.c.agent_log_channel_id, text)

    # ------------------------------------------------------------- publish for workflows
    def status_body(self) -> str:
        st, why = self.state()
        data = {"state": st, "updated": int(self.c.clock()), "until": int(float(self.get("limit_until", 0))) or None}
        return (
            "Managed by homie-catcher (DECISIONS D62). Workflows read the marker below: `ok` runs Claude, `paused` skips "
            "agent runs (reviewer still runs), `limit` skips both. Do not edit by hand; use the owner commands in Mattermost.\n\n"
            f"**State:** `{st}` ({why})\n\n"
            f"<!-- homie-budget {json.dumps(data, separators=(',', ':'))} -->\n"
        )

    async def publish(self, force: bool = False) -> None:
        st, _ = self.state()
        now = self.c.clock()
        if not force and st == self.get("published_state") and now - float(self.get("published_at", 0)) < PUBLISH_HEARTBEAT_S:
            return
        try:
            num = self.get("status_issue")
            if not num:
                found = await self.c.gh.find_issue(STATUS_REPO, STATUS_LABEL)
                if found:
                    num = found["number"]
                else:
                    await self.c.gh.ensure_label(STATUS_REPO, STATUS_LABEL, "0e8a16", "Claude budget state for workflows")
                    created = await self.c.gh.create_issue(STATUS_REPO, "Claude budget status (managed by homie-catcher)",
                                                           self.status_body(), [STATUS_LABEL])  # fmt: skip
                    num = created["number"]
                self.put("status_issue", num)
            await self.c.gh.set_issue_body(STATUS_REPO, int(num), self.status_body())
            self.put("published_state", st)
            self.put("published_at", now)
        except Exception:
            log.exception("publishing budget status failed")

    # ------------------------------------------------------------- GitHub Actions usage
    async def ingest_gha(self) -> int:
        repos = sorted(set(selectable_repos(await self.c.org_repos())) | {"app-template"})
        n = 0
        for repo in repos:
            since = self.get(f"cursor:{repo}") or datetime.fromtimestamp(self.c.clock() - 86400, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            try:
                comments = await self.c.gh.repo_comments_since(repo, since)
            except Exception as exc:  # noqa: BLE001
                log.warning("usage scan %s failed: %s", repo, exc)
                continue
            newest = since
            for cm in comments:
                newest = max(newest, cm.get("updated_at") or since)
                if (cm.get("user") or {}).get("login") != BOT_LOGIN:
                    continue
                body = cm.get("body") or ""
                num = int(str(cm.get("issue_url", "")).rsplit("/", 1)[-1] or 0)
                m = _USAGE_MARK.search(body)
                if m:
                    try:
                        d = json.loads(m.group(1))
                    except ValueError:
                        continue
                    result = {"total_cost_usd": d.get("cost"), "duration_ms": d.get("ms"), "num_turns": d.get("turns"),
                              "is_error": d.get("err"), "api_error_status": d.get("api"), "result": d.get("text"),
                              "usage": {"input_tokens": d.get("in"), "output_tokens": d.get("out"),
                                        "cache_read_input_tokens": d.get("cr"), "cache_creation_input_tokens": d.get("cc")}}  # fmt: skip
                    self.record(GHA_FEATURES.get(str(d.get("wf")), "gha"), result, ok=not d.get("err"), source="gha",
                                repo=repo, ref=f"gh:{repo}:{cm.get('id')}")  # fmt: skip
                    n += 1
                elif _SKIP_MARK in body and num:
                    self._once(f"skip:{repo}:{num}:{cm.get('id')}", {"repo": repo, "issue": num, "retried": False})
            self.put(f"cursor:{repo}", newest)
        return n

    async def retry_skipped(self) -> int:
        """When usage is ok again, re-trigger agent runs that the gate skipped (open issues only, once each)."""
        if self.state()[0] != "ok":
            return 0
        rows = self._x("SELECT key, data FROM usage_events WHERE key LIKE 'skip:%'").fetchall()
        n = 0
        for row in rows:
            d = json.loads(row["data"] or "{}")
            if d.get("retried"):
                continue
            d["retried"] = True
            self._x("UPDATE usage_events SET data=? WHERE key=?", (json.dumps(d), row["key"]))
            try:
                open_nums = {i["number"] for i in await self.c.gh.open_issues(d["repo"]) if "pull_request" not in i}
                if d["issue"] in open_nums:
                    await self.c.gh.comment(
                        d["repo"], d["issue"], "@claude the owner's Claude usage gate is open again: please continue with this issue."
                    )
                    n += 1
            except Exception:
                log.exception("retry of skipped run %s failed", row["key"])
        return n

    async def tick(self) -> None:
        await self.ingest_gha()
        await self.evaluate()
        await self.retry_skipped()

    # ------------------------------------------------------------- owner commands
    _CMD = re.compile(
        r"^\s*(usage|budget\s+(daily|weekly)\s+(\d+(?:\.\d+)?)|budget\s+(pause|resume)|budget\s+override\s+(\d{1,2})\s*h)\s*$", re.I
    )

    def is_command(self, text: str) -> bool:
        return bool(self._CMD.match(text or ""))

    async def command(self, text: str) -> str:
        m = self._CMD.match(text or "")
        if not m:
            return ""
        if m.group(1).lower() == "usage":
            return self.report()
        if m.group(2):
            val = float(m.group(3))
            if not 0 < val <= 10000:
                return "Budget must be between 0 and 10000 (USD-equivalent)."
            self.put(f"{m.group(2).lower()}_usd", val)
            await self.evaluate()
            return f"✅ {m.group(2).lower()} budget set to {usd(val)}. Now: {self.state()[0]} ({self.state()[1]})."
        if m.group(4):
            if m.group(4).lower() == "pause":
                self.put("paused", True)
                await self.publish(force=True)
                return "⏸️ Claude work paused by the owner. `budget resume` to continue."
            self.put("paused", False)
            self.put("limit_until", 0)
            await self.publish(force=True)
            retried = await self.retry_skipped()
            st, why = self.state()
            extra = f" Re-triggered {retried} skipped agent run(s)." if retried else ""
            return f"▶️ Resumed. Now: **{st}** ({why}).{extra}"
        hours = int(m.group(5))
        if not 1 <= hours <= 24:
            return "Override must be 1-24 hours."
        self.put("override_until", self.c.clock() + hours * 3600)
        await self.publish(force=True)
        return f"⏩ Budget override for {hours}h (a real usage limit still pauses). Now: {self.state()[0]}."

    def report(self) -> str:
        st, why = self.state()
        lines = [f"**Claude usage** (API-equivalent cost from Claude Code; the Max quota itself is not visible). State: **{st}** ({why})."]
        for period, label, budget in (("day", "Today (UTC)", self.daily), ("week", "This week", self.weekly)):
            spent = self.spent(period)
            pct = f"{spent / budget:.0%}" if budget > 0 else "-"
            lines += [
                "",
                f"**{label}:** {usd(spent)} of {usd(budget)} ({pct})",
                "| feature | runs | ≈USD | tokens in+out | cache tokens | time |",
                "|---|---:|---:|---:|---:|---:|",
            ]
            rows = self.breakdown(period) or [("(none)", 0, 0.0, 0, 0, 0)]
            lines += [f"| {f} | {n} | {usd(c)} | {t:,} | {k:,} | {dur(ms)} |" for f, n, c, t, k, ms in rows]
        return "\n".join(lines)


def now_utc() -> float:
    return time.time()
