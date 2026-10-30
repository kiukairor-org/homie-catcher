import asyncio
import base64
import json

from catcher.brainstorm import MAX_OWNER_TURNS, Brainstorm
from catcher.github import EndpointNotAllowed
from tests.fakes import BRAINSTORM, IDEAS, ORG, STRANGER, FakeBrainstormRunner, make_bot, post

DRAFT = {
    "title": "Roll multiple dice (NdM)",
    "target": {"kind": "existing", "repo": "notes"},
    "problem": "Players need 3d6-style rolls; today only one die per request.",
    "build": ["Accept dice=NdM on GET /roll", "Return each die and the total", "Keep GET /roll?sides=N working"],
    "acceptance_criteria": ["GET /roll?dice=3d6 returns 3 results between 1 and 6 and their sum", "Invalid notation returns 400"],
    "out_of_scope": ["Modifiers like +2"],
}


async def setup(outputs):
    bot, svc, claude = await make_bot([])
    runner = FakeBrainstormRunner(outputs)
    bot.brainstorm = Brainstorm(bot, runner)
    await bot.startup()
    return bot, svc, claude, runner


def thread_posts(svc, root):
    return [p for p in svc.posts if p.get("root_id") == root]


def buttons_of(p):
    atts = (p.get("props") or {}).get("attachments") or []
    return atts[0].get("actions", []) if atts else []


def ans(reply):
    return {"reply": reply, "intent": "answer", "request": None}


def test_owner_only_history_images_and_plain_answers():
    async def go():
        bot, svc, claude, runner = await setup([ans("Here's an idea, @channel: use **NdM**."), ans("Looks solid."), ans("Still fine.")])
        # a stranger is ignored entirely
        await bot.handle_post(post("hi", user=STRANGER, channel=BRAINSTORM))
        assert runner.runs == []
        png = b"\x89PNG\r\n\x1a\n" + b"x" * 8 + b"IEND\xaeB`\x82"
        svc.files["f1"] = {"info": {"id": "f1", "name": "sketch.png", "mime_type": "image/png", "size": len(png)}, "data": png}
        svc.files["f2"] = {"info": {"id": "f2", "name": "cut.png", "mime_type": "image/png", "size": 9}, "data": png[:9]}
        root = post("dice notation for dice-service?", channel=BRAINSTORM, file_ids=["f1", "f2"])
        await bot.handle_post(root)
        r0 = runner.runs[0]
        assert r0["images"] == [("image/png", base64.b64encode(png).decode())]
        assert "cut.png (unreadable image, skipped)" in r0["text"]
        assert "dice notation for dice-service?" in r0["text"] and "sketch.png (image, attached)" in r0["text"]
        assert r0["max_turns"] == 10 and r0["github_token"].startswith("ghs_")
        assert "release" not in r0["repos"] and "papl" not in r0["repos"]
        reply = thread_posts(svc, root["id"])[-1]
        assert "@​channel" in reply["message"] and "**NdM**" in reply["message"]  # markdown kept, mention defanged
        # questions get plain answers: no buttons, no drafts, nothing started
        await bot.handle_post(post("ok what about 3d6", channel=BRAINSTORM, root=root["id"]))
        assert "Here's an idea" in runner.runs[1]["text"] and "dice notation" in runner.runs[1]["text"]
        await bot.handle_post(post("and 2d20?", channel=BRAINSTORM, root=root["id"]))
        assert not any(buttons_of(p) for p in thread_posts(svc, root["id"]))
        assert len(bot.brainstorm.messages(root["id"])) == 6
        assert not svc.issues and not svc.plane_items and claude.prompts == []

    asyncio.run(go())


def test_owner_order_starts_issue_immediately_with_proof_link():
    async def go():
        bot, svc, claude, runner = await setup([{"reply": "Adding NdM rolls to notes.", "intent": "order", "request": DRAFT}])
        root = post("add multi-dice rolls to notes", channel=BRAINSTORM)
        await bot.handle_post(root)
        await bot.drain()
        # issue created at once: no draft, no Send button, no Approve in #ideas, no Claude spec run
        (issue,) = svc.issues
        assert issue["repo"] == "notes" and issue["title"] == "Roll multiple dice (NdM)" and issue["labels"] == ["agent"]
        assert claude.prompts == []
        ideas_root = next(p for p in svc.posts if p["channel_id"] == IDEAS and not p.get("root_id"))
        assert ideas_root["message"].startswith("💡 **Owner order from #brainstorm**")
        assert not any(b["name"] in ("Approve", "Send to #ideas", "Send to agent") for p in svc.posts for b in buttons_of(p))
        (wi,) = svc.plane_items.values()
        assert svc.plane_state_name(wi["id"]) == "Ready for agent"
        last = thread_posts(svc, root["id"])[-1]["message"]
        assert last == f"✅ Started: GitHub issue in `notes`: {issue['html_url']}"
        th = bot.brainstorm.thread(root["id"])
        assert th["handoff_url"] == issue["html_url"] and th["status"] == "sent"

    asyncio.run(go())


