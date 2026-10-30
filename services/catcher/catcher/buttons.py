"""Mattermost interactive buttons whose context is HMAC-SHA256 signed.

The signed payload includes the action name, so a context minted for one button
cannot be replayed against another endpoint. Replays of the same button are
harmless because every handler is an atomic status transition (idempotent).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

ACTION_NAMES = ("choose_repo", "approve", "reject", "approve_repo", "reject_repo")


def _canonical(ctx: dict[str, Any]) -> bytes:
    return json.dumps({k: v for k, v in ctx.items() if k != "sig"}, sort_keys=True, separators=(",", ":")).encode()


def sign(secret: bytes, ctx: dict[str, Any]) -> str:
    return hmac.new(secret, _canonical(ctx), hashlib.sha256).hexdigest()


def verify(secret: bytes, action: str, ctx: Any) -> dict[str, Any] | None:
    """Return the context if the signature is valid and bound to ``action``; else None."""
    if not isinstance(ctx, dict):
        return None
    sig = ctx.get("sig")
    if not isinstance(sig, str) or ctx.get("a") != action:
        return None
    if not hmac.compare_digest(sig, sign(secret, ctx)):
        return None
    return ctx


def button(base_url: str, secret: bytes, label: str, action: str, ctx: dict[str, Any], style: str = "default") -> dict:
    if action not in ACTION_NAMES:
        raise ValueError(action)
    full = {"a": action, **ctx}
    full["sig"] = sign(secret, full)
    # Mattermost action ids must be alphanumeric.
    bid = re.sub(r"[^A-Za-z0-9]", "", f"{action}{ctx.get('item', '')}{ctx.get('repo', '')}")[:60] or action.replace("_", "")
    return {
        "id": bid,
        "name": label,
        "style": style,
        "integration": {"url": f"{base_url}/actions/{action}", "context": full},
    }
