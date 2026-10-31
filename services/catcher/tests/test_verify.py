"""D74: the chat sees the whole thread (incl. the pipeline's own status posts), gets read-only verification
tools scoped to the channel's repo, answers "did it work?" from evidence, and flags unsupported claims."""

import asyncio
import json

from catcher import brainstorm_runner as br
from catcher import verify_ro_mcp as v
from catcher.brainstorm import EVIDENCE_WARNING, HANDOFF_WARNING, Brainstorm, unsupported_claims
from tests.fakes import BRAINSTORM, OWNER, FakeBrainstormRunner, FakeServices, make_bot, post

REPOS = ("houss", "agentic-demo", "app-template", "homie-catcher", "papl", "release")
PR = "https://github.com/kiukairor-org/houss/pull/7"
ISSUE = "https://github.com/kiukairor-org/houss/issues/6"


async def houss_bot(outputs):
    bot, svc, _ = await make_bot([], services=FakeServices(repos=REPOS), APP_CHANNELS="true")
    runner = FakeBrainstormRunner(outputs)
    bot.brainstorm = Brainstorm(bot, runner)
    await bot.startup()
    await bot.channels.reconcile(force=True)
    return bot, svc, runner, bot.channels.channel_for_repo("houss")


def owner_says(svc, text, channel, root=""):
    """An owner post as Mattermost stores it (it is in the thread when the websocket event arrives)."""
    p = post(text, channel=channel, root=root, create_at=1_800_000_000_000 + 10_000_000 + len(svc.posts) * 1000)
    svc.posts.append(dict(p))
    return p


async def houss6_thread(bot, svc, ch):
    """The item thread of houss#6 as the pipeline left it: root + status posts + another bot's post."""
    root = (await bot.mm.post(ch, "**Remove list dropdown from add-item form**\n\nPlane HOUSS-6"))["id"]
    for msg in (f"📋 GitHub issue opened: {ISSUE}", f"🔧 PR #7 opened: {PR}", "✅ CI `success` on PR #7 (5be3c1a)",
                "🔍 Reviewer approved on PR #7", "🎉 PR #7 merged",
                "🚀 PR #7 deployed to prod (0cc9e58): https://houss.tail8e3e5b.ts.net"):  # fmt: skip
        await bot.mm.post(ch, msg, root_id=root)
    svc.mm_users["nrbotuserid"] = {"username": "nr-alerts", "is_bot": True}
    svc.posts.append({"id": "otherbotpost", "user_id": "nrbotuserid", "channel_id": ch, "root_id": root,
                      "message": "no open incidents", "create_at": 1_800_000_000_000 + 9_000_000})  # fmt: skip
    return root


VERIFIED = (
    "**Delivered and live.**\n"
    f"- PR #7 merged 00:33Z: {PR}; reviewer approved\n"
    "- Deployed: live /version is 0cc9e5853e9e, Argo Synced/Healthy\n"
    "- Not verified: how the form looks in your browser."
)


