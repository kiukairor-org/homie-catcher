import asyncio

from catcher.poller import DEPLOY_TIMEOUT_S, Poller, pr_matches_issue, preview_version_url, prod_version_url
from tests.fakes import ORG, click, last_buttons, make_bot, post, spec, triage_mode


def test_rate_limit_queues_beyond_limit():
    async def go():
        bot, svc, claude = await make_bot([spec() for _ in range(3)], CLAUDE_RUNS_PER_HOUR="2")
        triage_mode(bot)
        for i in range(3):
            await bot.handle_post(post(f"work on notes: idea {i}"))
        assert await bot.run_next() and await bot.run_next()
        assert not await bot.run_next()  # limit reached: stays queued
        assert bot.db.queued_count() == 1
        assert any("limit of 2 runs/hour" in p["message"] for p in svc.posts)
        bot.clock.t += 3601
        assert await bot.run_next()
        assert len(claude.prompts) == 3

    asyncio.run(go())


def test_pr_matching():
    assert pr_matches_issue({"body": "Closes #12", "head": {"ref": "x"}}, 12)
    assert not pr_matches_issue({"body": "Closes #123", "head": {"ref": "x"}}, 12)
    assert pr_matches_issue({"body": "", "head": {"ref": "claude/issue-12-20261030"}}, 12)
    assert not pr_matches_issue({"body": None, "head": {"ref": "claude/issue-120-x"}}, 12)
    # cross-repo references are not this repo's issue (agentic-demo#14 mentioned app-template#10)
    assert not pr_matches_issue({"body": "Closes #13. Verified on app-template#10.", "head": {"ref": "fix/x"}}, 10)
    assert not pr_matches_issue({"body": "see kiukairor-org/app-template#10", "head": {"ref": "fix/x"}}, 10)
    assert pr_matches_issue({"body": "Fixes (#10) and more", "head": {"ref": "fix/x"}}, 10)


