import asyncio
import json

from catcher.brainstorm import MAX_OWNER_TURNS, Brainstorm
from catcher.github import EndpointNotAllowed
from tests.fakes import BRAINSTORM, IDEAS, ORG, STRANGER, FakeBrainstormRunner, click, make_bot, post

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


def test_owner_only_history_images_and_promote_offer():
    async def go():
        bot, svc, claude, runner = await setup([
            {"reply": "Here's an idea, @channel: use **NdM**.", "ready_to_build": False},
            {"reply": "Looks solid.", "ready_to_build": True},
            {"reply": "Still fine.", "ready_to_build": True},
        ])  # fmt: skip
        # a stranger is ignored entirely
        await bot.handle_post(post("hi", user=STRANGER, channel=BRAINSTORM))
        assert runner.runs == []
        svc.files["f1"] = {"info": {"id": "f1", "name": "sketch.png", "mime_type": "image/png", "size": 4}, "data": b"\x89PNG"}
        root = post("dice notation for dice-service?", channel=BRAINSTORM, file_ids=["f1"])
        await bot.handle_post(root)
        r0 = runner.runs[0]
        assert r0["images"] == [("image/png", "iVBORw==")]
        assert "dice notation for dice-service?" in r0["text"] and "sketch.png (image, attached)" in r0["text"]
        assert r0["max_turns"] == 10 and r0["github_token"].startswith("ghs_")
        assert "release" not in r0["repos"] and "papl" not in r0["repos"]
        reply = thread_posts(svc, root["id"])[-1]
        assert "@​channel" in reply["message"] and "**NdM**" in reply["message"]  # markdown kept, mention defanged
        assert not buttons_of(reply)
        # second turn carries the history; ready_to_build -> one Promote offer, only once
        await bot.handle_post(post("ok what about 3d6", channel=BRAINSTORM, root=root["id"]))
        assert "Here's an idea" in runner.runs[1]["text"] and "dice notation" in runner.runs[1]["text"]
        assert [b["name"] for b in buttons_of(thread_posts(svc, root["id"])[-1])] == ["Promote"]
        await bot.handle_post(post("and 2d20?", channel=BRAINSTORM, root=root["id"]))
        assert not buttons_of(thread_posts(svc, root["id"])[-1])
        assert len(bot.brainstorm.messages(root["id"])) == 6

    asyncio.run(go())


def test_promote_edit_send_creates_ideas_item_without_spec_run():
    async def go():
        v2 = dict(DRAFT, out_of_scope=["Modifiers like +2", "More than 20 dice"])
        bot, svc, claude, runner = await setup([{"reply": "Sure.", "ready_to_build": True}, DRAFT, v2])
        root = post("multi-dice rolls", channel=BRAINSTORM)
        await bot.handle_post(root)
        rid = root["id"]
        await bot.handle_post(post("promote", channel=BRAINSTORM, root=rid))
        d1 = next(p for p in thread_posts(svc, rid) if p["message"].startswith("**Feature request draft v1**"))
        assert (
            "**Target app:** `notes`" in d1["message"]
            and "**Brainstorm thread:** https://chat.tail8e3e5b.ts.net/homie/pl/" + rid in d1["message"]
        )
        btn = {b["name"]: b for b in buttons_of(d1)}
        assert list(btn) == ["Send to #ideas", "Edit", "Cancel"]
        # only the owner may press them
        assert await bot.handle_action(*click(btn["Send to #ideas"], user=STRANGER)) == {"ephemeral_text": "not allowed"}
        # Edit -> corrections -> v2; v1 retired and its buttons refused
        assert (await bot.handle_action(*click(btn["Edit"])))["ephemeral_text"].startswith("Waiting")
        await bot.drain()
        await bot.handle_post(post("cap N at 20", channel=BRAINSTORM, root=rid))
        assert "<corrections>\ncap N at 20\n</corrections>" in runner.runs[2]["text"] and "Previous draft" in runner.runs[2]["text"]
        d2 = next(p for p in thread_posts(svc, rid) if p["message"].startswith("**Feature request draft v2**"))
        assert "More than 20 dice" in d2["message"]
        assert d1["message"] == "_Superseded by a newer draft._" and d1["props"]["attachments"] == []
        assert "superseded" in (await bot.handle_action(*click(btn["Send to #ideas"])))["ephemeral_text"]
        assert not [p for p in svc.posts if p["channel_id"] == IDEAS]
        # Send v2 -> #ideas root + normal triage (Plane + Approve/Reject), no Claude spec run, no GitHub issue
        send = {b["name"]: b for b in buttons_of(d2)}["Send to #ideas"]
        assert (await bot.handle_action(*click(send)))["ephemeral_text"].startswith("Sending")
        await bot.drain()
        ideas_root = next(p for p in svc.posts if p["channel_id"] == IDEAS and not p.get("root_id"))
        assert ideas_root["message"].startswith("💡 **Feature request from brainstorm by kiukairor**")
        triage = [p for p in svc.posts if p.get("root_id") == ideas_root["id"]]
        assert [b["name"] for b in buttons_of(triage[-1])] == ["Approve", "Reject"]
        assert claude.prompts == [] and not svc.issues
        (wi,) = svc.plane_items.values()
        assert wi["name"] == "Roll multiple dice (NdM)" and "Out of scope" in wi["description_html"]
        assert svc.plane_state_name(wi["id"]) == "Needs triage"
        assert bot.brainstorm.thread(rid)["status"] == "sent"
        assert "✅ Sent to #ideas" in thread_posts(svc, rid)[-1]["message"]
        # pressing Send again does nothing
        assert (await bot.handle_action(*click(send)))["ephemeral_text"] == "Already handled."

    asyncio.run(go())


def test_bad_target_disables_send_and_cancel_returns_to_chat():
    async def go():
        bad = dict(DRAFT, target={"kind": "existing", "repo": "release"})
        bot, svc, claude, runner = await setup([{"reply": "ok", "ready_to_build": False}, bad])
        root = post("idea", channel=BRAINSTORM)
        await bot.handle_post(root)
        await bot.handle_post(post("promote", channel=BRAINSTORM, root=root["id"]))
        d1 = [p for p in thread_posts(svc, root["id"]) if "draft v1" in p["message"]][0]
        assert [b["name"] for b in buttons_of(d1)] == ["Edit", "Cancel"] and "not an org app repo" in d1["message"]
        cancel = buttons_of(d1)[1]
        await bot.handle_action(*click(cancel))
        await bot.drain()
        assert bot.brainstorm.thread(root["id"])["status"] == "chat"

    asyncio.run(go())


def test_thread_turn_cap_and_ideas_untouched_by_brainstorm_posts():
    async def go():
        bot, svc, claude, runner = await setup([{"reply": "r", "ready_to_build": False}] * MAX_OWNER_TURNS)
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
