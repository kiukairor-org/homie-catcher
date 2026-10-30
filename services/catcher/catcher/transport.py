"""Minimal HTTP transport. Every HTTP call in the bot goes through ``Transport.send``,
which keeps clients testable (tests swap in a recording fake) and gives one choke point."""

from __future__ import annotations

import json as jsonlib
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Response:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased keys

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class HttpError(Exception):
    def __init__(self, method: str, url: str, status: int, body: Any = None) -> None:
        # Never include request headers (tokens) in the message.
        detail = body if isinstance(body, (str, dict, list)) else ""
        super().__init__(f"{method} {url} -> HTTP {status}: {str(detail)[:300]}")
        self.method, self.url, self.status, self.body = method, url, status, body


class Transport(Protocol):
    async def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> Response: ...


class AiohttpTransport:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def send(self, method, url, *, headers=None, json=None, params=None, timeout=30) -> Response:
        import aiohttp

        async with self.session.request(
            method,
            url,
            headers=headers,
            json=json,
            params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            text = await resp.text()
            try:
                body = jsonlib.loads(text) if text else None
            except ValueError:
                body = text
            return Response(resp.status, body, {k.lower(): v for k, v in resp.headers.items()})
