"""aiohttp HTTP server (button callbacks + health) and process wiring."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os

from aiohttp import ClientSession, web

from .bot import NOT_ALLOWED, Catcher
from .brainstorm import Brainstorm
from .brainstorm_runner import BrainstormRunner
from .claude_runner import ClaudeRunner
from .config import Config
from .db import DB
from .github import GitHub
from .logsetup import setup_logging
from .mattermost import Mattermost
from .plane import Plane
from .poller import Poller
from .transport import AiohttpTransport

log = logging.getLogger(__name__)
MAX_BODY = 64 * 1024


def make_app(catcher: Catcher) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def action(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            payload = await request.json()
        except (ValueError, json.JSONDecodeError):
            return web.json_response(NOT_ALLOWED)
        try:
            result = await catcher.handle_action(name, payload)
        except Exception:
            log.exception("action %s failed", name)
            result = {"ephemeral_text": "Something went wrong; see the catcher logs."}
        return web.json_response(result)

    async def ingest(request: web.Request) -> web.Response:
        expected = catcher.cfg.ingest_token
        if not expected:
            return web.json_response({"error": "ingest disabled"}, status=404)
        given = request.headers.get("X-Catcher-Ingest-Token", "")
        if not hmac.compare_digest(given.encode(), expected.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            payload = await request.json()
            result = await catcher.ingest_alerts(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            return web.json_response({"error": str(exc)[:200]}, status=400)
        return web.json_response(result)

    app.router.add_get("/healthz", healthz)
    app.router.add_post("/ingest/alert", ingest)
    app.router.add_post("/actions/{name}", action)
    return app


async def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg.secret_values(), os.environ.get("LOG_LEVEL", "INFO"))
    with open(cfg.github_app_private_key_path, encoding="utf-8") as fh:
        key = fh.read()
    async with ClientSession() as session:
        transport = AiohttpTransport(session)
        mm = Mattermost(transport, url=cfg.mm_url, token=cfg.mm_bot_token)
        plane = Plane(transport, base_url=cfg.plane_base_url, public_url=cfg.plane_public_url,
                      api_key=cfg.plane_api_key, workspace=cfg.plane_workspace_slug)  # fmt: skip
        gh = GitHub(transport, org=cfg.github_org, app_id=cfg.github_app_id,
                    installation_id=cfg.github_installation_id, private_key_pem=key)  # fmt: skip
        claude = ClaudeRunner(claude_bin=cfg.claude_bin, plane_mcp_bin=cfg.plane_mcp_bin, plane_base_url=cfg.plane_base_url,
                              plane_api_key=cfg.plane_api_key, workspace=cfg.plane_workspace_slug,
                              oauth_token=cfg.claude_oauth_token, model=cfg.claude_model)  # fmt: skip
        catcher = Catcher(cfg, DB(cfg.db_path), mm, plane, gh, claude)
        bs_runner = BrainstormRunner(claude_bin=cfg.claude_bin, plane_mcp_bin=cfg.plane_mcp_bin, plane_base_url=cfg.plane_base_url,
                                     plane_api_key=cfg.plane_api_key, workspace=cfg.plane_workspace_slug,
                                     oauth_token=cfg.claude_oauth_token, github_org=cfg.github_org,
                                     mcp_python=cfg.mcp_python, model=cfg.claude_model, nr_key_file=cfg.nr_key_file,
                                     nr_account_id=cfg.nr_account_id, nr_region=cfg.nr_region)  # fmt: skip
        catcher.brainstorm = Brainstorm(catcher, bs_runner)
        bs_runner.on_result = lambda feature, result, err: catcher.usage.record(
            feature, result, ok=bool(result) and not result.get("is_error"), error=err
        )
        await catcher.startup()

        runner = web.AppRunner(make_app(catcher), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", cfg.listen_port).start()
        log.info("catcher listening on :%d", cfg.listen_port)
        try:
            await asyncio.gather(
                mm.listen(session, catcher.on_event),
                catcher.worker_loop(),
                Poller(catcher).loop(cfg.poll_interval_s),
            )
        finally:
            await runner.cleanup()
