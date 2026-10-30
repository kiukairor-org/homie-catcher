import asyncio

from catcher.routing import (
    EXCLUDED_REPOS,
    candidate_names,
    parse_message,
    parse_name_reply,
    pick_free_repo_name,
    resolve_repo,
    selectable_repos,
    valid_repo_name,
)
from tests.fakes import STRANGER, last_buttons, make_bot, post, spec


def test_parse_forms():
    r = parse_message("Work On agentic-demo: add a /health endpoint")
    assert (r.kind, r.repo, r.text) == ("repo", "agentic-demo", "add a /health endpoint")
    r = parse_message("NEW IDEA: a recipe app\nwith tags")
    assert (r.kind, r.text) == ("new", "a recipe app\nwith tags")
    assert parse_message("make it faster").kind == "none"


def test_excluded_repos():
    assert EXCLUDED_REPOS == {"release", "papl", "homelab-gitops", "mattermost-arm64", "homie-catcher", "app-template"}
    avail = ["agentic-demo", "papl", "Release", "notes", "app-template"]
    assert selectable_repos(avail) == ["agentic-demo", "notes"]
    assert resolve_repo("PAPL", avail) is None
    assert resolve_repo("Agentic-Demo", avail) == "agentic-demo"


def test_repo_name_validation():
    assert valid_repo_name("recipe-box")
    for bad in ("Recipe", "1abc", "a", "x" * 40, "has_underscore", "trailing-", "papl"):
        assert not valid_repo_name(bad), bad
    assert candidate_names("Recipe Box")[:3] == ["recipe-box", "recipe-box-2", "recipe-box-3"]
    assert candidate_names("!!!") == []
    assert parse_name_reply("name: My App") == "my-app"


class _GH:
    def __init__(self, taken):
        self.taken = set(taken)

    async def repo_exists(self, name):
        return name in self.taken


def test_pick_free_name_taken_appends_suffix():
    assert asyncio.run(pick_free_repo_name(_GH({"recipes"}), "recipes")) == "recipes-2"
    assert asyncio.run(pick_free_repo_name(_GH({"recipes", "recipes-2"}), "recipes")) == "recipes-3"
    all_taken = {"recipes"} | {f"recipes-{i}" for i in range(2, 10)}
    assert asyncio.run(pick_free_repo_name(_GH(all_taken), "recipes")) is None
    assert asyncio.run(pick_free_repo_name(_GH(set()), "Bad_Name!")) is None
    assert asyncio.run(pick_free_repo_name(_GH(set()), None)) is None


# ---- through the bot -------------------------------------------------------


def test_named_repo_routes_without_asking():
    async def go():
        bot, svc, claude = await make_bot([spec()])
        await bot.handle_post(post("work on agentic-demo: add dark mode"))
        await bot.run_next()
        assert "kiukairor-org/agentic-demo" in claude.prompts[0]
        assert bot.db.latest_item(svc.posts[0]["root_id"])["status"] == "triage"
        assert svc.plane_projects[0]["name"] == "agentic_demo"  # Plane-safe name

    asyncio.run(go())


def test_unknown_or_excluded_repo_asks_which_app():
    async def go():
        bot, svc, claude = await make_bot([spec()])
        await bot.handle_post(post("work on papl: do something"))
        btns = last_buttons(svc)
        names = [b["name"] for b in btns]
        assert names == ["agentic-demo", "notes", "New repo"]
        assert "papl" in svc.posts[-1]["props"]["attachments"][0]["text"]
        assert claude.prompts == []

    asyncio.run(go())


def test_no_repo_asks_which_app_then_button_routes():
    async def go():
        bot, svc, claude = await make_bot([spec()])
        p = post("add dark mode")
        await bot.handle_post(p)
        btns = last_buttons(svc)
        assert [b["name"] for b in btns] == ["agentic-demo", "notes", "New repo"]
        name, payload = next(("choose_repo", {"user_id": p["user_id"], "context": b["integration"]["context"]})
                             for b in btns if b["name"] == "notes")  # fmt: skip
        res = await bot.handle_action(name, payload)
        assert "notes" in res["ephemeral_text"]
        await bot.run_next()
        assert bot.db.thread(p["id"])["repo"] == "notes"
        assert bot.db.latest_item(p["id"])["status"] == "triage"

    asyncio.run(go())


def test_thread_reply_reuses_remembered_repo():
    async def go():
        bot, svc, claude = await make_bot([spec(), spec(title="Also a high-contrast theme")])
        root = post("work on notes: dark mode")
        await bot.handle_post(root)
        await bot.run_next()
        await bot.handle_post(post("also a high-contrast theme", root=root["id"]))
        await bot.run_next()
        assert "kiukairor-org/notes" in claude.prompts[1]
        items = bot.db._x("SELECT * FROM items WHERE root_post_id=?", (root["id"],)).fetchall()
        assert [i["repo"] for i in items] == ["notes", "notes"]
        assert sum(1 for p in svc.posts if "which app" in str(p).lower()) == 0

    asyncio.run(go())


def test_new_repo_name_taken_gets_suffix():
    async def go():
        bot, svc, claude = await make_bot([spec(new_repo={"name": "notes", "description": "x"})])
        root = post("new idea: a notes app")
        await bot.handle_post(root)
        await bot.run_next()
        item = bot.db.latest_item(root["id"])
        assert item["new_repo_name"] == "notes-2" and item["status"] == "triage"

    asyncio.run(go())


def test_new_repo_invalid_name_asks_owner():
    async def go():
        bot, svc, claude = await make_bot([spec(new_repo={"name": "!!!", "description": "x"})])
        root = post("new idea: a notes app")
        await bot.handle_post(root)
        await bot.run_next()
        assert bot.db.latest_item(root["id"])["status"] == "awaiting_name"
        await bot.handle_post(post("name: agentic-demo", root=root["id"]))  # taken
        assert bot.db.latest_item(root["id"])["status"] == "awaiting_name"
        await bot.handle_post(post("name: jotter", root=root["id"]))
        item = bot.db.latest_item(root["id"])
        assert item["new_repo_name"] == "jotter" and item["status"] == "triage"

    asyncio.run(go())


def test_user_allowlist_and_channel_and_self():
    async def go():
        bot, svc, claude = await make_bot([spec()])
        await bot.handle_post(post("work on notes: x", user=STRANGER))
        await bot.handle_post(post("work on notes: x", channel="otherchannel00000000000000"))
        await bot.handle_post(post("work on notes: x", user=bot.mm.bot_user_id))
        await bot.handle_post(post("work on notes: x", type="system_join_channel"))
        await bot.handle_post(post("work on notes: x", props={"from_webhook": "true"}))
        assert svc.posts == [] and bot.db.next_queued() is None
        assert not [c for c in svc.calls if c.path == "/api/v4/reactions"]
        # the owner is accepted and gets the 👀 reaction
        p = post("work on notes: x")
        await bot.handle_post(p)
        reacts = [c for c in svc.calls if c.path == "/api/v4/reactions"]
        assert reacts and reacts[0].json == {"user_id": bot.mm.bot_user_id, "post_id": p["id"], "emoji_name": "eyes"}
        # duplicate websocket delivery is ignored
        await bot.handle_post(p)
        assert len([c for c in svc.calls if c.path == "/api/v4/reactions"]) == 1

    asyncio.run(go())
