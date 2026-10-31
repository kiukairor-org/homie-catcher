"""D75: SRE agent on demand. Owner orders in ~<repo> wake it (Job in namespace sre), alerts may wake it for
investigation only, the budget gate applies, findings and tool-logged actions are posted in the thread."""

import asyncio
import json

import pytest

from catcher import sre_job, sre_mcp
from catcher.brainstorm import Brainstorm
from catcher.github import EndpointNotAllowed
from catcher.sre import JOB_DEADLINE_S, SreManager, findings_markdown, parse_result
from tests.fakes import FakeBrainstormRunner, FakeServices, make_bot, post

REPOS = ("houss", "agentic-demo", "app-template", "homie-catcher", "papl", "release")
IMAGE = "ghcr.io/kiukairor-org/homie-catcher:abc1234"


class FakeK8s:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.jobs: dict[str, dict] = {}
        self.logs: dict[str, str] = {}
        self.deleted: list[str] = []

    async def create(self, path, body):
        self.created.append((path, body))
        if body["kind"] == "Job":
            name = body["metadata"]["name"]
            self.jobs[name] = {"metadata": {"name": name, "uid": f"uid-{name}"}, "status": {}}
            return self.jobs[name]
        return body

    async def get(self, path):
        if "/pods?" in path:
            run = path.split("homie%2Frun%3D")[1]
            return {"items": [{"metadata": {"name": f"pod-{run}"}}]}
        return self.jobs[path.rsplit("/", 1)[1]]

    async def log(self, path):
        return self.logs.get(path.split("/pods/")[1].split("/")[0], "")

    async def delete(self, path):
        self.deleted.append(path)


def result_line(actions=(), error=None, output=True):
    out = {"summary": "The client treats every sync failure as offline", "cause": "web/app.js catch-all sets Offline",
           "confidence": "high", "evidence": [{"claim": "catch block sets offline", "source": "web/app.js:212"},
                                              {"claim": "API healthy", "source": "nrql: 0 5xx since 0cc9e58"}],
           "proposed_fix": "Distinguish network errors from HTTP errors", "not_verified": ["UI in the browser"]}  # fmt: skip
    res = {
        "total_cost_usd": 0.42,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "num_turns": 12,
        "duration_ms": 90000,
        "is_error": False,
    }
    return "noise\nSRE_RESULT " + json.dumps({"run": "x", "output": out if output else None, "error": error, "result": res,
                                              "actions": list(actions)}) + "\n"  # fmt: skip


async def sre_bot(outputs, **cfg):
    bot, svc, _ = await make_bot([], services=FakeServices(repos=REPOS), APP_CHANNELS="true", SRE_IMAGE=IMAGE,
                                 NEW_RELIC_ACCOUNT_ID="8680047", **cfg)  # fmt: skip
    runner = FakeBrainstormRunner(outputs)
    bot.brainstorm = Brainstorm(bot, runner)
    k8s = FakeK8s()
    bot.sre = SreManager(bot, k8s)
    watched = []
    bot.sre.watch = watched.append  # tests drive finish() themselves
    await bot.startup()
    await bot.channels.reconcile(force=True)
    return bot, svc, runner, k8s, watched, bot.channels.channel_for_repo("houss")


def thread(svc, root):
    return [p["message"] for p in svc.posts if p.get("root_id") == root]


def job_of(k8s):
    return next(b for p, b in k8s.created if b["kind"] == "Job")


def investigate(fix=False):
    return {"reply": "I'll have the SRE agent look at the sync path, logs and NR.", "intent": "investigate", "request": None,
            "fix_requested": fix}  # fmt: skip


