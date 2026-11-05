"""D80: pipeline telemetry (OTLP JSON to the collector, custom events, change tracking markers)."""

import asyncio
import json

from catcher.poller import Poller, prod_version_url
from catcher.telemetry import Telemetry, item_root_span_id, item_trace_id, run_span_id
from catcher.transport import Response
from tests.fakes import click, last_buttons, make_bot, post, spec, triage_mode

OTLP = "http://otel-collector.observability.svc.cluster.local:4318"


class Recorder:
    """Transport for the telemetry: records OTLP posts and answers NerdGraph."""

    def __init__(self, guid="GUID-NOTES") -> None:
        self.posts: list[tuple[str, dict, dict]] = []
        self.guid = guid

    async def send(self, method, url, *, headers=None, json=None, params=None, timeout=30):
        self.posts.append((url, json, dict(headers or {})))
        if url.endswith("/graphql"):
            return Response(200, {"data": {"changeTrackingCreateEvent": {"changeTrackingEvent": {"changeTrackingId": "dep-1"}}}})
        return Response(200, {})

    def spans(self) -> list[dict]:
        out = []
        for url, body, _ in self.posts:
            if url.endswith("/v1/traces"):
                for rs in body["resourceSpans"]:
                    svc = next(a["value"]["stringValue"] for a in rs["resource"]["attributes"] if a["key"] == "service.name")
                    for ss in rs["scopeSpans"]:
                        out += [{**s, "service": svc, "a": _flat(s["attributes"])} for s in ss["spans"]]
        return out

    def events(self, etype=None) -> list[dict]:
        out = []
        for url, body, _ in self.posts:
            if url.endswith("/v1/logs"):
                for rl in body["resourceLogs"]:
                    for sl in rl["scopeLogs"]:
                        out += [_flat(r["attributes"]) for r in sl["logRecords"]]
        return [e for e in out if etype is None or e["newrelic.event.type"] == etype]


def _flat(attrs):
    return {a["key"]: int(a["value"]["intValue"]) if "intValue" in a["value"] else next(iter(a["value"].values())) for a in attrs}


class FakeK8s:
    def __init__(self, apps):
        self.apps = apps

    async def get(self, path):
        name = path.rsplit("/", 1)[1]
        if name not in self.apps:
            raise RuntimeError("404")
        return self.apps[name]


def enable(bot, rec, tmp_path=None, k8s=None):
    key = None
    if tmp_path is not None:
        key = tmp_path / "key"
        key.write_text("NRAK-TEST\n")
    bot.telemetry = Telemetry(bot, rec, endpoint=OTLP, nr_key_file=str(key or ""), nr_account_id="8680047", k8s=k8s)
    return bot.telemetry


def test_disabled_by_default_sends_nothing():
    async def go():
        bot, svc, _ = await make_bot([spec()])
        assert bot.telemetry.enabled is False
        await bot.handle_post(post("work on notes: dark mode"))
        await bot.run_next()
        await bot.telemetry.flush()
        assert not [c for c in svc.calls if "4318" in c.url]

    asyncio.run(go())