def test_poller_announces_each_milestone_once():
    async def go():
        bot, svc, claude = await make_bot([spec()])
        triage_mode(bot)
        p = post("work on notes: dark mode")
        await bot.handle_post(p)
        await bot.run_next()
        await bot.handle_action(*click(next(b for b in last_buttons(svc) if b["name"] == "Approve")))
        await bot.drain()
        item = bot.db.latest_item(p["id"])
        n = item["gh_issue_number"]
        svc.pulls["notes"] = [{"number": 7, "body": f"Closes #{n}", "head": {"ref": "claude/x", "sha": "deadbeef"},
                               "html_url": f"https://github.com/{ORG}/notes/pull/7", "state": "open", "labels": [],
                               "merged_at": None}]  # fmt: skip
        poller = Poller(bot)
        await poller.poll_once()
        assert svc.plane_state_name(item["plane_item_id"]) == "In Progress"
        svc.check_runs["deadbeef"] = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        svc.reviews[7] = [
            {"id": 1, "user": {"login": "someone"}, "state": "APPROVED"},
            {"id": 2, "user": {"login": "kiu-homie-reviewer[bot]"}, "state": "APPROVED"},
        ]
        svc.comments[7] = [
            {"user": {"login": "random-person"}, "body": "Preview: https://evil.example"},
            {"user": {"login": "github-actions[bot]"}, "body": "Preview: https://pr-7-notes.tail8e3e5b.ts.net"},
        ]
        svc.pulls["notes"][0]["labels"] = [{"name": "needs-human"}]
        await poller.poll_once()
        await poller.poll_once()  # nothing new the second time
        msgs = [x["message"] for x in svc.posts if x.get("root_id") == p["id"]]
        joined = "\n".join(msgs)
        assert joined.count("PR #7 opened") == 1
        assert joined.count("CI `success`") == 1
        assert joined.count("Reviewer approved") == 1
        assert joined.count("@kiukairor PR #7 needs a human") == 1
        assert "https://pr-7-notes.tail8e3e5b.ts.net" in joined and "evil.example" not in joined
        assert svc.plane_state_name(item["plane_item_id"]) == "In review"
        # milestones are mirrored to #agent-log
        log_msgs = [x["message"] for x in svc.posts if x["channel_id"] == bot.agent_log_channel_id]
        assert any(m.startswith(f"[notes#{n}] 🔧 PR #7 opened") for m in log_msgs)
        # preview: announced once the preview namespace serves the PR head commit
        svc.pulls["notes"][0]["labels"] = [{"name": "preview"}]
        await poller.poll_once()
        assert "Preview ready for PR #7 (deadbee)" not in "\n".join(x["message"] for x in svc.posts)
        svc.app_versions[preview_version_url("notes", 7)] = "deadbeef"
        await poller.poll_once()
        await poller.poll_once()
        joined = "\n".join(x["message"] for x in svc.posts if x.get("root_id") == p["id"])
        assert joined.count("Preview ready for PR #7 (deadbee): https://pr-7-notes.tail8e3e5b.ts.net") == 1
        # merged: Plane Done at once; "deployed" only when prod serves the merge commit
        svc.pulls["notes"][0].update(state="closed", merged_at="2026-10-30T12:00:00Z", merge_commit_sha="cafef00d")
        bot.clock.t = 1_793_361_600  # 2026-10-30T12:00:00Z
        await poller.poll_once()
        assert svc.plane_state_name(item["plane_item_id"]) == "Done"
        assert bot.db.latest_item(p["id"])["status"] == "approved"  # still tracked, waiting for deploy
        svc.app_versions[prod_version_url("notes")] = "cafef00d"
        await poller.poll_once()
        await poller.poll_once()
        joined = "\n".join(x["message"] for x in svc.posts if x.get("root_id") == p["id"])
        assert joined.count("PR #7 merged") == 1
        assert joined.count("PR #7 deployed to prod (cafef00): https://notes.tail8e3e5b.ts.net") == 1
        assert bot.db.latest_item(p["id"])["status"] == "done"
        assert bot.db.tracked_items() == []

    asyncio.run(go())


def test_poller_respects_rate_limit():
    async def go():
        bot, svc, claude = await make_bot([])
        triage_mode(bot)
        bot.gh.rate_remaining, bot.gh.rate_reset = 10, bot.clock.t + 600
        assert bot.gh.rate_limited()
        bot.clock.t += 601
        assert not bot.gh.rate_limited()

    asyncio.run(go())


def test_poller_deploy_timeout_warns_owner():
    async def go():
        bot, svc, claude = await make_bot([spec()])
        triage_mode(bot)
        p = post("work on notes: dark mode")
        await bot.handle_post(p)
        await bot.run_next()
        await bot.handle_action(*click(next(b for b in last_buttons(svc) if b["name"] == "Approve")))
        await bot.drain()
        n = bot.db.latest_item(p["id"])["gh_issue_number"]
        svc.pulls["notes"] = [{"number": 8, "body": f"Closes #{n}", "head": {"ref": "claude/x", "sha": "abc1234"},
                               "html_url": "u", "state": "closed", "labels": [], "merged_at": "2026-10-30T12:00:00Z",
                               "merge_commit_sha": "feedbeef"}]  # fmt: skip
        svc.app_versions[prod_version_url("notes")] = "0ldversion"
        bot.clock.t = 1_793_361_600 + DEPLOY_TIMEOUT_S + 1
        poller = Poller(bot)
        await poller.poll_once()
        await poller.poll_once()
        joined = "\n".join(x["message"] for x in svc.posts if x.get("root_id") == p["id"])
        assert joined.count("PR #8 merged but prod is not serving feedbee") == 1
        assert "running: 0ldversion" in joined and "deployed to prod" not in joined
        assert bot.db.tracked_items() == []

    asyncio.run(go())
