"""Configuration, read from the environment only. See README.md for every variable."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_ACTION_BASE_URL = "http://catcher.catcher.svc.cluster.local:8080"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Config:
    mm_url: str
    mm_bot_token: str = field(repr=False)
    mm_team: str
    owner_user_ids: frozenset[str]
    action_secret: bytes = field(repr=False)
    plane_base_url: str
    plane_public_url: str
    plane_api_key: str = field(repr=False)
    plane_workspace_slug: str
    github_org: str
    github_app_id: str
    github_installation_id: str
    github_app_private_key_path: str
    claude_oauth_token: str = field(repr=False)
    db_path: str
    claude_runs_per_hour: int = 10
    # Optional, with defaults
    ideas_channel: str = "ideas"
    agent_log_channel: str = "agent-log"
    action_base_url: str = DEFAULT_ACTION_BASE_URL
    claude_bin: str = "claude"
    plane_mcp_bin: str = "plane-mcp-server"
    claude_model: str | None = None
    owner_mention: str = "@kiukairor"
    poll_interval_s: int = 60
    ingest_token: str | None = field(default=None, repr=False)  # CATCHER_INGEST_TOKEN; unset -> /ingest disabled
    nr_target_repo: str = "agentic-demo"
    app_domain: str = "tail8e3e5b.ts.net"  # APP_DOMAIN: tailnet suffix of app/preview Ingress hosts
    brainstorm_channel: str = "brainstorm"  # MM_BRAINSTORM_CHANNEL; missing channel = feature off
    brainstorm_runs_per_hour: int = 30  # BRAINSTORM_RUNS_PER_HOUR (chat replies + drafts)
    brainstorm_max_turns: int = 10  # BRAINSTORM_MAX_TURNS: Claude agentic turns per reply
    brainstorm_draft_max_turns: int = 6
    mm_public_url: str = "https://chat.tail8e3e5b.ts.net"  # MM_PUBLIC_URL: for thread permalinks
    mcp_python: str = "/opt/plane-mcp/bin/python"  # MCP_PYTHON: runs the read-only GitHub MCP server
    app_channels: bool = True  # APP_CHANNELS: one ~<repo> channel per app
    nr_key_file: str = "/secrets/newrelic/NEW_RELIC_API_KEY"  # NR_KEY_FILE: user key file for the verify tools (D74)
    nr_account_id: str = ""  # NEW_RELIC_ACCOUNT_ID
    nr_region: str = "EU"  # NEW_RELIC_REGION
    sre_image: str = ""  # SRE_IMAGE: catcher image for SRE Jobs; empty = SRE agent off (D75)
    sre_max_turns: int = 40  # SRE_MAX_TURNS
    sre_on_alerts: bool = False  # SRE_ON_ALERTS: NR alerts may wake an investigation-only run
    listen_port: int = 8080

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        def req(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(f"missing required environment variable {name}")
            return value

        owners = frozenset(u.strip() for u in req("OWNER_USER_IDS").split(",") if u.strip())
        if not owners:
            raise ConfigError("OWNER_USER_IDS is empty")
        secret = req("CATCHER_ACTION_SECRET")
        if len(secret) < 32:
            raise ConfigError("CATCHER_ACTION_SECRET must be at least 32 characters")
        try:
            runs = int(env.get("CLAUDE_RUNS_PER_HOUR", "10"))
        except ValueError as exc:
            raise ConfigError("CLAUDE_RUNS_PER_HOUR must be an integer") from exc
        if runs < 1:
            raise ConfigError("CLAUDE_RUNS_PER_HOUR must be >= 1")
        return cls(
            mm_url=req("MM_URL").rstrip("/"),
            mm_bot_token=req("MM_BOT_TOKEN"),
            mm_team=req("MM_TEAM"),
            owner_user_ids=owners,
            action_secret=secret.encode(),
            plane_base_url=req("PLANE_BASE_URL").rstrip("/"),
            plane_public_url=req("PLANE_PUBLIC_URL").rstrip("/"),
            plane_api_key=req("PLANE_API_KEY"),
            plane_workspace_slug=req("PLANE_WORKSPACE_SLUG"),
            github_org=req("GITHUB_ORG"),
            github_app_id=req("GITHUB_APP_ID"),
            github_installation_id=req("GITHUB_INSTALLATION_ID"),
            github_app_private_key_path=req("GITHUB_APP_PRIVATE_KEY_PATH"),
            claude_oauth_token=req("CLAUDE_CODE_OAUTH_TOKEN"),
            db_path=req("DB_PATH"),
            claude_runs_per_hour=runs,
            ideas_channel=env.get("MM_IDEAS_CHANNEL", "ideas"),
            agent_log_channel=env.get("MM_AGENT_LOG_CHANNEL", "agent-log"),
            action_base_url=env.get("CATCHER_ACTION_BASE_URL", DEFAULT_ACTION_BASE_URL).rstrip("/"),
            claude_bin=env.get("CLAUDE_BIN", "claude"),
            plane_mcp_bin=env.get("PLANE_MCP_BIN", "plane-mcp-server"),
            claude_model=env.get("CLAUDE_MODEL") or None,
            owner_mention=env.get("OWNER_MENTION", "@kiukairor"),
            ingest_token=_ingest_token(env.get("CATCHER_INGEST_TOKEN", "")),
            nr_target_repo=env.get("NR_TARGET_REPO", "agentic-demo").strip() or "agentic-demo",
            app_domain=env.get("APP_DOMAIN", "tail8e3e5b.ts.net").strip() or "tail8e3e5b.ts.net",
            brainstorm_channel=env.get("MM_BRAINSTORM_CHANNEL", "brainstorm").strip() or "brainstorm",
            brainstorm_runs_per_hour=_int_between(env, "BRAINSTORM_RUNS_PER_HOUR", 30, 1, 200),
            brainstorm_max_turns=_int_between(env, "BRAINSTORM_MAX_TURNS", 10, 2, 10),
            mm_public_url=env.get("MM_PUBLIC_URL", "https://chat.tail8e3e5b.ts.net").strip().rstrip("/"),
            app_channels=env.get("APP_CHANNELS", "true").strip().lower() not in ("0", "false", "no", "off"),
            mcp_python=env.get("MCP_PYTHON", "/opt/plane-mcp/bin/python").strip() or "/opt/plane-mcp/bin/python",
            nr_key_file=env.get("NR_KEY_FILE", "/secrets/newrelic/NEW_RELIC_API_KEY").strip(),
            nr_account_id=env.get("NEW_RELIC_ACCOUNT_ID", "").strip(),
            nr_region=(env.get("NEW_RELIC_REGION", "EU").strip().upper() or "EU"),
            sre_image=env.get("SRE_IMAGE", "").strip(),
            sre_max_turns=_int_between(env, "SRE_MAX_TURNS", 40, 5, 80),
            sre_on_alerts=env.get("SRE_ON_ALERTS", "false").strip().lower() in ("1", "true", "yes", "on"),
        )

    def secret_values(self) -> list[str]:
        """Values the log filter must never let through."""
        return [
            self.mm_bot_token,
            self.action_secret.decode(),
            self.plane_api_key,
            self.claude_oauth_token,
            self.ingest_token or "",
        ]


def _int_between(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name, "").strip()
    try:
        v = int(raw) if raw else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not lo <= v <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return v


def _ingest_token(value: str) -> str | None:
    value = value.strip()
    if not value:
        return None
    if len(value) < 32:
        raise ConfigError("CATCHER_INGEST_TOKEN must be at least 32 characters")
    return value