def test_owner_order_in_app_channel_starts_a_bounded_least_privilege_job():
    async def go():
        bot, svc, runner, k8s, watched, ch = await sre_bot([investigate()])
        root = post("investigate why houss shows Offline, logs and all", channel=ch)
        await bot.handle_post(root)
        assert "SRE AGENT" in runner.runs[0]["system_prompt"]
        (issue,) = svc.issues
        assert issue["repo"] == "houss" and issue["labels"] == ["sre"]  # tracking issue, NOT `agent`: nothing builds it
        job = job_of(k8s)
        spec, pod = job["spec"], job["spec"]["template"]["spec"]
        assert job["metadata"]["namespace"] == "sre" and spec["activeDeadlineSeconds"] == JOB_DEADLINE_S == 900
        assert spec["ttlSecondsAfterFinished"] > 0 and spec["backoffLimit"] == 0
        assert pod["serviceAccountName"] == "sre-houss"  # read-only SA (no restart permission)
        env = {e["name"]: e["value"] for e in pod["containers"][0]["env"]}
        assert env["MODE"] == "owner" and env["REPO"] == "houss" and env["ISSUE_NUMBER"] == str(issue["number"])
        assert "Offline" in env["SRE_TASK"] and env["NEW_RELIC_ACCOUNT_ID"] == "8680047"
        assert not any("TOKEN" in k and k != "GITHUB_TOKEN_FILE" for k in env) and pod["containers"][0]["image"] == IMAGE
        assert pod["containers"][0]["envFrom"] == [{"secretRef": {"name": "sre-claude-token"}}]
        secret = next(b for p, b in k8s.created if b["kind"] == "Secret")
        assert secret["metadata"]["ownerReferences"][0]["name"] == job["metadata"]["name"]  # GC'd with the Job
        tok = [c for c in svc.calls if c.path.endswith("/access_tokens") and c.json and c.json.get("repositories") == ["houss"]][-1]
        assert tok.json["permissions"] == {"contents": "write", "issues": "write", "pull_requests": "write", "metadata": "read"}
        run_id = watched[0]
        assert any(m.startswith(f"🔎 Investigating… (run `{run_id}`") for m in thread(svc, root["id"]))
        item = bot.db.latest_item(root["id"])
        assert item["status"] == "investigating" and item["repo"] == "houss"
        # the Job finishes with a fix PR in its tool log
        k8s.logs[f"pod-{run_id}"] = result_line(
            [{"action": "fix_pr", "url": "https://github.com/kiukairor-org/houss/pull/13", "number": 13}]
        )
        k8s.jobs[job["metadata"]["name"]]["status"] = {"succeeded": 1}
        await bot.sre._watch(run_id)
        last = thread(svc, root["id"])[-1]
        assert "**SRE findings**" in last and "confidence: **high**" in last and "web/app.js:212" in last
        assert "🔧 Fix PR opened: https://github.com/kiukairor-org/houss/pull/13" in last
        assert bot.db.item(item["id"])["status"] == "approved"  # the poller now posts CI/review/merge/deploy
        assert bot.sre.run(run_id)["status"] == "done"
        u = bot.db._x("SELECT feature, cost_usd, repo FROM claude_usage").fetchall()
        assert [(r["feature"], r["cost_usd"], r["repo"]) for r in u] == [("sre", 0.42, "houss")]

    asyncio.run(go())


def test_fix_order_uses_the_fix_service_account_and_no_pr_closes_the_issue():
    async def go():
        bot, svc, runner, k8s, watched, ch = await sre_bot([investigate(fix=True)])
        root = post("houss pod looks stuck, check logs and fix it", channel=ch)
        await bot.handle_post(root)
        job = job_of(k8s)
        env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert job["spec"]["template"]["spec"]["serviceAccountName"] == "sre-houss-fix" and env["MODE"] == "fix"
        k8s.logs[f"pod-{watched[0]}"] = result_line([{"action": "restart", "deployment": "app-houss/app", "at": "2026-10-31T16:00:00Z"}])
        await bot.sre.finish(watched[0], "succeeded")
        assert "🔄 Rollout restart of `app-houss/app`" in thread(svc, root["id"])[-1]
        item = bot.db.latest_item(root["id"])
        assert item["status"] == "done"
        closes = [c for c in svc.calls if c.method == "PATCH" and c.path.endswith(f"/issues/{item['gh_issue_number']}")]
        assert closes and closes[0].json == {"state": "closed", "state_reason": "completed"}

    asyncio.run(go())


def test_budget_gate_and_one_run_per_repo():
    async def go():
        bot, svc, runner, k8s, watched, ch = await sre_bot([investigate(), investigate()])
        bot.usage.allow = lambda f: f != "sre"
        bot.usage.paused_text = lambda: "⛔ Budget reached. Type `unlock` to continue."
        r1 = post("investigate the 500s", channel=ch)
        await bot.handle_post(r1)
        assert not k8s.created and not svc.issues and "unlock" in thread(svc, r1["id"])[-1]
        bot.usage.allow = lambda f: True
        await bot.handle_post(post("investigate the 500s", channel=ch))
        r3 = post("and investigate the login too", channel=ch)
        runner.outputs.append(investigate())
        await bot.handle_post(r3)
        assert len([b for p, b in k8s.created if b["kind"] == "Job"]) == 1
        assert "already going" in thread(svc, r3["id"])[-1]

    asyncio.run(go())


