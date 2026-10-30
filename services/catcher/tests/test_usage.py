import asyncio
import json
from datetime import datetime, timezone

from catcher.brainstorm import Brainstorm
from catcher.usage import NONESSENTIAL, detect_limit
from tests.fakes import AGENT_LOG, BRAINSTORM, IDEAS, FakeBrainstormRunner, make_bot, post, spec


def res(cost, **kw):
    return {"total_cost_usd": cost, "duration_ms": 1000, "num_turns": 3, "is_error": False,
            "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 1}, **kw}  # fmt: skip


def log_msgs(svc):
    return [p["message"] for p in svc.posts if p["channel_id"] == AGENT_LOG]


def test_meter_thresholds_pause_and_status_issue():
    async def go():
        bot, svc, claude = await make_bot([spec()])
        u = bot.usage
        u.put("daily_usd", 1.0)
        u.record("brainstorm", res(0.5), ok=True)
        await bot.drain()
        assert u.state()[0] == "ok" and not [m for m in log_msgs(svc) if "Claude usage at" in m]
        u.record("spec", res(0.35), ok=True)
        await bot.drain()
        warns = [m for m in log_msgs(svc) if "Claude usage at" in m]
        assert len(warns) == 1 and "85%" in warns[0] and "daily" in warns[0]
        u.record("promote", res(0.2), ok=True)  # 105%
        await bot.drain()
        assert u.state()[0] == "paused" and "daily budget reached" in u.state()[1]
        assert len([m for m in log_msgs(svc) if "budget reached" in m]) == 1
        assert not u.allow("brainstorm") and not u.allow("spec") and u.allow("review") and u.allow("agent_run")
        assert [r[0] for r in u.breakdown("day")] == ["brainstorm", "spec", "promote"]
        # status issue for the workflows (homie-catcher, label budget-status, marker)
        (issue,) = [i for i in svc.issues if i["repo"] == "homie-catcher"]
        assert issue["labels"] == ["budget-status"]
        body = svc.issue_bodies[("homie-catcher", issue["number"])]
        assert json.loads(body.split("<!-- homie-budget ")[1].split(" -->")[0])["state"] == "paused"
        # spec runs wait (queued, no Claude), with one notice
        p = post("work on notes: dark mode")
        await bot.handle_post(p)
        assert not await bot.run_next() and not await bot.run_next()
        assert claude.prompts == []
        assert len([x for x in svc.posts if x.get("root_id") == p["id"] and "Claude work is **paused**" in x["message"]]) == 1
        # owner override lets it run
        await bot.handle_post(post("budget override 1h", channel=IDEAS))
        assert u.state()[0] == "ok"
        assert await bot.run_next() and len(claude.prompts) == 1

    asyncio.run(go())


def test_brainstorm_refuses_while_paused():
    async def go():
        bot, svc, _ = await make_bot([])
        runner = FakeBrainstormRunner([{"reply": "hi", "ready_to_build": False}])
        bot.brainstorm = Brainstorm(bot, runner)
        await bot.startup()
        await bot.handle_post(post("budget pause", channel=AGENT_LOG))
        root = post("idea", channel=BRAINSTORM)
        await bot.handle_post(root)
        assert runner.runs == []
        assert "Claude work is **paused**" in [p for p in svc.posts if p.get("root_id") == root["id"]][-1]["message"]
        await bot.handle_post(post("budget resume", channel=AGENT_LOG))
        await bot.handle_post(post("again", channel=BRAINSTORM, root=root["id"]))
        assert len(runner.runs) == 1

    asyncio.run(go())


def test_commands_and_report():
    async def go():
        bot, svc, _ = await make_bot([])
        u = bot.usage
        u.record("brainstorm", res(0.12), ok=True)
        await bot.drain()
        for cmd in ("budget daily 5", "budget weekly 30", "usage", "budget daily 0", "budget override 30h"):
            p = post(cmd, channel=IDEAS)
            await bot.handle_post(p)
        replies = [x["message"] for x in svc.posts if x["channel_id"] == IDEAS and x.get("root_id")]
        assert replies[0].startswith("✅ daily budget set to $5.00") and replies[1].startswith("✅ weekly budget set to $30.00")
        assert "| brainstorm | 1 | 0.12 |" in replies[2] and "of $5.00" in replies[2] and "of $30.00" in replies[2]
        assert "between 0 and 10000" in replies[3] and "1-24 hours" in replies[4]
        assert u.daily == 5.0 and u.weekly == 30.0

    asyncio.run(go())


def test_limit_detection_and_override_does_not_lift_limit():
    now = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc).timestamp()
    assert detect_limit({"is_error": True, "result": "Claude AI usage limit reached|1793390400"}, None, now) == 1793390400.0
    t = detect_limit({"is_error": True, "result": "You've hit your limit · resets 3pm"}, None, now)
    assert datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M") == "15:00"
    assert detect_limit({"api_error_status": 429, "is_error": True, "result": "x"}, None, now) == now + 3600
    assert detect_limit(res(0.1), None, now) is None
    assert detect_limit(None, "everything fine", now) is None

    async def go():
        bot, svc, _ = await make_bot([])
        u = bot.usage
        u.record("brainstorm", {"is_error": True, "api_error_status": 429, "result": "usage limit reached"}, ok=False)
        await bot.drain()
        assert u.state()[0] == "limit"
        assert any("Claude usage limit reached" in m for m in log_msgs(svc))
        await bot.handle_post(post("budget override 1h", channel=IDEAS))
        assert u.state()[0] == "limit"  # override lifts budgets only
        await bot.handle_post(post("budget resume", channel=IDEAS))
        assert u.state()[0] == "ok"
        assert all(f in NONESSENTIAL for f in ("spec", "brainstorm", "channel_chat", "promote"))

    asyncio.run(go())