def test_item_is_one_trace_from_post_to_deploy():
    async def go():
        bot, svc, claude = await make_bot([spec()])
        rec = Recorder()
        tel = enable(bot, rec)
        real_run = claude.run

        async def run_and_meter(prompt):
            out = await real_run(prompt)
            claude.on_result(
                "spec",
                {
                    "total_cost_usd": 0.05,
                    "duration_ms": 4000,
                    "num_turns": 2,
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "modelUsage": {"claude-opus-5-5": {}},
                },
                None,
            )
            return out

        claude.run = run_and_meter
        triage_mode(bot)
        p = post("work on notes: dark mode")
        await bot.handle_post(p)
        await bot.run_next()
        await bot.handle_action(*click(next(b for b in last_buttons(svc) if b["name"] == "Approve")))
        await bot.drain()
        item = bot.db.latest_item(p["id"])
        iid, n = item["id"], item["gh_issue_number"]
        svc.pulls["notes"] = [
            {
                "number": 7,
                "body": f"Closes #{n}",
                "head": {"ref": "claude/x", "sha": "deadbeef"},
                "html_url": "u",
                "state": "open",
                "labels": [],
                "merged_at": None,
            }
        ]
        poller = Poller(bot)
        bot.clock.t += 600
        await poller.poll_once()
        svc.pulls["notes"][0].update(state="closed", merged_at="2027-01-15T08:20:00Z", merge_commit_sha="cafef00d")
        bot.clock.t += 600
        await poller.poll_once()
        svc.app_versions[prod_version_url("notes")] = "cafef00d"
        bot.clock.t += 120
        await poller.poll_once()
        assert bot.db.latest_item(p["id"])["status"] == "done"
        await tel.flush()

        trace = item_trace_id(iid)
        spans = rec.spans()
        assert spans and all(s["a"]["ai-pipeline"] is True for s in spans)
        mine = [s for s in spans if s["traceId"] == trace]
        names = {s["name"] for s in mine}
        assert {"pipeline notes", "claude spec", "phase agent build", "phase deploy", "stage triage", "stage running"} <= names
        root = next(s for s in mine if s["name"] == "pipeline notes")
        assert root["spanId"] == item_root_span_id(iid) and "parentSpanId" not in root
        assert root["a"]["cicd.pipeline.result"] == "success" and root["a"]["pipeline.deployed"] is True
        assert root["a"]["llm.runs"] == 1 and root["a"]["gen_ai.request.model"] == "claude-opus-5-5"
        assert all(s.get("parentSpanId") == root["spanId"] for s in mine if s is not root)
        llm = next(s for s in mine if s["name"] == "claude spec")
        assert llm["a"]["gen_ai.usage.input_tokens"] == 100 and llm["a"]["gen_ai.provider.name"] == "anthropic"
        (run,) = rec.events("CicdPipelineRun")
        assert run["cicd.pipeline.run.id"] == iid and run["trace.id"] == trace and run["ai-pipeline"] is True
        stages = rec.events("CicdStage")
        assert {e["stage.to"] for e in stages} >= {"running", "triage", "approved", "done", "opened", "merged", "deployed"}
        (summary,) = rec.events("CicdLlmRun")
        assert summary["vendor"] == "anthropic" and summary["response.usage.total_tokens"] == 150 and summary["trace_id"] == trace
        assert tel.lookup("merge", "cafef00d") == iid and tel.lookup("sha", "notes:deadbeef") == iid
        # terminal status reached again (e.g. a retry): the root is not emitted twice
        rec.posts.clear()
        tel.on_transition(iid, "approved", "done")
        await tel.flush()
        assert not [s for s in rec.spans() if s["name"] == "pipeline notes"]

    asyncio.run(go())


def test_workflow_runs_join_the_item_trace_with_jobs_and_steps():
    async def go():
        bot, svc, _ = await make_bot([])
        rec = Recorder()
        tel = enable(bot, rec)
        bot.clock.t = 1_793_361_600 + 3600  # 2026-10-30T13:00Z
        bot.db.upsert_thread("root1", "chan")
        iid = bot.db.create_item("root1", "idea", "approved")
        tel.map("sha", "notes:deadbeef", iid)
        run = {
            "id": 41,
            "run_attempt": 1,
            "name": "ci",
            "event": "pull_request",
            "conclusion": "success",
            "head_sha": "deadbeef",
            "head_branch": "claude/x",
            "run_started_at": "2026-10-30T12:00:00Z",
            "updated_at": "2026-10-30T12:03:00Z",
            "html_url": "https://github.com/x/notes/actions/runs/41",
            "pull_requests": [{"number": 7}],
        }
        fresh = {**run, "id": 42, "updated_at": "2026-10-30T12:58:00Z"}  # completed 2 min ago: waits for the usage comment
        old = {**run, "id": 40, "updated_at": "2026-10-25T12:00:00Z"}  # beyond the backfill window: never sent
        skipped = {**run, "id": 43, "conclusion": "skipped", "head_sha": "other"}
        svc.actions_runs["notes"] = [fresh, run, old, skipped]
        svc.actions_jobs[41] = [
            {
                "id": 9,
                "name": "test",
                "conclusion": "success",
                "created_at": "2026-10-30T12:00:05Z",
                "started_at": "2026-10-30T12:00:20Z",
                "completed_at": "2026-10-30T12:02:50Z",
                "runner_name": "GitHub Actions 1",
                "labels": ["ubuntu-24.04-arm"],
                "steps": [
                    {
                        "name": "pytest",
                        "number": 4,
                        "conclusion": "success",
                        "started_at": "2026-10-30T12:01:00Z",
                        "completed_at": "2026-10-30T12:02:40Z",
                    }
                ],
            }
        ]
        assert await tel.export_runs() == 2
        assert await tel.export_runs() == 0  # once each
        await tel.flush()
        spans = rec.spans()
        wf = next(s for s in spans if s["name"] == "workflow ci" and s["a"]["cicd.pipeline.run.id"] == 41)
        assert wf["service"] == "github-actions" and wf["traceId"] == item_trace_id(iid)
        assert wf["parentSpanId"] == item_root_span_id(iid) and wf["spanId"] == run_span_id(41, 1)
        job = next(s for s in spans if s["name"] == "job test")
        step = next(s for s in spans if s["name"] == "step pytest")
        assert job["parentSpanId"] == wf["spanId"] and step["parentSpanId"] == job["spanId"]
        assert job["a"]["cicd.pipeline.task.queue_s"] == 15.0
        sk = next(s for s in spans if s["a"].get("cicd.pipeline.run.id") == 43)
        assert sk["traceId"] != item_trace_id(iid) and sk["a"]["cicd.pipeline.result"] == "skip"
        assert {e["cicd.pipeline.run.id"] for e in rec.events("CicdWorkflowRun")} == {41, 43}
        assert not [c for c in svc.calls if "/runs/43/jobs" in c.url]  # no job fetch for skipped runs

    asyncio.run(go())


