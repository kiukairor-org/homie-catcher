"""Pipeline telemetry (DECISIONS D80): the AI CI/CD pipeline as New Relic traces, events and deploy markers.

Same path as the apps (D40): OTLP/HTTP JSON built with the stdlib, sent through the shared ``Transport`` to the
in-cluster collector, which alone holds the licence key. No OpenTelemetry SDK: nothing here can be auto-instrumented,
because the pipeline's stages are business events (a Mattermost post, an owner's Approve, a PR milestone, an Argo CD
sync), not HTTP requests or SQL calls that an agent could wrap.

What is emitted (every record carries ``ai-pipeline = true``):
- One TRACE per pipeline item (trace id derived from the item id, so independent emitters join the same trace):
  root span ``pipeline <repo>`` (item created -> terminal), ``stage <status>`` spans (how long the item sat in each
  state, e.g. waiting for the owner's Approve), phase spans from the milestones (agent build, review+merge, deploy),
  ``claude <feature>`` spans (gen_ai.* attributes), GitHub Actions workflow runs with their jobs and steps
  (service ``github-actions``) and the Argo CD sync (service ``argocd``).
- EVENTS (OTLP log records with ``newrelic.event.type``, which New Relic stores as custom events):
  CicdPipelineRun, CicdStage, CicdWorkflowRun, CicdDeployment, LlmChatCompletionSummary.
- CHANGE TRACKING markers (NerdGraph, the read-only verify key file of D74 also has change-tracking rights) for each
  Argo CD sync of an app that has a New Relic entity.

Never raises into the pipeline: every public entry point swallows and logs. Off when OTEL_EXPORTER_OTLP_ENDPOINT is
unset. Buffer is bounded (oldest dropped).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any

from .routing import selectable_repos

log = logging.getLogger(__name__)

TAG = "ai-pipeline"
MAX_BUFFER = 5000
RUN_SETTLE_S = 480  # wait for the usage comment (polled every 5 min) so a Claude run maps to its item first
# Measured on account 8680047 (2026-11-05): back-dated OTLP spans are kept up to ~20 h, custom events (OTLP logs) up to ~47 h.
BACKFILL_S = 46 * 3600  # first start: export runs/deploys/items of the last 46 h (events); older spans are not sent
SPAN_MAX_AGE_S = 20 * 3600
TERMINAL = frozenset({"done", "rejected", "error"})
EXTRA_RUN_REPOS = ("app-template", "homie-catcher")
PLATFORM_ARGO_APPS = {"catcher": "homie-catcher", "observability": None, "ai": None, "sre": None}
ARGO_NS = "argocd"
NERDGRAPH = {"US": "https://api.newrelic.com/graphql", "EU": "https://api.eu.newrelic.com/graphql"}

# The pipeline item the current asyncio task works on (set by Catcher.process_item), so a Claude run joins its trace.
current_item: contextvars.ContextVar[int | None] = contextvars.ContextVar("current_item", default=None)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS otel_sent(key TEXT PRIMARY KEY, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS otel_map(kind TEXT NOT NULL, key TEXT NOT NULL, item_id INTEGER NOT NULL, PRIMARY KEY(kind, key));
CREATE TABLE IF NOT EXISTS otel_trans(item_id INTEGER NOT NULL, status TEXT NOT NULL, ts REAL NOT NULL);
CREATE TABLE IF NOT EXISTS otel_llm(item_id INTEGER NOT NULL, cost REAL, tin INTEGER, tout INTEGER, model TEXT, ts REAL NOT NULL);
"""


def claude_otel_env(endpoint: str | None = None) -> dict[str, str]:
    """D80: Claude Code's OWN OpenTelemetry for a headless `claude -p` child (env only, zero code in Claude Code):
    claude_code.api_request events (model, tokens, cost, duration per API call), metrics and, in beta, spans.
    `-p` sessions read TRACEPARENT from their environment, so when the run belongs to a pipeline item its spans
    become children of the item's root span (documented: code.claude.com/docs/en/monitoring-usage).
    Prompts, tool details and raw API bodies stay OFF (Claude Code defaults)."""
    ep = (endpoint if endpoint is not None else os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")).strip()
    if not ep:
        return {}
    env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": ep,
        "OTEL_METRICS_INCLUDE_ACCOUNT_UUID": "false",
        "OTEL_RESOURCE_ATTRIBUTES": f"service.name=claude-code,service.namespace=homie-pipeline,{TAG}=true,k8s.cluster.name=oracle-k3s",
    }
    item_id = current_item.get()
    if item_id is not None:
        env["TRACEPARENT"] = f"00-{item_trace_id(item_id)}-{item_root_span_id(item_id)}-01"
    return env