def test_did_it_work_sees_status_posts_and_gets_scoped_verify_tools():
    async def go():
        bot, svc, runner, ch = await houss_bot([{"reply": VERIFIED, "intent": "answer", "request": None},
                                                {"reply": "Yes, it was merged and deployed.", "intent": "answer", "request": None}])  # fmt: skip
        root = await houss6_thread(bot, svc, ch)
        q = owner_says(svc, "did it work? the dropdown should be gone", ch, root)
        await bot.handle_post(q)
        (run,) = runner.runs
        text = run["text"]
        # (1) every post of the thread, labelled by author, the bot's status posts included
        for needle in (ISSUE, PR, "CI `success` on PR #7", "Reviewer approved", "PR #7 merged", "deployed to prod (0cc9e58)"):
            assert needle in text, needle
        assert text.count('author="catcher-bot (pipeline status)"') == 7  # root + 6 milestones
        assert '<post author="bot:nr-alerts"' in text and "no open incidents" in text
        assert '<post author="owner"' in text and text.rstrip().endswith("Return the JSON object now.")
        assert text.index("PR #7 merged") < text.index("did it work?")  # chronological, the question last
        # (2) verification tools scoped to this repo, read-only token for it alone, more turns for the checks
        assert run["verify_repo"] == "houss" and run["verify_token"].startswith("ghs_")
        tok = [c for c in svc.calls if c.path.endswith("/access_tokens") and c.json and c.json.get("repositories") == ["houss"]]
        assert tok and tok[-1].json["permissions"] == {"contents": "read", "issues": "read", "pull_requests": "read", "metadata": "read"}
        assert run["max_turns"] == br.VERIFY_MAX_TURNS
        assert "DID IT WORK?" in run["system_prompt"] and "scoped to kiukairor-org/houss" in run["system_prompt"]
        # (3) the evidence-backed answer is posted as a chat reply, untouched; nothing is started
        reply = [p for p in svc.posts if p.get("root_id") == root][-1]
        assert reply["message"] == VERIFIED and reply["props"]["homie_kind"] == "chat"
        assert not svc.issues and not svc.plane_items and not svc.posted_comments
        # an unsupported claim gets flagged; the earlier reply is now labelled as the chat's own
        await bot.handle_post(owner_says(svc, "so it's live?", ch, root))
        assert '<post author="catcher-bot (chat reply)"' in runner.runs[1]["text"]
        last = [p for p in svc.posts if p.get("root_id") == root][-1]["message"]
        assert last.endswith(EVIDENCE_WARNING) and HANDOFF_WARNING not in last

    asyncio.run(go())


def test_history_cap_keeps_the_most_recent_status_posts():
    async def go():
        bot, svc, runner, ch = await houss_bot([{"reply": "ok", "intent": "answer", "request": None}])
        root = await houss6_thread(bot, svc, ch)
        for i in range(25):  # 75k chars of later chat after the milestones: more than the 40k cap
            owner_says(svc, f"chatter {i} " + "x" * 3000, ch, root)
        await bot.handle_post(owner_says(svc, "did it deploy?", ch, root))
        text = runner.runs[0]["text"]
        assert "deployed to prod (0cc9e58)" in text and "PR #7 merged" in text and "Remove list dropdown" in text
        assert "older post(s) omitted" in text and "chatter 24" in text and "chatter 0 " not in text
        assert len(text) < 60_000

    asyncio.run(go())


def test_brainstorm_channel_has_no_verify_tools():
    async def go():
        bot, svc, runner, _ = await houss_bot([{"reply": "hi", "intent": "answer", "request": None}])
        await bot.handle_post(owner_says(svc, "ideas for a recipe app?", BRAINSTORM))
        assert "verify_repo" not in runner.runs[0] and "DID IT WORK?" not in runner.runs[0]["system_prompt"]

    asyncio.run(go())


def test_owner_orders_the_offline_banner_fix_in_a_done_item_thread():
    """(4) Context only: an owner order in ~houss (in the houss#6 thread) must start a NEW houss issue."""
    req = {"title": "Show the real sync error instead of 'Offline'", "target": {"kind": "existing", "repo": "houss"},
           "problem": "The client shows 'Offline - changes will sync later' for any sync exception, though the API is up.",
           "build": ["distinguish network errors from others", "show the error", "test it"],
           "acceptance_criteria": ["an HTTP 500 from sync is not shown as Offline", "tests pass"], "out_of_scope": [],
           "related_pr": 7}  # fmt: skip

    async def go():
        bot, svc, runner, ch = await houss_bot([{"reply": "Fixing the banner.", "intent": "order", "request": req}])
        root = await houss6_thread(bot, svc, ch)
        bot.db.upsert_thread(root, ch)
        bot.db.set_thread_repo(root, "houss", False)
        done = bot.db.create_item(root, "Remove list dropdown", "done")
        bot.db.update_item(done, repo="houss", gh_issue_url=ISSUE)
        svc.pulls["houss"] = [{"number": 7, "state": "closed", "user": {"login": "kiu-homie-bot[bot]"}}]
        await bot.handle_post(owner_says(svc, "the app says Offline for every sync error, fix that", ch, root))
        (issue,) = svc.issues
        assert issue["repo"] == "houss" and issue["title"] == req["title"] and not svc.posted_comments  # merged PR: new issue
        assert [p for p in svc.posts if p.get("root_id") == root][-1][
            "message"
        ] == f"✅ Started: GitHub issue in `houss`: {issue['html_url']}"
        assert bot.db.item(done)["status"] == "done"  # the delivered item is untouched

    asyncio.run(go())