def test_gha_usage_maps_run_to_item_and_is_counted_once():
    async def go():
        bot, svc, _ = await make_bot([])
        rec = Recorder()
        tel = enable(bot, rec)
        bot.clock.t = 1_793_361_600 + 600  # 10 min after the comment
        bot.db.upsert_thread("root1", "chan")
        iid = bot.db.create_item("root1", "idea", "approved")
        bot.db.update_item(iid, repo="notes", gh_issue_number=5)
        mark = {"wf": "agent", "run": "777", "cost": 0.8, "in": 1, "out": 2, "cr": 3, "cc": 4, "ms": 5000, "turns": 9, "err": False}
        svc.repo_comments["notes"] = [
            {
                "id": 11,
                "user": {"login": "kiu-homie-bot[bot]"},
                "updated_at": "2026-10-30T12:00:00Z",
                "created_at": "2026-10-30T12:00:00Z",
                "issue_url": "https://api.github.com/repos/o/notes/issues/5",
                "body": f"<!-- homie-usage {json.dumps(mark)} -->",
            }
        ]
        await bot.usage.ingest_gha()
        bot.usage.put("cursor:notes", "2026-01-01T00:00:00Z")  # re-read the same comment
        await bot.usage.ingest_gha()
        await tel.flush()
        (s,) = [s for s in rec.spans() if s["name"] == "claude agent_run"]
        assert s["service"] == "github-actions" and s["traceId"] == item_trace_id(iid) and s["parentSpanId"] == run_span_id("777")
        assert s["a"]["gen_ai.request.model"] == "claude-code-action-default"
        assert tel.lookup("run", "777") == iid
        assert len(rec.events("CicdLlmRun")) == 1
        # a span older than New Relic keeps (~20 h, measured) is not sent; its event still is
        rec.posts.clear()
        tel.on_claude("review", {"duration_ms": 1000}, ok=True, error=None, source="gha", repo="notes", ctx={"ts": bot.clock.t - 30 * 3600})
        await tel.flush()
        assert not rec.spans() and len(rec.events("CicdLlmRun")) == 1

    asyncio.run(go())


def test_argo_deploy_span_event_and_change_marker(tmp_path):
    async def go():
        bot, svc, _ = await make_bot([])
        rec = Recorder()
        app = {
            "status": {
                "summary": {"images": ["ghcr.io/x/notes:cafef00"]},
                "health": {"status": "Healthy"},
                "sync": {"status": "Synced"},
                "history": [
                    {"id": 3, "revision": "cafef00d", "deployStartedAt": "2026-10-30T12:05:00Z", "deployedAt": "2026-10-30T12:05:30Z"},
                    {"id": 2, "revision": "0ld", "deployedAt": "2026-10-20T12:00:00Z"},
                ],
            }
        }
        multi = {
            "status": {
                "history": [
                    {
                        "id": 7,
                        "revisions": ["g1tops", "beefcafe"],
                        "deployedAt": "2026-10-30T12:30:00Z",
                        "sources": [
                            {"repoURL": "git@github.com:o/homelab-gitops.git"},
                            {"repoURL": "https://github.com/o/agentic-demo.git"},
                        ],
                    }
                ]
            }
        }
        tel = enable(bot, rec, tmp_path, k8s=FakeK8s({"app-notes": app, "app-agentic-demo": multi}))
        bot.clock.t = 1_793_361_600 + 3600
        bot.db.upsert_thread("root1", "chan")
        iid = bot.db.create_item("root1", "idea", "approved")
        tel.map("merge", "cafef00d", iid)
        assert await tel.watch_deploys() == 2
        assert await tel.watch_deploys() == 0
        await tel.flush()
        (d,) = [s for s in rec.spans() if s["name"] == "deploy app-notes"]
        assert d["service"] == "argocd" and d["traceId"] == item_trace_id(iid)
        ev2 = next(e for e in rec.events("CicdDeployment") if e["deployment.name"] == "app-agentic-demo")
        assert ev2["vcs.ref.head.revision"] == "beefcafe"  # the app repo's revision, not the gitops one
        (ev,) = [e for e in rec.events("CicdDeployment") if e["deployment.name"] == "app-notes"]
        assert ev["nr.changeTracking.id"] == "dep-1" and ev["vcs.ref.head.revision"] == "cafef00d"
        (mut,) = [b for u, b, h in rec.posts if u.endswith("/graphql") and "'notes'" in b["variables"]["e"]["entitySearch"]["query"]]
        e = mut["variables"]["e"]
        assert e["entitySearch"]["query"] == "name = 'notes' AND accountId = 8680047 AND type = 'SERVICE'"
        assert e["categoryAndTypeData"]["kind"] == {"category": "Deployment", "type": "Basic"}
        assert e["categoryAndTypeData"]["categoryFields"]["deployment"]["commit"] == "cafef00d"
        assert e["customAttributes"]["ai-pipeline"] is True and e["customAttributes"]["trace.id"] == item_trace_id(iid)
        assert all(h.get("API-Key") == "NRAK-TEST" for u, b, h in rec.posts if u.endswith("/graphql"))

    asyncio.run(go())


