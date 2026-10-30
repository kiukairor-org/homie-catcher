import asyncio

from catcher import buttons
from tests.fakes import ORG, OWNER, STRANGER, click, last_buttons, make_bot, post, spec, triage_mode


async def _triaged(outputs=None, msg="work on notes: dark mode"):
    bot, svc, claude = await make_bot(outputs or [spec()])
    triage_mode(bot)
    p = post(msg)
    await bot.handle_post(p)
    await bot.run_next()
    return bot, svc, p


def _btn(svc, label):
    return next(b for b in last_buttons(svc) if b["name"] == label)


def test_buttons_point_at_internal_url_and_are_signed():
    async def go():
        bot, svc, p = await _triaged()
        b = _btn(svc, "Approve")
        assert b["integration"]["url"] == "http://catcher.catcher.svc.cluster.local:8080/actions/approve"
        ctx = b["integration"]["context"]
        assert buttons.verify(bot.cfg.action_secret, "approve", ctx) == ctx
        assert b["id"].isalnum()

    asyncio.run(go())


def test_wrong_user_is_refused_and_nothing_happens():
    async def go():
        bot, svc, p = await _triaged()
        name, payload = click(_btn(svc, "Approve"), user=STRANGER)
        before = len(svc.calls)
        assert await bot.handle_action(name, payload) == {"ephemeral_text": "not allowed"}
        await bot.drain()
        assert len(svc.calls) == before
        assert bot.db.latest_item(p["id"])["status"] == "triage"

    asyncio.run(go())


def test_bad_hmac_is_refused():
    async def go():
        bot, svc, p = await _triaged()
        name, payload = click(_btn(svc, "Approve"))
        payload["context"]["item"] = payload["context"]["item"] + 1  # tamper
        assert await bot.handle_action(name, payload) == {"ephemeral_text": "not allowed"}
        name, payload = click(_btn(svc, "Approve"))
        payload["context"]["sig"] = "0" * 64
        assert await bot.handle_action(name, payload) == {"ephemeral_text": "not allowed"}
        name, payload = click(_btn(svc, "Approve"))
        del payload["context"]["sig"]
        assert await bot.handle_action(name, payload) == {"ephemeral_text": "not allowed"}
        # a valid reject context replayed at the approve endpoint
        _, reject_payload = click(_btn(svc, "Reject"))
        assert await bot.handle_action("approve", reject_payload) == {"ephemeral_text": "not allowed"}
        assert await bot.handle_action("delete_everything", reject_payload) == {"ephemeral_text": "not allowed"}
        await bot.drain()
        assert bot.db.latest_item(p["id"])["status"] == "triage"
        assert not svc.writes("github")

    asyncio.run(go())


def test_approve_creates_issue_once_replay_is_idempotent():
    async def go():
        bot, svc, p = await _triaged()
        btn = _btn(svc, "Approve")
        res = await bot.handle_action(*click(btn))
        await bot.drain()
        assert "Approved" in res["ephemeral_text"]
        assert len(svc.issues) == 1
        issue = svc.issues[0]
        assert issue["repo"] == "notes" and issue["labels"] == ["agent"]
        item = bot.db.latest_item(p["id"])
        assert f"<!-- homie-catcher thread={p['id']} plane={item['plane_item_id']} -->" in issue["body"]
        assert f"https://plane.tail8e3e5b.ts.net/homie/projects/{item['plane_project_id']}/issues/{item['plane_item_id']}" in issue["body"]
        assert svc.plane_state_name(item["plane_item_id"]) == "Ready for agent"
        assert item["status"] == "approved"
        # second click (replay) does nothing
        res2 = await bot.handle_action(*click(btn))
        await bot.drain()
        assert res2 == {"ephemeral_text": "Already handled."}
        assert len(svc.issues) == 1

    asyncio.run(go())


def test_reject_sets_plane_rejected_and_no_github_write():
    async def go():
        bot, svc, p = await _triaged()
        await bot.handle_action(*click(_btn(svc, "Reject")))
        await bot.drain()
        item = bot.db.latest_item(p["id"])
        assert item["status"] == "rejected"
        assert svc.plane_state_name(item["plane_item_id"]) == "Rejected"
        assert not svc.writes("github")
        assert "Rejected" in svc.posts[-1]["message"]
        # approve after reject: nothing
        assert (await bot.handle_action(*click(_btn(svc, "Approve")))) == {"ephemeral_text": "Already handled."}

    asyncio.run(go())


def test_new_repo_needs_its_own_approve():
    async def go():
        bot, svc, p = await _triaged([spec(new_repo={"name": "recipe-box", "description": "Recipes"})], "new idea: recipes")
        await bot.handle_action(*click(_btn(svc, "Approve")))
        await bot.drain()
        # no repo yet: a separate confirmation is shown
        assert "recipe-box" not in svc.repos
        assert not [c for c in svc.writes("github") if c.path.endswith("/generate")]
        assert "Create repo `kiukairor-org/recipe-box`" in svc.posts[-1]["props"]["attachments"][0]["text"]
        repo_btn = _btn(svc, "Approve repo")
        # a stranger can't approve the repo
        assert (await bot.handle_action(*click(repo_btn, user=STRANGER))) == {"ephemeral_text": "not allowed"}
        await bot.handle_action(*click(repo_btn))
        await bot.drain()
        writes = [(c.method, c.path) for c in svc.writes("github") if not c.path.endswith("/access_tokens")]
        assert writes[0] == ("POST", f"/repos/{ORG}/app-template/generate")
        gen = next(c for c in svc.calls if c.path.endswith("/generate"))
        assert gen.json == {"owner": ORG, "name": "recipe-box", "description": "Recipes", "private": False, "include_all_branches": False}
        assert ("PATCH", f"/repos/{ORG}/recipe-box") in writes
        assert ("PUT", f"/repos/{ORG}/recipe-box/branches/main/protection") in writes
        assert svc.gh_labels["recipe-box"] == {"agent", "needs-human", "hold", "preview", "fix-round-1", "fix-round-2", "build-v1"}
        assert svc.issues[0]["repo"] == "recipe-box" and svc.issues[0]["title"].startswith("Build v1")
        assert svc.issues[0]["labels"] == ["build-v1", "agent"]  # D69: v1 marker for the reviewer size limit
        assert "Build v1 of this app" in svc.issues[0]["body"]
        assert bot.db.thread(p["id"])["repo"] == "recipe-box"
        # replay of the repo approve does nothing
        assert (await bot.handle_action(*click(repo_btn))) == {"ephemeral_text": "Already handled."}
        await bot.drain()
        assert len([c for c in svc.calls if c.path.endswith("/generate")]) == 1

    asyncio.run(go())


def test_reject_repo():
    async def go():
        bot, svc, p = await _triaged([spec(new_repo={"name": "recipe-box", "description": "Recipes"})], "new idea: recipes")
        await bot.handle_action(*click(_btn(svc, "Approve")))
        await bot.drain()
        await bot.handle_action(*click(_btn(svc, "Reject")))
        await bot.drain()
        assert bot.db.latest_item(p["id"])["status"] == "rejected"
        assert not svc.writes("github")

    asyncio.run(go())


def test_owner_id_check_uses_configured_list():
    async def go():
        bot, svc, p = await _triaged()
        assert OWNER in bot.cfg.owner_user_ids and STRANGER not in bot.cfg.owner_user_ids

    asyncio.run(go())