def test_gha_ingest_skip_retry_and_forgery():
    async def go():
        bot, svc, _ = await make_bot([])
        u = bot.usage
        mark = {"wf": "agent", "run": "1", "cost": 0.8, "in": 1, "out": 2, "cr": 3, "cc": 4, "ms": 5000, "turns": 9, "err": False}
        svc.issues.append({"number": 5, "repo": "notes", "title": "x", "labels": ["agent"]})
        svc.repo_comments["notes"] = [
            {"id": 11, "user": {"login": "kiu-homie-bot[bot]"}, "updated_at": "2026-10-30T12:00:00Z",
             "issue_url": "https://api.github.com/repos/o/notes/issues/5", "body": f"<sub>u</sub>\n<!-- homie-usage {json.dumps(mark)} -->"},
            {"id": 12, "user": {"login": "random"}, "updated_at": "2026-10-30T12:00:01Z",
             "issue_url": "https://api.github.com/repos/o/notes/issues/5", "body": "<!-- homie-usage {\"wf\":\"agent\",\"cost\":999} -->"},
            {"id": 13, "user": {"login": "kiu-homie-bot[bot]"}, "updated_at": "2026-10-30T12:00:02Z",
             "issue_url": "https://api.github.com/repos/o/notes/issues/5", "body": "skipped <!-- homie-budget-skipped -->"},
        ]  # fmt: skip
        assert await u.ingest_gha() == 1
        assert await u.ingest_gha() == 1  # same comment again: counted once (ref unique)
        assert [(r[0], r[1]) for r in u.breakdown("day")] == [("agent_run", 1)]
        assert abs(u.spent("day") - 0.8) < 1e-9
        u.put("paused", True)
        assert await u.retry_skipped() == 0 and not svc.posted_comments
        u.put("paused", False)
        assert await u.retry_skipped() == 1
        (c,) = svc.posted_comments
        assert c["repo"] == "notes" and c["number"] == 5 and c["body"].startswith("@claude")
        assert await u.retry_skipped() == 0  # once only

    asyncio.run(go())