def test_unsupported_claims():
    for claim in ("It was merged and deployed.", "The change is live now. Enjoy!", "PR #7 got merged yesterday",
                  "Your fix shipped this morning."):  # fmt: skip
        assert unsupported_claims(claim), claim
    for ok in (f"PR #7 merged: {PR}", "Deployed as 0cc9e58, Argo Healthy.", "- The app is deployed. — _image 67754a820035102f3c01aa0bdae2040469db2338_", "It has not been deployed yet.",
               "I can't verify that it was deployed.", "Was it merged?", "Not verified: deploy state.",
               "No PR yet, so nothing is deployed."):  # fmt: skip
        assert not unsupported_claims(ok), ok


def test_verify_server_config_and_tools():
    r = br.BrainstormRunner(claude_bin="claude", plane_mcp_bin="p", plane_base_url="http://p", plane_api_key="k", workspace="w",
                            oauth_token="t", github_org="kiukairor-org", mcp_python="/py", nr_key_file="/secrets/nr/KEY",
                            nr_account_id="8680047", nr_region="EU")  # fmt: skip
    plain = r.mcp_config("ghs_a", ["houss"])
    assert br.VERIFY_SERVER not in plain["mcpServers"]
    cfg = r.mcp_config("ghs_a", ["houss"], "houss", "ghs_scoped")
    env = cfg["mcpServers"]["verify"]["env"]
    assert env["REPO"] == "houss" and env["GITHUB_TOKEN"] == "ghs_scoped" and env["NR_KEY_FILE"] == "/secrets/nr/KEY"
    assert env["NEW_RELIC_ACCOUNT_ID"] == "8680047" and "NEW_RELIC_API_KEY" not in json.dumps(cfg)
    argv = br.build_argv("claude", "/m.json", "sys", {}, 16, None, verify=True)
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert {f"mcp__verify__{t}" for t in br.VERIFY_TOOLS} <= set(allowed)
    assert "mcp__verify__deploy_status" not in br.build_argv("claude", "/m.json", "sys", {}, 10, None)[argv.index("--allowedTools") + 1]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"  # still no Bash/Read/Write


def test_verify_helpers():
    app = {"metadata": {"name": "app-houss"},
           "spec": {"sources": [{"path": "charts/app-namespace"},
                                {"kustomize": {"images": ["ghcr.io/kiukairor-org/app-template=ghcr.io/kiukairor-org/houss:0cc9e58"]}}]},
           "status": {"sync": {"status": "Synced"}, "health": {"status": "Healthy"},
                      "operationState": {"phase": "Succeeded", "finishedAt": "2026-10-31T00:35:00Z"}}}  # fmt: skip
    s = v.argo_summary(app)
    assert s["sync"] == "Synced" and s["health"] == "Healthy" and s["images"][0].endswith("houss:0cc9e58")
    q = v.nrql_queries("houss", 60)
    assert all("service.name = 'houss'" in x and "SINCE 60 minutes ago" in x for x in q.values())
    for bad in ("houss' OR true", "Houss", "a"):
        try:
            v.nrql_queries(bad, 60)
        except v.ToolError:
            continue
        raise AssertionError(bad)
    assert OWNER  # fakes imported for the owner id used by post()