def test_telemetry_failures_never_break_the_pipeline():
    async def go():
        bot, svc, _ = await make_bot([spec()])

        class Boom:
            async def send(self, *a, **k):
                raise OSError("collector down")

        tel = enable(bot, Boom())
        await bot.handle_post(post("work on notes: dark mode"))
        await bot.run_next()
        await tel.flush()  # logged, not raised
        tel.on_claude("spec", None, ok=False, error="x", source="catcher", repo=None)
        await tel.flush()

    asyncio.run(go())


def test_claude_child_env_carries_otel_and_traceparent(monkeypatch):
    from catcher.claude_runner import child_env
    from catcher.telemetry import current_item

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" not in child_env("t")  # off unless the collector endpoint is set
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", OTLP)
    env = child_env("t")
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1" and env["OTEL_EXPORTER_OTLP_ENDPOINT"] == OTLP
    assert "ai-pipeline=true" in env["OTEL_RESOURCE_ATTRIBUTES"] and "TRACEPARENT" not in env
    assert not any(k.startswith("OTEL_LOG_") for k in env)  # prompts / tool details / raw bodies stay off
    token = current_item.set(42)
    try:
        assert child_env("t")["TRACEPARENT"] == f"00-{item_trace_id(42)}-{item_root_span_id(42)}-01"
    finally:
        current_item.reset(token)


def test_marker_retried_until_the_entity_exists(tmp_path):
    async def go():
        bot, svc, _ = await make_bot([])

        class NoEntityYet(Recorder):
            ready = False

            async def send(self, method, url, *, headers=None, json=None, params=None, timeout=30):
                if url.endswith("/graphql") and not self.ready:
                    self.posts.append((url, json, dict(headers or {})))
                    return Response(200, {"data": {"changeTrackingCreateEvent": None}, "errors": [{"message": "No entities found"}]})
                return await super().send(method, url, headers=headers, json=json, params=params, timeout=timeout)

        rec = NoEntityYet()
        app = {"status": {"history": [{"id": 1, "revision": "abc", "deployedAt": "2026-10-30T12:00:00Z"}]}}
        tel = enable(bot, rec, tmp_path, k8s=FakeK8s({"catcher": app}))
        bot.clock.t = 1_793_361_600 + 600
        await tel.watch_deploys()
        assert (await tel.retry_markers()) == 0
        rec.ready = True
        assert (await tel.retry_markers()) == 1 and (await tel.retry_markers()) == 0
        bot.clock.t += 30 * 3600  # an old deploy never gets a marker attempt (NerdGraph refuses > 24 h)
        app["status"]["history"].append({"id": 2, "revision": "def", "deployedAt": "2026-10-30T12:00:00Z"})
        n = len(rec.posts)
        await tel.watch_deploys()
        assert not [p for p in rec.posts[n:] if p[0].endswith("/graphql")]

    asyncio.run(go())


def test_issue_body_carries_the_item_traceparent():
    async def go():
        bot, svc, _ = await make_bot([spec()])
        triage_mode(bot)
        p = post("work on notes: dark mode")
        await bot.handle_post(p)
        await bot.run_next()
        await bot.handle_action(*click(next(b for b in last_buttons(svc) if b["name"] == "Approve")))
        await bot.drain()
        iid = bot.db.latest_item(p["id"])["id"]
        (issue,) = svc.issues
        assert f"<!-- homie-trace 00-{item_trace_id(iid)}-{item_root_span_id(iid)}-01 -->" in issue["body"]

    asyncio.run(go())