def test_alert_wakes_investigation_only_and_fix_still_needs_approve():
    async def go():
        bot, svc, runner, k8s, watched, ch = await sre_bot([], SRE_ON_ALERTS="true", NR_TARGET_REPO="houss")
        await bot.ingest_alerts({"issues": [{"issue_id": "nr-1", "title": "houss error rate > 20%", "priority": "CRITICAL",
                                             "entity_names": ["houss"]}]})  # fmt: skip
        job = job_of(k8s)
        env = {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        assert env["MODE"] == "alert" and env["ISSUE_NUMBER"] == "" and job["spec"]["template"]["spec"]["serviceAccountName"] == "sre-houss"
        assert not svc.issues  # no tracking issue, nothing for the agent
        tok = [c for c in svc.calls if c.path.endswith("/access_tokens") and c.json and c.json.get("repositories") == ["houss"]][-1]
        assert set(tok.json["permissions"].values()) == {"read"}
        k8s.logs[f"pod-{watched[0]}"] = result_line()
        await bot.sre.finish(watched[0], "succeeded")
        run = bot.sre.run(watched[0])
        msgs = thread(svc, run["root_post_id"])
        assert "press **Approve** on the alert item" in msgs[-1]
        item = bot.db.latest_item(run["root_post_id"])
        assert item["status"] == "triage"  # still waiting for the owner's Approve

    asyncio.run(go())


def test_alerts_do_not_wake_it_by_default():
    async def go():
        bot, svc, runner, k8s, watched, ch = await sre_bot([], NR_TARGET_REPO="houss")
        await bot.ingest_alerts({"issues": [{"issue_id": "nr-2", "title": "x", "priority": "HIGH", "entity_names": ["houss"]}]})  # fmt: skip
        assert not k8s.created

    asyncio.run(go())


def test_timeout_without_result_is_reported_and_job_deleted():
    async def go():
        bot, svc, runner, k8s, watched, ch = await sre_bot([investigate()])
        root = post("investigate the crash loop", channel=ch)
        await bot.handle_post(root)

        async def tick(s):
            bot.clock.t += 60  # noqa: B023

        bot.sleep = tick
        await bot.sre._watch(watched[0])
        assert "ended without findings" in thread(svc, root["id"])[-1]
        assert k8s.deleted and bot.sre.run(watched[0])["status"] == "error"

    asyncio.run(go())


def test_github_guard_for_sre_tokens():
    async def go():
        bot, svc, *_ = await sre_bot([])
        for repo in ("homie-catcher", "app-template", "release"):
            with pytest.raises(EndpointNotAllowed):
                await bot.gh.sre_token(repo, write=True)
        with pytest.raises(EndpointNotAllowed):  # never administration/workflows
            await bot.gh.request("POST", f"/app/installations/{bot.gh.installation_id}/access_tokens",
                                 json={"repositories": ["houss"], "permissions": {"contents": "write", "workflows": "write"}})  # fmt: skip
        with pytest.raises(EndpointNotAllowed):  # closing is the only issue edit on app repos
            await bot.gh.request("PATCH", "/repos/kiukairor-org/houss/issues/3", json={"title": "x"})

    asyncio.run(go())


def test_findings_and_result_parsing():
    assert parse_result("garbage") is None
    out = parse_result(result_line() + "SRE_RESULT {not json")
    assert out is None or out["output"]["confidence"] == "high"
    md = findings_markdown("r1", parse_result(result_line()), "owner")
    assert "No PR was opened" in md and "— _web/app.js:212_" in md
    assert "ended without findings" in findings_markdown("r1", {"error": "boom"}, "owner")


def test_sre_tool_guards(monkeypatch, tmp_path):
    monkeypatch.setattr(sre_mcp, "REPO", "houss")
    monkeypatch.setattr(sre_mcp, "ACTIONS_FILE", str(tmp_path / "a.jsonl"))
    assert sre_mcp.check_namespace("app-houss") and sre_mcp.check_namespace("pr-houss-12")
    for bad in ("app-agentic-demo", "catcher", "sre", "pr-houss-x", "kube-system"):
        with pytest.raises(sre_mcp.ToolError):
            sre_mcp.check_namespace(bad)
    assert sre_mcp.check_nrql("SELECT count(*) FROM Log WHERE service.name = 'houss' SINCE 1 hour ago")
    for bad in ("SELECT * FROM Log", "SELECT 1 FROM Span WHERE service.name='houss'; DROP", "mutation { x } houss"):
        with pytest.raises(sre_mcp.ToolError):
            sre_mcp.check_nrql(bad)
    with pytest.raises(sre_mcp.ToolError):
        sre_mcp.check_pr_files([{"path": ".github/workflows/ci.yml", "content": "x"}], [])
    with pytest.raises(sre_mcp.ToolError):
        sre_mcp.check_pr_files([{"path": "../etc/passwd", "content": "x"}], [])
    sre_mcp.check_pr_files([{"path": "web/app.js", "content": "ok"}], [])
    for leak in ("token ghs_" + "a" * 36, "sk-ant-oat01-abcdefghijkl", "NRAK-ABCDEFGHIJKLMNOP", "-----BEGIN RSA PRIVATE KEY-----"):
        with pytest.raises(sre_mcp.ToolError):
            sre_mcp.scan_secrets("fine", leak)
    with pytest.raises(sre_mcp.ToolError):
        sre_mcp.scan_secrets("contains supersecretvalue123", known=("supersecretvalue123",))
    monkeypatch.setattr(sre_mcp, "MODE", "alert")
    for call in (lambda: sre_mcp.open_fix_pr("t" * 10, "s", [{"path": "a", "old_string": "b", "new_string": "c"}]),
                 lambda: sre_mcp.open_revert_pr("abcdef1", "r"),
                 lambda: sre_mcp.restart_deployment()):  # fmt: skip
        with pytest.raises(sre_mcp.ToolError):
            call()
    monkeypatch.setattr(sre_mcp, "MODE", "owner")
    with pytest.raises(sre_mcp.ToolError, match="only allowed when the owner ordered a fix"):
        sre_mcp.restart_deployment()
    sre_mcp.record_action("fix_pr", url="u")
    with pytest.raises(sre_mcp.ToolError, match="already opened a PR"):
        sre_mcp.open_fix_pr("a fix title", "s", [{"path": "a", "old_string": "b", "new_string": "c"}])


def test_sre_job_has_no_builtin_tools_only_sre_mcp():
    argv = sre_job.build_argv("claude", "/w/mcp.json", "sys", 40, None)
    assert argv[argv.index("--tools") + 1] == ""
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert allowed and all(t.startswith("mcp__sre__") for t in allowed)
    assert argv[argv.index("--max-turns") + 1] == "40" and "--strict-mcp-config" in argv
    assert "UNTRUSTED" in sre_job.SYSTEM and "INVESTIGATE ONLY" in sre_job.MODE_RULES["alert"]


def test_fix_edits_refuse_placeholders_and_file_wipes():
    app = "function sync() {\n  try { await fetch('/api/sync') } catch (e) { setStatus('Offline'); }\n}\n" + "// filler line\n" * 60
    cur = {"web/app.js": app}
    ok = sre_mcp.apply_edits(cur, [{"path": "web/app.js", "old_string": "catch (e) { setStatus('Offline'); }",
                                    "new_string": "catch (e) { console.error(e); setStatus('Sync error: ' + e.message); }"}], [])  # fmt: skip
    assert "Sync error" in ok["web/app.js"] and ok["web/app.js"].count("filler") == 60
    bad = [
        ([{"path": "web/app.js", "old_string": app, "new_string": "PLACEHOLDER"}], []),  # the houss PR #15 failure
        ([{"path": "web/app.js", "old_string": "// filler line\n" * 60, "new_string": ""}], []),  # wipes most of the file
        ([{"path": "web/app.js", "old_string": "// filler line", "new_string": "x"}], []),  # not unique
        ([{"path": "web/app.js", "old_string": "nope", "new_string": "x"}], []),  # not found
        ([{"path": "web/missing.js", "old_string": "a", "new_string": "b"}], []),
        ([], [{"path": "web/app.js", "content": "overwrite"}]),  # existing file via new_files
        ([], [{"path": "web/new.js", "content": "// ... rest of the file unchanged"}]),
        ([{"path": ".github/workflows/ci.yml", "old_string": "a", "new_string": "b"}], []),
    ]
    for edits, new_files in bad:
        with pytest.raises(sre_mcp.ToolError):
            sre_mcp.apply_edits(cur, edits, new_files)
    assert "preview_fix" in sre_job.TOOLS


def test_fix_pr_without_auto_merge_is_flagged():
    out = parse_result(result_line([{"action": "fix_pr", "url": "https://github.com/kiukairor-org/houss/pull/9", "auto_merge": False}]))
    assert "auto-merge could not be enabled" in findings_markdown("r", out, "owner")
    ok = parse_result(result_line([{"action": "fix_pr", "url": "https://github.com/kiukairor-org/houss/pull/9", "auto_merge": True}]))
    assert "auto-merge could not" not in findings_markdown("r", ok, "owner")
