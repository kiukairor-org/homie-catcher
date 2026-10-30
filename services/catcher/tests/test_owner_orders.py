"""D73: the owner's own instruction is the trigger (no Approve); unclear intent asks once; feedback on an
open agent PR becomes an @claude instruction; a still-pending item is reused, not duplicated."""

import asyncio
import json

from catcher.brainstorm import Brainstorm
from tests.fakes import FakeBrainstormRunner, make_bot, post, spec


def thread(svc, root):
    return [p for p in svc.posts if p.get("root_id") == root]


def has_buttons(svc):
    return any((p.get("props") or {}).get("attachments") for p in svc.posts)


def test_ideas_order_starts_at_once():
    async def go():
        bot, svc, claude = await make_bot([spec(intent="order")])
        p = post("work on notes: add dark mode")
        await bot.handle_post(p)
        await bot.run_next()
        (issue,) = svc.issues
        assert issue["repo"] == "notes" and not has_buttons(svc)
        assert any(m["message"].startswith("📋 GitHub issue opened: " + issue["html_url"]) for m in thread(svc, p["id"]))
        assert bot.db.latest_item(p["id"])["status"] == "approved"

    asyncio.run(go())


def test_ideas_ambiguous_asks_yes_starts_and_no_cancels():
    async def go():
        bot, svc, claude = await make_bot([spec(intent="ambiguous"), spec(intent="ambiguous", title="Other")])
        p = post("work on notes: maybe dark mode?")
        await bot.handle_post(p)
        await bot.run_next()
        assert thread(svc, p["id"])[-1]["message"].endswith("**Want me to start this now?** Reply `yes` to start it.")
        assert not svc.issues and not svc.plane_items and not has_buttons(svc)
        await bot.handle_post(post("yes", root=p["id"]))
        assert len(svc.issues) == 1 and bot.db.latest_item(p["id"])["status"] == "approved"
        q = post("work on notes: maybe a logo?")
        await bot.handle_post(q)
        await bot.run_next()
        await bot.handle_post(post("no", root=q["id"]))
        assert len(svc.issues) == 1 and bot.db.latest_item(q["id"])["status"] == "rejected"
        assert "OK, not started. Nothing was sent to GitHub." in thread(svc, q["id"])[-1]["message"]

    asyncio.run(go())


def test_new_app_order_creates_repo_and_v1_issue_without_approvals():
    async def go():
        bot, svc, claude = await make_bot([spec(intent="order", new_repo={"name": "recipe-box", "description": "Recipes"})])
        await bot.handle_post(post("new idea: an app for recipes"))
        await bot.run_next()
        assert "recipe-box" in svc.repos and not has_buttons(svc)
        (issue,) = svc.issues
        assert issue["repo"] == "recipe-box" and issue["labels"] == ["build-v1", "agent"]

    asyncio.run(go())


def request(**over):
    base = {"title": "Restyle the list", "target": {"kind": "existing", "repo": "notes"}, "problem": "The owner says it is ugly.",
            "build": ["palette", "spacing", "cards"], "acceptance_criteria": ["looks right at 390px", "tests pass"],
            "out_of_scope": [], "related_pr": None}  # fmt: skip
    base.update(over)
    return base


async def chat_bot(outputs, **cfg):
    bot, svc, claude = await make_bot([], APP_CHANNELS="true", **cfg)
    bot.brainstorm = Brainstorm(bot, FakeBrainstormRunner(outputs))
    await bot.startup()
    await bot.channels.reconcile(force=True)
    return bot, svc, bot.channels.channel_for_repo("notes")


def test_feedback_on_open_agent_pr_becomes_claude_instruction():
    async def go():
        bot, svc, ch = await chat_bot([{"reply": "Restyling the list in PR #7.", "intent": "order", "request": request(related_pr=7)},
                                       {"reply": "Doing it.", "intent": "order", "request": request(related_pr=8)}])  # fmt: skip
        svc.pulls["notes"] = [{"number": 7, "state": "open", "user": {"login": "kiu-homie-bot[bot]"}},
                              {"number": 8, "state": "open", "user": {"login": "someone"}}]  # fmt: skip
        root = post("make it prettier", channel=ch)
        await bot.handle_post(root)
        (cm,) = svc.posted_comments
        assert cm["repo"] == "notes" and cm["number"] == 7 and cm["body"].startswith("@claude The owner asked for this change")
        assert not svc.issues
        assert thread(svc, root["id"])[-1]["message"] == f"✅ Started: @claude instruction on PR #7: {cm['html_url']}"
        # a PR the agent did not open is never instructed: a new issue instead
        root2 = post("and bigger buttons", channel=ch)
        await bot.handle_post(root2)
        assert len(svc.posted_comments) == 1 and [i["repo"] for i in svc.issues] == ["notes"]

    asyncio.run(go())


def test_pending_triage_item_of_the_thread_is_reused():
    async def go():
        bot, svc, ch = await chat_bot([{"reply": "Starting the redesign.", "intent": "order", "request": request()}])
        root = post("it is ugly", channel=ch)
        # an older Send left an item waiting in triage (pre-D73 flow)
        old_root = (await bot.mm.post(ch, "old request"))["id"]
        bot.db.upsert_thread(old_root, ch)
        bot.db.set_thread_repo(old_root, "notes", False)
        item_id = bot.db.create_item(old_root, "old", "running")
        bot.db.update_item(item_id, repo="notes", spec_json=json.dumps(spec()))
        from catcher.spec import validate_spec

        await bot.create_triage(item_id, validate_spec(spec()), "notes", "running")
        bot.brainstorm._ensure_thread(root["id"], ch, "notes")
        bot.brainstorm._set(root["id"], ideas_post_id=old_root)
        await bot.handle_post(root)
        (issue,) = svc.issues
        assert len(svc.plane_items) == 1 and issue["title"] == "Restyle the list"
        assert bot.db.item(item_id)["status"] == "approved" and bot.db.item(item_id)["gh_issue_url"] == issue["html_url"]

    asyncio.run(go())
