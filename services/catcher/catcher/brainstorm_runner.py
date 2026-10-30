"""Headless Claude Code runs for #brainstorm (chat replies and feature-request drafts).

Same isolation as claude_runner (empty temp cwd, minimal env with only the OAuth token, no settings
files, no session persistence, strict MCP config, dontAsk) with a different tool set:

--input-format stream-json   one user message with text + image content blocks (Mattermost attachments)
--output-format stream-json  (+ --verbose, required) events; the last "result" event has structured_output
--tools WebSearch,WebFetch   the ONLY built-in tools (no Bash/Read/Edit/Write/...)
MCP: plane (read-only tool allowlist, as in #ideas) + github_ro (read-only server, down-scoped token)
--max-turns                  BRAINSTORM_MAX_TURNS (10) for chat, DRAFT_MAX_TURNS (6) for drafts

Flags checked against `claude --help` of Claude Code 2.1.312 in the catcher image (2026-10-30).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from typing import Any

from . import plane_mcp_tools
from .claude_runner import ClaudeError, build_mcp_config, child_env

log = logging.getLogger(__name__)

BRAINSTORM_MAX_TURNS = 10
DRAFT_MAX_TURNS = 6
RUN_TIMEOUT_S = 420
GITHUB_RO_SERVER = "github_ro"
GITHUB_RO_TOOLS = ("list_repos", "list_dir", "read_file", "list_issues", "get_issue")
BUILTIN_TOOLS = ("WebSearch", "WebFetch")
DENIED_BUILTINS = ("Bash", "Edit", "Write", "Read", "Glob", "Grep", "NotebookEdit", "Task", "TodoWrite")
# The MCP server file ships in the image next to the catcher package.
GITHUB_RO_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "github_ro_mcp.py")


def allowed_tools() -> list[str]:
    return [*BUILTIN_TOOLS, *plane_mcp_tools.allowed_tool_names(), *(f"mcp__{GITHUB_RO_SERVER}__{t}" for t in GITHUB_RO_TOOLS)]


def build_argv(claude_bin: str, mcp_path: str, system_prompt: str, schema: dict, max_turns: int, model: str | None) -> list[str]:
    argv = [
        claude_bin,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--json-schema", json.dumps(schema, separators=(",", ":")),
        "--strict-mcp-config",
        "--mcp-config", mcp_path,
        "--tools", ",".join(BUILTIN_TOOLS),
        "--allowedTools", ",".join(allowed_tools()),
        "--disallowedTools", ",".join([*DENIED_BUILTINS, *plane_mcp_tools.denied_tool_names()]),
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--setting-sources", "",
        "--no-session-persistence",
        "--max-turns", str(max_turns),
        "--system-prompt", system_prompt,
    ]  # fmt: skip
    if model:
        argv += ["--model", model]
    return argv


def user_message(text: str, images: list[tuple[str, str]]) -> bytes:
    """One stream-json user message: image blocks (media_type, base64) then the text."""
    content: list[dict[str, Any]] = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}} for mt, data in images]
    content.append({"type": "text", "text": text})
    return (json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n").encode()


def parse_stream(stdout: bytes, returncode: int | None) -> tuple[Any, list[str]]:
    """Return (structured_output, tool names used) from stream-json output."""
    tools: list[str] = []
    result: dict | None = None
    for line in stdout.decode(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tools.append(str(c.get("name")))
        elif ev.get("type") == "result":
            result = ev
    if result is None:
        raise ClaudeError(f"claude produced no result event (exit {returncode})")
    if result.get("is_error") or result.get("subtype") not in (None, "success"):
        raise ClaudeError(f"claude run failed: {result.get('subtype')} (exit {returncode})")
    if "structured_output" not in result:
        raise ClaudeError("claude returned no structured_output")
    return result["structured_output"], [t for t in tools if t != "StructuredOutput"]


class BrainstormRunner:
    def __init__(self, *, claude_bin: str, plane_mcp_bin: str, plane_base_url: str, plane_api_key: str, workspace: str,
                 oauth_token: str, github_org: str, mcp_python: str, model: str | None = None) -> None:  # fmt: skip
        self.claude_bin = claude_bin
        self._plane = build_mcp_config(
            plane_mcp_bin=plane_mcp_bin, plane_base_url=plane_base_url, plane_api_key=plane_api_key, workspace=workspace
        )
        self._token = oauth_token
        self.github_org = github_org
        self.mcp_python = mcp_python
        self.model = model

    def mcp_config(self, github_token: str, repos: list[str]) -> dict:
        cfg = json.loads(json.dumps(self._plane))
        cfg["mcpServers"][GITHUB_RO_SERVER] = {
            "type": "stdio",
            "command": self.mcp_python,
            "args": [GITHUB_RO_SCRIPT],
            "env": {"GITHUB_TOKEN": github_token, "GITHUB_ORG": self.github_org, "ALLOWED_REPOS": ",".join(repos)},
        }
        return cfg

    async def run(self, *, system_prompt: str, text: str, images: list[tuple[str, str]], schema: dict, max_turns: int,
                  github_token: str, repos: list[str]) -> tuple[Any, list[str]]:  # fmt: skip
        workdir = tempfile.mkdtemp(prefix="catcher-bs-")  # empty cwd: no CLAUDE.md, no project settings
        try:
            os.chmod(workdir, 0o700)
            cfg_path = os.path.join(workdir, "mcp.json")
            fd = os.open(cfg_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump(self.mcp_config(github_token, repos), fh)
            proc = await asyncio.create_subprocess_exec(
                *build_argv(self.claude_bin, cfg_path, system_prompt, schema, max_turns, self.model),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=child_env(self._token),
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(user_message(text, images)), RUN_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise ClaudeError("claude run timed out") from exc
            if proc.returncode and not out:
                log.warning("brainstorm claude exited %s with no output (%d bytes stderr)", proc.returncode, len(err))
            return parse_stream(out, proc.returncode)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
