import asyncio

from catcher.brainstorm import Brainstorm
from tests.fakes import IDEAS, ORG, OWNER, STRANGER, FakeBrainstormRunner, click, last_buttons, make_bot, post, spec


def thread(svc, root):
    return [p for p in svc.posts if p.get("root_id") == root]


async def admin_bot(**cfg):
    bot, svc, claude = await make_bot([spec()], **cfg)
    svc.repos.add("throwaway")
    svc.topics["throwaway"] = ["demo"]
    return bot, svc, claude


def test_protected_and_unknown_repos_refused():
    async def go():
        bot, svc, _ = await admin_bot()
        for name in ("app-template", "homie-catcher", "homelab-gitops", "mattermost-arm64", "release", "papl"):
            p = post(f"retire {name}")
            await bot.handle_post(p)
            assert "protected" in thread(svc, p["id"])[-1]["message"]
        p = post("retire nope-repo")
        await bot.handle_post(p)
        assert "There is no repo" in thread(svc, p["id"])[-1]["message"]
        assert not svc.archived and not svc.topics.get("app-template")

    asyncio.run(go())


def test_retire_needs_button_and_typed_name_then_restore_and_delete():
    async def go():
        bot, svc, _ = await admin_bot()
        await bot.plane_project("throwaway")  # Plane project exists
        svc.channels["throwaway"] = {"id": "chanthrowaway", "name": "throwaway", "delete_at": 0}
        # retire: plan + buttons
        p = post("retire throwaway")
        await bot.handle_post(p)
        plan = thread(svc, p["id"])[-1]
        assert "Retire `kiukairor-org/throwaway`" in plan["message"] and "Plane project" in plan["message"]
        confirm, cancel = last_buttons(svc)
        assert confirm["name"] == "Retire"
        assert await bot.handle_action(*click(confirm, user=STRANGER)) == {"ephemeral_text": "not allowed"}
        # typing the name before the button does nothing
        await bot.handle_post(post("throwaway", root=p["id"]))
        assert "Use the buttons" in thread(svc, p["id"])[-1]["message"] and not svc.archived
        assert (await bot.handle_action(*click(confirm)))["ephemeral_text"] == "Now type the repo name."
        await bot.drain()
        await bot.handle_post(post("throw-away", root=p["id"]))
        assert "doesn't match" in thread(svc, p["id"])[-1]["message"] and not svc.archived
        await bot.handle_post(post("throwaway", root=p["id"]))
        done = thread(svc, p["id"])[-1]["message"]
        assert done.startswith("🗄️ Retired") and "⚠️" not in done
        assert svc.topics["throwaway"] == ["retired"] and "throwaway" in svc.archived
        assert svc.plane_projects[0].get("archived_at") and svc.channels["throwaway"]["delete_at"]
        assert bot.admin.is_retired("throwaway")
        # replaying the button does nothing
        assert (await bot.handle_action(*click(confirm)))["ephemeral_text"] in ("Already handled.",)
        # restore: button only, everything back including the original topics
        r = post("restore throwaway")
        await bot.handle_post(r)
        rb = last_buttons(svc)[0]
        assert rb["name"] == "Restore"
        await bot.handle_action(*click(rb))
        await bot.drain()
        assert thread(svc, r["id"])[-1]["message"].startswith("♻️ Restored")
        assert "throwaway" not in svc.archived and svc.topics["throwaway"] == ["demo"]
        assert not svc.plane_projects[0].get("archived_at") and not svc.channels["throwaway"]["delete_at"]
        # delete refused while active
        d = post("delete throwaway")
        await bot.handle_post(d)
        assert "only works on a retired repo" in thread(svc, d["id"])[-1]["message"]
        # retire again, then delete (button + name)
        r2 = post("retire throwaway")
        await bot.handle_post(r2)
        await bot.handle_action(*click(last_buttons(svc)[0]))
        await bot.drain()
        await bot.handle_post(post("throwaway", root=r2["id"]))
        d2 = post("delete throwaway")
        await bot.handle_post(d2)
        db_btn = last_buttons(svc)[0]
        assert db_btn["name"] == "Delete permanently" and "cannot be undone" in thread(svc, d2["id"])[-1]["message"]
        await bot.handle_action(*click(db_btn))
        await bot.drain()
        await bot.handle_post(post("throwaway", root=d2["id"]))
        assert thread(svc, d2["id"])[-1]["message"].startswith("🗑️ Deleted")
        assert "throwaway" in svc.deleted and not svc.plane_projects
        assert svc.channels["throwaway"]["delete_at"]  # channel stays archived (history kept)

    asyncio.run(go())