def _h(*parts: Any) -> str:
    return hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


def item_trace_id(item_id: int) -> str:
    return _h("homie-pipeline-item", item_id)[:32]


def item_root_span_id(item_id: int) -> str:
    return _h("homie-pipeline-root", item_id)[:16]


def run_span_id(run_id: Any, attempt: Any = 1) -> str:
    return _h("gha-run", run_id, attempt)[:16]


def _new_span_id() -> str:
    return os.urandom(8).hex()


def iso_ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def otlp_value(v: Any) -> dict:
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def otlp_attrs(d: dict[str, Any]) -> list[dict]:
    return [{"key": k, "value": otlp_value(v)} for k, v in d.items() if v is not None and v != ""]


def _ns(ts: float) -> str:
    return str(int(ts * 1e9))


def gha_result(conclusion: str | None) -> str:
    """GitHub conclusion -> OTel cicd.pipeline.result."""
    return {
        "success": "success",
        "failure": "failure",
        "cancelled": "cancellation",
        "skipped": "skip",
        "timed_out": "timeout",
        "neutral": "success",
        "action_required": "failure",
        "startup_failure": "error",
        "stale": "skip",
    }.get(conclusion or "", "error")


class Telemetry:
    def __init__(
        self,
        catcher: Any,
        transport: Any,
        *,
        endpoint: str | None,
        nr_key_file: str = "",
        nr_account_id: str = "",
        nr_region: str = "EU",
        k8s: Any = None,
        cluster: str = "oracle-k3s",
    ) -> None:
        self.c = catcher
        self.transport = transport
        self.endpoint = (endpoint or "").rstrip("/")
        self.enabled = bool(self.endpoint)
        self.nr_key_file, self.nr_account_id, self.nr_region = nr_key_file, nr_account_id, nr_region
        self.k8s = k8s
        self.cluster = cluster
        self._spans: dict[str, list[dict]] = {}
        self._logs: dict[str, list[dict]] = {}
        self._count = 0
        catcher.db._conn.executescript(SCHEMA_SQL)

    # ------------------------------------------------------------------ storage
    def _x(self, sql: str, args: tuple = ()):  # noqa: ANN202
        return self.c.db._x(sql, args)

    def _once(self, key: str) -> bool:
        return self._x("INSERT OR IGNORE INTO otel_sent(key, ts) VALUES(?,?)", (key, self.c.clock())).rowcount == 1

    def _sent(self, key: str) -> bool:
        return self._x("SELECT 1 FROM otel_sent WHERE key=?", (key,)).fetchone() is not None

    def map(self, kind: str, key: Any, item_id: int) -> None:
        """Remember that a commit SHA / workflow run / merge commit belongs to a pipeline item (trace stitching)."""
        try:
            self._x("INSERT OR IGNORE INTO otel_map(kind, key, item_id) VALUES(?,?,?)", (kind, str(key), int(item_id)))
        except Exception:
            log.exception("telemetry map failed")

    def lookup(self, kind: str, key: Any) -> int | None:
        row = self._x("SELECT item_id FROM otel_map WHERE kind=? AND key=?", (kind, str(key))).fetchone()
        return int(row["item_id"]) if row else None

    def item_for_number(self, repo: str, number: int) -> int | None:
        """Issue number or PR number -> pipeline item."""
        row = self._x("SELECT id FROM items WHERE repo=? AND gh_issue_number=? ORDER BY id DESC LIMIT 1", (repo, number)).fetchone()
        if row:
            return int(row["id"])
        row = self._x("SELECT item_id FROM pull_requests WHERE repo=? AND pr_number=?", (repo, number)).fetchone()
        return int(row["item_id"]) if row else None

    # ------------------------------------------------------------------ low-level emit
    def _resource(self, service: str) -> list[dict]:
        return otlp_attrs(
            {
                "service.name": service,
                "service.namespace": "homie-pipeline",
                TAG: "true",
                "k8s.cluster.name": self.cluster,
                "telemetry.sdk.name": "homie-stdlib-otlp",
            }
        )

    def _room(self) -> bool:
        if self._count >= MAX_BUFFER:
            log.warning("telemetry buffer full; dropping")
            return False
        self._count += 1
        return True

    def span(
        self,
        service: str,
        name: str,
        start: float,
        end: float,
        *,
        trace_id: str,
        span_id: str | None = None,
        parent: str | None = None,
        attrs: dict | None = None,
        ok: bool = True,
        kind: int = 1,
        events: list[tuple[float, str, dict]] | None = None,
    ) -> str:
        sid = span_id or _new_span_id()
        if not self.enabled or self.c.clock() - end > SPAN_MAX_AGE_S or not self._room():
            return sid
        a = {TAG: True, **(attrs or {})}
        s = {
            "traceId": trace_id,
            "spanId": sid,
            "name": name[:200],
            "kind": kind,
            "startTimeUnixNano": _ns(start),
            "endTimeUnixNano": _ns(max(end, start)),
            "attributes": otlp_attrs(a),
            "status": {"code": 1 if ok else 2},
        }
        if parent:
            s["parentSpanId"] = parent
        if events:
            s["events"] = [{"timeUnixNano": _ns(t), "name": n, "attributes": otlp_attrs(ea)} for t, n, ea in events]
        self._spans.setdefault(service, []).append(s)
        return sid

    def event(self, service: str, event_type: str, attrs: dict, ts: float | None = None) -> None:
        """A New Relic custom event, sent as an OTLP log record with `newrelic.event.type` (verified on 8680047)."""
        if not self.enabled or not self._room():
            return
        rec = {
            "timeUnixNano": _ns(ts if ts is not None else self.c.clock()),
            "body": {"stringValue": event_type},
            "attributes": otlp_attrs({"newrelic.event.type": event_type, TAG: True, **attrs}),
        }
        self._logs.setdefault(service, []).append(rec)

    async def flush(self) -> None:
        if not self.enabled:
            return
        spans, logs = self._spans, self._logs
        self._spans, self._logs, self._count = {}, {}, 0
        scope = {"name": "homie-catcher.telemetry"}
        if spans:
            body = {
                "resourceSpans": [
                    {"resource": {"attributes": self._resource(svc)}, "scopeSpans": [{"scope": scope, "spans": ss}]}
                    for svc, ss in spans.items()
                ]
            }
            await self._post("/v1/traces", body)
        if logs:
            body = {
                "resourceLogs": [
                    {"resource": {"attributes": self._resource(svc)}, "scopeLogs": [{"scope": scope, "logRecords": ls}]}
                    for svc, ls in logs.items()
                ]
            }
            await self._post("/v1/logs", body)

    async def _post(self, path: str, body: dict) -> None:
        for attempt in range(2):
            try:
                resp = await self.transport.send("POST", self.endpoint + path, json=body, timeout=15)
                if resp.ok:
                    return
                log.warning("telemetry %s -> HTTP %s", path, resp.status)
            except Exception as exc:  # noqa: BLE001 - telemetry must never break the pipeline
                log.warning("telemetry %s failed (attempt %d): %s", path, attempt + 1, exc)

    async def loop(self, interval: float = 10.0) -> None:
        while True:
            await self.c.sleep(interval)
            try:
                await self.flush()
            except Exception:
                log.exception("telemetry flush failed")

    # ------------------------------------------------------------------ hooks (sync, called from DB / Usage)
    def on_transition(self, item_id: int, prev: str | None, new: str) -> None:
        if not self.enabled:
            return
        try:
            now = self.c.clock()
            self._x("INSERT INTO otel_trans(item_id, status, ts) VALUES(?,?,?)", (item_id, new, now))
            item = self.c.db.item(item_id)
            repo = item["repo"] if item else None
            self.event(
                "homie-catcher",
                "CicdStage",
                {
                    "cicd.pipeline.run.id": item_id,
                    "cicd.pipeline.name": f"homie/{repo or '?'}",
                    "stage.kind": "status",
                    "stage.from": prev,
                    "stage.to": new,
                    "vcs.repository.name": repo,
                },
                now,
            )
            if new in TERMINAL and self._once(f"root:{item_id}"):
                self.emit_item(item_id, new, now)
        except Exception:
            log.exception("telemetry transition hook failed")

    def on_milestone(self, item_id: int, key: str) -> None:
        if not self.enabled:
            return
        try:
            item = self.c.db.item(item_id)
            repo = item["repo"] if item else None
            self.event(
                "homie-catcher",
                "CicdStage",
                {
                    "cicd.pipeline.run.id": item_id,
                    "cicd.pipeline.name": f"homie/{repo or '?'}",
                    "stage.kind": "milestone",
                    "stage.to": key.split(":")[1] if ":" in key else key,
                    "stage.key": key,
                    "vcs.repository.name": repo,
                },
            )
        except Exception:
            log.exception("telemetry milestone hook failed")

    def on_claude(
        self, feature: str, result: dict | None, *, ok: bool, error: str | None, source: str, repo: str | None, ctx: dict | None = None
    ) -> None:
        """One headless Claude run (catcher `claude -p` or claude-code-action): a gen_ai span + LlmChatCompletionSummary."""
        if not self.enabled:
            return
        try:
            ctx = ctx or {}
            r = result or {}
            u = r.get("usage") or {}
            now = self.c.clock()
            end = ctx.get("ts") or now
            dur_ms = int(r.get("duration_ms") or 0)
            start = end - dur_ms / 1000
            item_id = current_item.get()
            if item_id is None and repo and ctx.get("number"):
                item_id = self.item_for_number(repo, int(ctx["number"]))
            run_id = ctx.get("run_id")
            if run_id and item_id is not None:
                self.map("run", run_id, item_id)
            if item_id is not None:
                trace, parent = item_trace_id(item_id), item_root_span_id(item_id)
            elif run_id:
                trace, parent = _h("gha-run-trace", run_id)[:32], run_span_id(run_id)
            else:
                trace, parent = _h("claude-run", now, os.urandom(4).hex())[:32], None
            if run_id:
                parent = run_span_id(run_id)  # inside its workflow run (emitted later with the same ids)
            models = sorted((r.get("modelUsage") or {}).keys())
            model = (
                ",".join(models) or self.c.cfg.claude_model or ("claude-code-action-default" if source == "gha" else "claude-code-default")
            )
            tin, tout = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
            cr, cc = int(u.get("cache_read_input_tokens") or 0), int(u.get("cache_creation_input_tokens") or 0)
            cost = float(r.get("total_cost_usd") or 0)
            service = "github-actions" if source == "gha" else "homie-catcher"
            attrs = {
                "gen_ai.provider.name": "anthropic",
                "gen_ai.system": "anthropic",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": f"claude-code/{feature}",
                "gen_ai.request.model": model,
                "gen_ai.response.model": model,
                "gen_ai.usage.input_tokens": tin,
                "gen_ai.usage.output_tokens": tout,
                "gen_ai.usage.cache_read_input_tokens": cr,
                "gen_ai.usage.cache_creation_input_tokens": cc,
                "llm.cost_usd_equivalent": cost,
                "llm.num_turns": int(r.get("num_turns") or 0),
                "llm.feature": feature,
                "llm.source": source,
                "cicd.pipeline.run.id": item_id,
                "vcs.repository.name": repo,
                "cicd.pipeline.run.url": f"https://github.com/{self.c.cfg.github_org}/{repo}/actions/runs/{run_id}" if run_id else None,
                "error.type": (str(r.get("api_error_status") or "") or ("claude_error" if not ok else None)),
            }
            sid = self.span(service, f"claude {feature}", start, end, trace_id=trace, parent=parent, attrs=attrs, ok=ok, kind=3)
            self.event(
                service,
                "LlmChatCompletionSummary",
                {
                    "id": _h("llm", trace, sid)[:32],
                    "span_id": sid,
                    "trace_id": trace,
                    "vendor": "anthropic",
                    "ingest_source": "homie-catcher",
                    "request.model": model,
                    "response.model": model,
                    "duration": dur_ms,
                    "response.usage.prompt_tokens": tin,
                    "response.usage.completion_tokens": tout,
                    "response.usage.total_tokens": tin + tout,
                    "response.number_of_messages": int(r.get("num_turns") or 0),
                    "llm.cache_read_tokens": cr,
                    "llm.cache_creation_tokens": cc,
                    "llm.cost_usd_equivalent": cost,
                    "llm.feature": feature,
                    "llm.source": source,
                    "error": not ok,
                    "error.message": (error or "")[:300] or None,
                    "cicd.pipeline.run.id": item_id,
                    "vcs.repository.name": repo,
                },
                end,
            )
            if item_id is not None:
                self._x(
                    "INSERT INTO otel_llm(item_id, cost, tin, tout, model, ts) VALUES(?,?,?,?,?,?)", (item_id, cost, tin, tout, model, now)
                )
        except Exception:
            log.exception("telemetry claude hook failed")

    # ------------------------------------------------------------------ one pipeline item = one trace
    def emit_item(self, item_id: int, status: str, now: float) -> None:
        item = self.c.db.item(item_id)
        if not item:
            return
        trace, root = item_trace_id(item_id), item_root_span_id(item_id)
        repo = item["repo"] or "?"
        created = float(item["created_at"])
        ms = {r["key"]: float(r["announced_at"]) for r in self._x("SELECT key, announced_at FROM milestones WHERE item_id=?", (item_id,))}
        prs = [dict(r) for r in self._x("SELECT pr_number, head_sha FROM pull_requests WHERE item_id=? ORDER BY pr_number", (item_id,))]
        merge = [r["key"] for r in self._x("SELECT key FROM otel_map WHERE kind='merge' AND item_id=?", (item_id,))]
        trigger = "newrelic" if self._x("SELECT 1 FROM nr_issues WHERE item_id=?", (item_id,)).fetchone() else "owner"
        llm = self._x(
            "SELECT COUNT(*) AS n, COALESCE(SUM(cost),0) AS c, COALESCE(SUM(tin),0) AS i, COALESCE(SUM(tout),0) AS o,"
            " GROUP_CONCAT(DISTINCT model) AS m FROM otel_llm WHERE item_id=?",
            (item_id,),
        ).fetchone()
        deployed = any(k.endswith(":deployed") for k in ms)
        timeout = any(k.endswith(":deploy-timeout") for k in ms)
        result = {"rejected": "cancellation", "error": "error"}.get(status) or (
            "success" if deployed or not prs else "timeout" if timeout else "success"
        )
        try:
            title = (json.loads(item["spec_json"] or "{}") or {}).get("title")
        except ValueError:
            title = None
        base = {"cicd.pipeline.name": f"homie/{repo}", "cicd.pipeline.run.id": item_id, "vcs.repository.name": repo}
        # status timeline: created -> each transition (live hook rows), as `stage <status>` spans
        trans = [
            (r["status"], float(r["ts"])) for r in self._x("SELECT status, ts FROM otel_trans WHERE item_id=? ORDER BY ts", (item_id,))
        ]
        for (st, t0), nxt in zip(trans, trans[1:] + [(None, now)]):
            if st not in TERMINAL and nxt[1] >= t0:
                self.span(
                    "homie-catcher",
                    f"stage {st}",
                    t0,
                    nxt[1],
                    trace_id=trace,
                    parent=root,
                    attrs={**base, "cicd.pipeline.task.name": st, "cicd.pipeline.task.type": "status"},
                )
        # phases from the milestones (poll granularity 60 s; works for items that predate this code too)
        events = [(t, k, {}) for k, t in sorted(ms.items(), key=lambda kv: kv[1])]
        for pr in prs:
            n = pr["pr_number"]
            opened, merged = ms.get(f"pr{n}:opened"), ms.get(f"pr{n}:merged")
            dep = ms.get(f"pr{n}:deployed") or ms.get(f"pr{n}:deploy-timeout")
            pattrs = {**base, "vcs.change.id": n, "vcs.ref.head.revision": pr["head_sha"]}
            if opened:
                self.span(
                    "homie-catcher",
                    "phase agent build",
                    created,
                    opened,
                    trace_id=trace,
                    parent=root,
                    attrs={**pattrs, "cicd.pipeline.task.name": "agent_build"},
                )
            if opened and merged:
                self.span(
                    "homie-catcher",
                    "phase review and merge",
                    opened,
                    merged,
                    trace_id=trace,
                    parent=root,
                    attrs={**pattrs, "cicd.pipeline.task.name": "review_merge"},
                )
            if merged and dep:
                self.span(
                    "homie-catcher",
                    "phase deploy",
                    merged,
                    dep,
                    trace_id=trace,
                    parent=root,
                    ok=not timeout,
                    attrs={**pattrs, "cicd.pipeline.task.name": "deploy"},
                )
        attrs = {
            **base,
            "cicd.pipeline.result": result,
            "cicd.pipeline.title": (title or "")[:150] or None,
            "pipeline.status": status,
            "pipeline.trigger": trigger,
            "pipeline.trigger.user": self.c.cfg.owner_mention if trigger == "owner" else "newrelic",
            "vcs.change.id": ",".join(str(p["pr_number"]) for p in prs) or None,
            "vcs.issue.id": item["gh_issue_number"],
            "vcs.ref.merge.revision": ",".join(merge) or None,
            "pipeline.deployed": deployed,
            "llm.runs": int(llm["n"]),
            "llm.cost_usd_equivalent": float(llm["c"]),
            "gen_ai.usage.input_tokens": int(llm["i"]),
            "gen_ai.usage.output_tokens": int(llm["o"]),
            "gen_ai.request.model": llm["m"],
        }
        self.span(
            "homie-catcher",
            f"pipeline {repo}",
            created,
            now,
            trace_id=trace,
            span_id=root,
            attrs=attrs,
            ok=result in ("success", "cancellation"),
            kind=2,
            events=events,
        )
        self.event("homie-catcher", "CicdPipelineRun", {**attrs, "duration.s": round(now - created, 1), "trace.id": trace}, now)

    # ------------------------------------------------------------------ periodic: GitHub Actions + Argo CD
    async def backfill(self) -> int:
        """First start only: map recent items' PR head/merge commits, then emit items that finished in the last 46 h
        (phases from the milestones table; their live status timeline was not recorded yet)."""
        if not self._once("backfill:v1"):
            return 0
        since = self.c.clock() - BACKFILL_S
        rows = self._x("SELECT id, repo, status, updated_at FROM items WHERE updated_at > ? AND repo IS NOT NULL", (since,)).fetchall()
        pulls: dict[str, list[dict]] = {}
        for r in rows:
            for pr in self._x("SELECT pr_number FROM pull_requests WHERE item_id=?", (r["id"],)).fetchall():
                if r["repo"] not in pulls:
                    pulls[r["repo"]] = await self.c.gh.list_pulls(r["repo"])
                for p in pulls[r["repo"]]:
                    if p.get("number") == pr["pr_number"]:
                        self.map_pr(r["repo"], p, r["id"])
        n = 0
        for r in rows:
            if r["status"] in TERMINAL and self._once(f"root:{r['id']}"):
                self.emit_item(r["id"], r["status"], float(r["updated_at"]))
                n += 1
        return n

    def map_pr(self, repo: str, pr: dict, item_id: int) -> None:
        """A PR's head commit and (once merged) its merge commit belong to the item: CI, review and the deploy join its trace."""
        if not self.enabled:
            return
        sha = (pr.get("head") or {}).get("sha")
        if sha:
            self.map("sha", f"{repo}:{sha}", item_id)
        if pr.get("merged_at") and pr.get("merge_commit_sha"):
            self.map("merge", pr["merge_commit_sha"], item_id)
            self.map("sha", f"{repo}:{pr['merge_commit_sha']}", item_id)

    async def tick(self) -> None:
        if not self.enabled:
            return
        for name, fn in (("backfill", self.backfill), ("workflow runs", self.export_runs), ("deploys", self.watch_deploys)):
            try:
                await fn()
            except Exception:
                log.exception("telemetry %s failed", name)
        await self.flush()

    async def export_runs(self) -> int:
        gh = self.c.gh
        repos = sorted(set(selectable_repos(await self.c.org_repos())) | set(EXTRA_RUN_REPOS))
        now, n = self.c.clock(), 0
        for repo in repos:
            if gh.rate_limited():
                return n
            resp = await gh.request("GET", f"/repos/{gh.org}/{repo}/actions/runs", params={"status": "completed", "per_page": 30})
            for run in (resp.body or {}).get("workflow_runs", []):
                key = f"run:{run.get('id')}:{run.get('run_attempt', 1)}"
                done = iso_ts(run.get("updated_at")) or now
                if self._sent(key) or now - done < RUN_SETTLE_S:
                    continue
                if now - done > BACKFILL_S:
                    self._once(key)  # too old to send: remember it as seen
                    continue
                jobs = []
                if run.get("conclusion") != "skipped":
                    jr = await gh.request("GET", f"/repos/{gh.org}/{repo}/actions/runs/{run['id']}/jobs", params={"per_page": 50})
                    jobs = (jr.body or {}).get("jobs", [])
                if self._once(key):
                    self.emit_run(repo, run, jobs)
                    n += 1
        return n

    def emit_run(self, repo: str, run: dict, jobs: list[dict]) -> None:
        rid, attempt = run.get("id"), run.get("run_attempt", 1)
        sha = run.get("head_sha") or ""
        item_id = self.lookup("run", rid)
        if item_id is None:
            item_id = self.lookup("sha", f"{repo}:{sha}")
        trace = item_trace_id(item_id) if item_id is not None else _h("gha-run-trace", rid)[:32]
        parent = item_root_span_id(item_id) if item_id is not None else None
        start = iso_ts(run.get("run_started_at")) or iso_ts(run.get("created_at")) or self.c.clock()
        end = iso_ts(run.get("updated_at")) or start
        result = gha_result(run.get("conclusion"))
        prs = [p.get("number") for p in run.get("pull_requests") or []]
        base = {
            "cicd.pipeline.name": run.get("name"),
            "cicd.pipeline.run.id": rid,
            "cicd.pipeline.run.attempt": attempt,
            "cicd.pipeline.run.url.full": run.get("html_url"),
            "cicd.pipeline.trigger.event": run.get("event"),
            "vcs.repository.name": repo,
            "vcs.ref.head.name": run.get("head_branch"),
            "vcs.ref.head.revision": sha,
            "vcs.change.id": ",".join(map(str, prs)) or None,
            "pipeline.item.id": item_id,
            "enduser.id": (run.get("triggering_actor") or {}).get("login"),
        }
        rsid = self.span(
            "github-actions",
            f"workflow {run.get('name')}",
            start,
            end,
            trace_id=trace,
            span_id=run_span_id(rid, attempt),
            parent=parent,
            attrs={**base, "cicd.pipeline.result": result},
            ok=result in ("success", "skip"),
            kind=2,
        )
        queued = 0.0
        for job in jobs:
            js, je = iso_ts(job.get("started_at")), iso_ts(job.get("completed_at"))
            if not js or not je:
                continue
            jres = gha_result(job.get("conclusion"))
            created = iso_ts(job.get("created_at"))
            queued += max(0.0, js - created) if created else 0.0
            jattrs = {
                **base,
                "cicd.pipeline.task.name": job.get("name"),
                "cicd.pipeline.task.run.id": job.get("id"),
                "cicd.pipeline.task.run.result": jres,
                "cicd.worker.name": job.get("runner_name"),
                "cicd.worker.labels": ",".join(job.get("labels") or []) or None,
                "cicd.pipeline.task.queue_s": round(js - created, 1) if created else None,
            }
            jsid = self.span(
                "github-actions",
                f"job {job.get('name')}",
                js,
                je,
                trace_id=trace,
                parent=rsid,
                attrs=jattrs,
                ok=jres in ("success", "skip"),
            )
            for step in job.get("steps") or []:
                ss, se = iso_ts(step.get("started_at")), iso_ts(step.get("completed_at"))
                if ss and se:
                    sres = gha_result(step.get("conclusion"))
                    self.span(
                        "github-actions",
                        f"step {step.get('name')}",
                        ss,
                        se,
                        trace_id=trace,
                        parent=jsid,
                        ok=sres in ("success", "skip"),
                        attrs={
                            **base,
                            "cicd.pipeline.task.name": job.get("name"),
                            "cicd.pipeline.step.name": step.get("name"),
                            "cicd.pipeline.step.number": step.get("number"),
                            "cicd.pipeline.task.run.result": sres,
                        },
                    )
        self.event(
            "github-actions",
            "CicdWorkflowRun",
            {
                **base,
                "cicd.pipeline.result": result,
                "conclusion": run.get("conclusion"),
                "duration.s": round(end - start, 1),
                "jobs": len(jobs),
                "queue.s": round(queued, 1),
                "trace.id": trace,
            },
            end,
        )

    async def watch_deploys(self) -> int:
        if self.k8s is None:
            return 0
        apps = {f"app-{r}": r for r in selectable_repos(await self.c.org_repos())} | PLATFORM_ARGO_APPS
        now, n = self.c.clock(), 0
        for app, entity in apps.items():
            try:
                body = await self.k8s.get(f"/apis/argoproj.io/v1alpha1/namespaces/{ARGO_NS}/applications/{app}")
            except Exception as exc:  # noqa: BLE001 - app may not exist yet (new repo) or RBAC may be missing
                log.debug("argo app %s: %s", app, exc)
                continue
            st = (body or {}).get("status") or {}
            images = ",".join((st.get("summary") or {}).get("images") or []) or None
            for h in st.get("history") or []:
                key = f"deploy:{app}:{h.get('id')}:{h.get('revision')}"
                end = iso_ts(h.get("deployedAt"))
                if not end or self._sent(key):
                    continue
                if now - end > BACKFILL_S:
                    self._once(key)  # too old to send: remember it as seen
                    continue
                if not self._once(key):
                    continue
                await self.emit_deploy(app, entity, h, end, images, st)
                n += 1
        return n

    async def emit_deploy(self, app: str, entity: str | None, h: dict, end: float, images: str | None, st: dict) -> None:
        rev = str(h.get("revision") or next(iter(h.get("revisions") or []), ""))
        start = iso_ts(h.get("deployStartedAt")) or end
        repo = app[4:] if app.startswith("app-") else None
        item_id = self.lookup("merge", rev) if rev else None
        trace = item_trace_id(item_id) if item_id is not None else _h("argo-deploy", app, h.get("id"))[:32]
        parent = item_root_span_id(item_id) if item_id is not None else None
        attrs = {
            "deployment.name": app,
            "deployment.id": h.get("id"),
            "deployment.environment.name": "prod",
            "argocd.app.name": app,
            "vcs.ref.head.revision": rev,
            "vcs.repository.name": repo,
            "container.image.names": images,
            "pipeline.item.id": item_id,
            "argocd.health": (st.get("health") or {}).get("status"),
            "argocd.sync": (st.get("sync") or {}).get("status"),
        }
        self.span("argocd", f"deploy {app}", start, end, trace_id=trace, parent=parent, attrs=attrs, kind=1)
        marker = await self.change_marker(entity, rev, app, images, end, trace) if entity else None
        self.event(
            "argocd",
            "CicdDeployment",
            {**attrs, "duration.s": round(end - start, 1), "trace.id": trace, "nr.entity.name": entity, "nr.changeTracking.id": marker},
            end,
        )

    # ------------------------------------------------------------------ New Relic change tracking (NerdGraph)
    def _nr_key(self) -> str | None:
        try:
            with open(self.nr_key_file, encoding="utf-8") as fh:
                return fh.read().strip() or None
        except OSError:
            return None

    async def _nerdgraph(self, query: str, variables: dict) -> dict | None:
        key = self._nr_key()
        if not key or not self.nr_account_id:
            return None
        resp = await self.transport.send(
            "POST",
            NERDGRAPH.get(self.nr_region, NERDGRAPH["EU"]),
            json={"query": query, "variables": variables},
            headers={"API-Key": key, "Content-Type": "application/json"},
            timeout=20,
        )
        if not resp.ok or not isinstance(resp.body, dict):
            log.warning("NerdGraph HTTP %s", resp.status)
            return None
        if resp.body.get("errors"):
            log.warning("NerdGraph errors: %s", str(resp.body["errors"])[:300])
        return resp.body.get("data")

    async def change_marker(self, entity: str, rev: str, app: str, images: str | None, ts: float, trace: str) -> str | None:
        """Change tracking EVENT (category Deployment / type Basic) on the entity named `entity`. `changeTrackingCreateEvent`
        replaces the legacy `changeTrackingCreateDeployment`; entitySearch spares a GUID lookup."""
        try:
            safe = entity.replace("'", "")
            q = (
                "mutation($e: ChangeTrackingCreateEventInput!) {"
                " changeTrackingCreateEvent(changeTrackingEvent: $e) { changeTrackingEvent { changeTrackingId } } }"
            )
            dep = {"version": rev[:12] or "unknown", "commit": rev or None, "changelog": images}
            e = {
                "categoryAndTypeData": {
                    "kind": {"category": "Deployment", "type": "Basic"},
                    "categoryFields": {"deployment": {k: v for k, v in dep.items() if v}},
                },
                "entitySearch": {"query": f"name = '{safe}' AND accountId = {int(self.nr_account_id or 0)} AND type = 'SERVICE'"},
                "shortDescription": f"Argo CD sync of {app}",
                "description": f"Argo CD synced {app} to {rev[:12]} (homie ai-pipeline)",
                "user": "argocd",
                "groupId": f"homie-pipeline:{app}",
                "timestamp": int(ts * 1000),
                "customAttributes": {TAG: True, "argocd.app": app, "trace.id": trace, "cicd.pipeline.provider": "homie"},
            }
            data = await self._nerdgraph(q, {"e": e})
            ev = ((data or {}).get("changeTrackingCreateEvent") or {}).get("changeTrackingEvent") or {}
            return ev.get("changeTrackingId")
        except Exception:
            log.exception("change tracking marker failed for %s", app)
            return None
