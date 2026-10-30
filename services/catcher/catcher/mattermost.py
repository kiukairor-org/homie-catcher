"""Mattermost REST v4 + websocket client for the catcher bot account."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .transport import HttpError, Transport

log = logging.getLogger(__name__)


class Mattermost:
    def __init__(self, transport: Transport, *, url: str, token: str) -> None:
        self.transport = transport
        self.url = url.rstrip("/")
        self._token = token
        self.bot_user_id: str | None = None

    async def _req(self, method: str, path: str, json_body: Any = None, params: dict | None = None) -> Any:
        resp = await self.transport.send(
            method, f"{self.url}/api/v4{path}", headers={"Authorization": f"Bearer {self._token}"}, json=json_body, params=params
        )
        if not resp.ok:
            raise HttpError(method, path, resp.status, resp.body)
        return resp.body

    async def me(self) -> dict:
        me = await self._req("GET", "/users/me")
        self.bot_user_id = me["id"]
        return me

    async def channel_id(self, team: str, channel: str) -> str:
        body = await self._req("GET", f"/teams/name/{team}/channels/name/{channel}")
        return body["id"]

    async def post(self, channel_id: str, message: str, *, root_id: str | None = None, attachments: list | None = None) -> dict:
        body: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            body["root_id"] = root_id
        if attachments:
            body["props"] = {"attachments": attachments}
        return await self._req("POST", "/posts", body)

    async def patch_post(self, post_id: str, message: str, attachments: list | None = None) -> dict:
        body: dict[str, Any] = {"message": message}
        if attachments is not None:
            body["props"] = {"attachments": attachments}
        return await self._req("PUT", f"/posts/{post_id}/patch", body)

    async def ephemeral(self, user_id: str, channel_id: str, message: str, root_id: str | None = None) -> None:
        post: dict[str, Any] = {"channel_id": channel_id, "message": message}
        if root_id:
            post["root_id"] = root_id
        await self._req("POST", "/posts/ephemeral", {"user_id": user_id, "post": post})

    async def file_info(self, file_id: str) -> dict:
        return await self._req("GET", f"/files/{file_id}/info")

    async def file_bytes(self, file_id: str, max_bytes: int) -> bytes:
        return await self.transport.fetch_bytes(
            f"{self.url}/api/v4/files/{file_id}", headers={"Authorization": f"Bearer {self._token}"}, max_bytes=max_bytes
        )

    async def react(self, post_id: str, emoji: str = "eyes") -> None:
        await self._req("POST", "/reactions", {"user_id": self.bot_user_id, "post_id": post_id, "emoji_name": emoji})

    async def listen(self, session: Any, on_event: Callable[[dict], Awaitable[None]]) -> None:
        """Websocket loop with reconnect/backoff. ``session`` is an aiohttp.ClientSession."""
        ws_url = self.url.replace("https://", "wss://").replace("http://", "ws://") + "/api/v4/websocket"
        backoff = 1.0
        while True:
            try:
                async with session.ws_connect(ws_url, headers={"Authorization": f"Bearer {self._token}"}, heartbeat=30) as ws:
                    log.info("websocket connected")
                    backoff = 1.0
                    async for msg in ws:
                        if msg.type.name != "TEXT":
                            if msg.type.name in ("CLOSED", "ERROR"):
                                break
                            continue
                        try:
                            event = json.loads(msg.data)
                        except ValueError:
                            continue
                        try:
                            await on_event(event)
                        except Exception:
                            log.exception("event handler failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("websocket error: %s", type(exc).__name__)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
