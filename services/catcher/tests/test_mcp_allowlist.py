import json

from catcher import plane_mcp_tools as t
from catcher.claude_runner import DISALLOWED_BUILTIN_TOOLS, build_argv, build_mcp_config, child_env


def test_allowlist_is_read_only():
    for name in t.READ_ONLY_TOOLS:
        assert name.split("_", 1)[0] in ("list", "get", "search", "retrieve"), name
        for marker in ("create", "update", "delete", "add", "remove", "archive", "transfer", *t.WRITE_MARKERS):
            assert marker not in name, f"{name} contains {marker}"


def test_allowlist_names_exist_in_pinned_server():
    assert set(t.READ_ONLY_TOOLS) <= t.ALL_TOOLS
    assert t.PLANE_MCP_VERSION == "0.2.10"


def test_everything_else_is_denied():
    denied = set(t.denied_tool_names())
    allowed = set(t.allowed_tool_names())
    assert not denied & allowed
    assert denied | allowed == {t.mcp_name(n) for n in t.ALL_TOOLS}
    assert t.mcp_name("delete_project") in denied and t.mcp_name("create_work_item") in denied


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_claude_argv():
    argv = build_argv("claude", "/tmp/x/mcp.json")
    assert argv[:2] == ["claude", "-p"]
    assert _flag(argv, "--output-format") == "json"
    json.loads(_flag(argv, "--json-schema"))
    assert "--strict-mcp-config" in argv and _flag(argv, "--mcp-config") == "/tmp/x/mcp.json"
    assert _flag(argv, "--tools") == ""
    assert _flag(argv, "--allowedTools").split(",") == t.allowed_tool_names()
    disallowed = _flag(argv, "--disallowedTools").split(",")
    assert set(DISALLOWED_BUILTIN_TOOLS) <= set(disallowed)
    assert {"Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit", "Task"} <= set(disallowed)
    assert _flag(argv, "--permission-mode") == "dontAsk"
    assert _flag(argv, "--permission-prompts") == "none"
    assert _flag(argv, "--setting-sources") == ""
    assert int(_flag(argv, "--max-turns")) <= 10
    assert "untrusted" in _flag(argv, "--system-prompt").lower()
    assert "--dangerously-skip-permissions" not in argv and "bypassPermissions" not in argv


def test_mcp_config_has_only_plane():
    cfg = build_mcp_config(plane_mcp_bin="plane-mcp-server", plane_base_url="http://p", plane_api_key="k", workspace="homie")
    assert list(cfg["mcpServers"]) == ["plane"]
    srv = cfg["mcpServers"]["plane"]
    assert srv["type"] == "stdio" and srv["command"] == "plane-mcp-server"
    assert set(srv["env"]) >= {"PLANE_API_KEY", "PLANE_WORKSPACE_SLUG", "PLANE_BASE_URL"}


def test_child_env_has_no_other_secrets(monkeypatch):
    monkeypatch.setenv("PLANE_API_KEY", "leak")
    monkeypatch.setenv("MM_BOT_TOKEN", "leak")
    env = child_env("tok")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert "leak" not in env.values()
