import asyncio

from aiohttp.test_utils import TestClient, TestServer

from catcher.app import make_app
from tests.fakes import STRANGER, click, last_buttons, make_bot, post, spec, triage_mode


def test_http_endpoints():
    async def go():
        bot, svc, claude = await make_bot([spec()])
        triage_mode(bot)
        p = post("work on notes: dark mode")
        await bot.handle_post(p)
        await bot.run_next()
        async with TestClient(TestServer(make_app(bot))) as client:
            r = await client.get("/healthz")
            assert r.status == 200 and (await r.json()) == {"ok": True}
            r = await client.post("/actions/approve", data="not json")
            assert (await r.json()) == {"ephemeral_text": "not allowed"}
            name, payload = click(next(b for b in last_buttons(svc) if b["name"] == "Approve"), user=STRANGER)
            r = await client.post(f"/actions/{name}", json=payload)
            assert (await r.json()) == {"ephemeral_text": "not allowed"}
            name, payload = click(next(b for b in last_buttons(svc) if b["name"] == "Approve"))
            r = await client.post(f"/actions/{name}", json=payload)
            assert "Approved" in (await r.json())["ephemeral_text"]
            await bot.drain()
        assert len(svc.issues) == 1

    asyncio.run(go())