def test_ambiguous_asks_once_yes_starts_no_does_nothing():
    async def go():
        bot, svc, claude, runner = await setup([
            {"reply": "That could work.", "intent": "ambiguous", "request": DRAFT},
            {"reply": "Maybe later then.", "intent": "ambiguous", "request": DRAFT},
            ans("Sure, here's how dice work."),
        ])  # fmt: skip
        root = post("maybe notes could roll dice?", channel=BRAINSTORM)
        await bot.handle_post(root)
        rid = root["id"]
        assert thread_posts(svc, rid)[-1]["message"].endswith("**Want me to start this now?**")
        assert not svc.issues and bot.brainstorm.thread(rid)["status"] == "confirm"
        await bot.handle_post(post("yes please", channel=BRAINSTORM, root=rid))
        await bot.drain()
        assert len(runner.runs) == 1 and len(svc.issues) == 1  # the yes is the order: no extra Claude run
        assert thread_posts(svc, rid)[-1]["message"].startswith("✅ Started: GitHub issue in `notes`")
        # a second ambiguous idea, then something other than yes: nothing is started, the chat continues
        root2 = post("what if notes had dice?", channel=BRAINSTORM)
        await bot.handle_post(root2)
        await bot.handle_post(post("how do dice notations work?", channel=BRAINSTORM, root=root2["id"]))
        assert len(svc.issues) == 1 and len(runner.runs) == 3 and bot.brainstorm.thread(root2["id"])["status"] == "chat"

    asyncio.run(go())


def test_order_with_invalid_target_reports_nothing_sent():
    async def go():
        bad = dict(DRAFT, target={"kind": "existing", "repo": "release"})
        bot, svc, claude, runner = await setup([{"reply": "Doing it.", "intent": "order", "request": bad}])
        root = post("add dice to release", channel=BRAINSTORM)
        await bot.handle_post(root)
        assert not svc.issues
        assert "⚠️ Could not start it" in thread_posts(svc, root["id"])[-1]["message"]
        assert "Nothing has been sent to the agent" in thread_posts(svc, root["id"])[-1]["message"]

    asyncio.run(go())


def test_truthfulness_guard_phrasings_and_reply():
    from catcher.brainstorm import implies_handoff

    claims = [
        "I'm sending your feedback to the agent now.", "I've forwarded this to the agent.",
        "Done — I've created an issue for the redesign.", "Your feedback has been sent to the agent.",
        "I've queued the work.", "The agent is on it.", "I'll pass this on to the agent.", "Sending work to the agent!",
        "I opened a GitHub issue for this.", "Ticket created in Plane.", "OK, I will tell the agent to use design skills.",
        "Great, the redesign is on its way.", "I went ahead and filed a ticket.", "We have started the fix.",
    ]  # fmt: skip
    honest = [
        "I can't send anything to the agent myself.", "Only you can send this, with the button.", "Nothing has been sent yet.",
        "Want me to start this now?", "I didn't create an issue.", "I read web/index.html and compared it with your screenshot.",
        "The agent will need a clear scope.", "Issue #1 and PR #2 are closed.", "PR #2 was escalated to a human (1112 lines).",
        "I created a short list of options below.", "I can help tighten the request.", "Shall I open an issue for it?",
        "I started by reading the README.", "You sent no question, so tell me if you meant something else.",
    ]  # fmt: skip
    assert [c for c in claims if not implies_handoff(c)] == []
    assert [h for h in honest if implies_handoff(h)] == []

    async def go():
        bot, svc, claude, runner = await setup([ans("Got it. I've forwarded your feedback to the agent.")])
        root = post("it is ugly", channel=BRAINSTORM)
        await bot.handle_post(root)
        msg = thread_posts(svc, root["id"])[-1]["message"]
        assert msg.endswith("⚠️ Nothing has been sent to the agent from this message.") and not svc.issues

    asyncio.run(go())


def test_thread_turn_cap_and_ideas_untouched_by_brainstorm_posts():
    async def go():
        bot, svc, claude, runner = await setup([ans("r")] * MAX_OWNER_TURNS)
        root = post("t0", channel=BRAINSTORM)
        await bot.handle_post(root)
        for i in range(1, MAX_OWNER_TURNS + 1):
            await bot.handle_post(post(f"t{i}", channel=BRAINSTORM, root=root["id"]))
        assert len(runner.runs) == MAX_OWNER_TURNS
        assert "reached its limit" in thread_posts(svc, root["id"])[-1]["message"]
        assert claude.prompts == [] and not [p for p in svc.posts if p["channel_id"] == IDEAS]

    asyncio.run(go())


def test_readonly_token_guard():
    async def go():
        bot, svc, claude, runner = await setup([])
        gh = bot.gh
        tok = await gh.readonly_token(["notes", "release", "agentic-demo"])
        assert tok.startswith("ghs_")
        body = next(c.json for c in svc.calls if c.path.endswith("/access_tokens") and c.json)
        assert body["repositories"] == ["agentic-demo", "notes"]
        assert set(body["permissions"].values()) == {"read"}
        path = f"/app/installations/{gh.installation_id}/access_tokens"
        for bad in (
            {"repositories": ["notes"], "permissions": {"contents": "write"}},
            {"repositories": ["papl"], "permissions": {"contents": "read"}},
            {"repositories": ["notes"], "permissions": {"administration": "read"}},
            {"repositories": ["notes"], "permissions": {"contents": "read"}, "repository_ids": [1]},
            ["notes"],
        ):
            try:
                gh.check_allowed("POST", path, bad)
            except EndpointNotAllowed:
                continue
            raise AssertionError(f"allowed: {json.dumps(bad)}")
        assert ORG

    asyncio.run(go())


def test_read_capped_reads_all_chunks_and_caps():
    from catcher.transport import HttpError, read_capped

    class Stream:  # returns small chunks, like aiohttp's StreamReader
        def __init__(self, data):
            self.data = data

        async def read(self, n):
            chunk, self.data = self.data[:7], self.data[7:]
            return chunk

    async def go():
        assert await read_capped(Stream(b"x" * 100), 1000) == b"x" * 100
        try:
            await read_capped(Stream(b"x" * 100), 50)
        except HttpError as exc:
            assert exc.status == 413
        else:
            raise AssertionError("not capped")

    asyncio.run(go())