def test_cancel_and_expiry():
    async def go():
        bot, svc, _ = await admin_bot()
        p = post("retire throwaway")
        await bot.handle_post(p)
        _, cancel = last_buttons(svc)
        await bot.handle_action(*click(cancel))
        await bot.drain()
        assert "Cancelled" in thread(svc, p["id"])[-1]["message"] and not svc.archived
        p2 = post("retire throwaway")
        await bot.handle_post(p2)
        confirm, _ = last_buttons(svc)
        bot.clock.t += 601
        assert "Expired" in (await bot.handle_action(*click(confirm)))["ephemeral_text"]
        assert not svc.archived

    asyncio.run(go())


def test_app_channels_backfill_progress_move_and_pretargeted_chat():
    async def go():
        bot, svc, claude = await make_bot([spec()], APP_CHANNELS="true")
        svc.repos.add("dice-service")
        runner = FakeBrainstormRunner([
            {"reply": "Dice ideas.", "ready_to_build": True},
            {"title": "Add 3d6", "target": {"kind": "existing", "repo": "agentic-demo"}, "problem": "Players want several dice.",
             "build": ["parse NdM", "roll N dice", "sum them"], "acceptance_criteria": ["3d6 works", "bad input is 400"], "out_of_scope": []},
        ])  # fmt: skip
        bot.brainstorm = Brainstorm(bot, runner)
        await bot.startup()
        made = await bot.channels.reconcile(force=True)
        assert "dice-service" in made and "agentic-demo" in made and "app-template" not in made and "release" not in made
        assert OWNER in svc.channel_members["dice-service"]
        ch = bot.channels.channel_for_repo("dice-service")
        assert any(p["channel_id"] == ch and "channel for **kiukairor-org/dice-service**" in p["message"] for p in svc.posts)
        # an #ideas item for an existing repo moves to ~repo (pointer left in #ideas)
        p = post("work on dice-service: add 3d6")
        await bot.handle_post(p)
        await bot.run_next()
        assert "Continues in ~dice-service" in thread(svc, p["id"])[-1]["message"]
        moved = [x for x in svc.posts if x["channel_id"] == ch and x.get("root_id")]
        assert any([b["name"] for b in (x.get("props") or {}).get("attachments", [{}])[0].get("actions", [])] == ["Approve", "Reject"]
                   for x in moved if (x.get("props") or {}).get("attachments"))  # fmt: skip
        # chat in ~dice-service: repo context preloaded; "do it" drafts pinned to the repo (Claude said agentic-demo)
        root = post("multi-dice?", channel=ch)
        await bot.handle_post(root)
        assert '<repo_context repo="dice-service">' in runner.runs[0]["text"] and "Rolls dice." in runner.runs[0]["text"]
        await bot.handle_post(post("do it", channel=ch, root=root["id"]))
        draft = [x for x in thread(svc, root["id"]) if "Feature request draft v1" in x["message"]][-1]
        assert "**Target app:** `dice-service`" in draft["message"]
        send = [b for b in draft["props"]["attachments"][0]["actions"] if b["name"] == "Send to ~dice-service"][0]
        await bot.handle_action(*click(send))
        await bot.drain()
        sent_root = [x for x in svc.posts if x["channel_id"] == ch and not x.get("root_id") and "Feature request from chat" in x["message"]]
        assert sent_root, "request stays in ~dice-service"
        assert not [x for x in svc.posts if x["channel_id"] == IDEAS and "from chat" in x["message"]]
        tri = thread(svc, sent_root[0]["id"])
        assert [b["name"] for b in tri[-1]["props"]["attachments"][0]["actions"]] == ["Approve", "Reject"]
        assert not svc.issues and len(claude.prompts) == 1  # the only spec run was the #ideas item
        # retired repos never get a channel back from reconcile
        bot.admin._save_state("dice-service", "retired")
        svc.channels["dice-service"]["delete_at"] = 1
        assert await bot.channels.ensure("dice-service") is None
        assert ORG

    asyncio.run(go())
