"""Administration-endpoint guard: static scan of the package + runtime refusal."""

import ast
import asyncio
import pathlib

import pytest

from catcher import github as ghmod
from catcher.github import BRANCH_PROTECTION, REPO_SETTINGS, EndpointNotAllowed, GitHub
from tests.fakes import ORG, PRIVATE_KEY_PEM, FakeServices

PKG = pathlib.Path(ghmod.__file__).parent

FORBIDDEN_FRAGMENTS = (
    "/collaborators",
    "/hooks",
    "/keys",
    "/actions/permissions",
    "/rulesets",
    "/transfer",
    "/teams",
    "/invitations",
    "/environments",
    "/secrets",
    "/vulnerability-alerts",
    "/topics",
    "/pages",
    "/autolinks",
    "/interaction-limits",
    "/deployments",
    "/forks",
)

EXPECTED_ADMIN = {
    ("POST", "/repos/{owner}/{repo}/generate"),
    ("POST", "/orgs/{org}/repos"),
    ("PATCH", "/repos/{owner}/{repo}"),
    ("PUT", "/repos/{owner}/{repo}/branches/main/protection"),
}


def _strings(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.value


def test_package_references_no_other_admin_endpoints():
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for s in _strings(tree):
            # GitHub-shaped strings anywhere, and every string in github.py
            # (Mattermost's own /teams/... API is not a GitHub endpoint).
            if path.name != "github.py" and "/repos" not in s and "/orgs" not in s:
                continue
            for frag in FORBIDDEN_FRAGMENTS:
                assert frag not in s, f"{path.name} references {frag!r}: {s!r}"
            assert not ("DELETE" in s and "/repos" in s), f"{path.name}: {s!r}"


def test_no_delete_method_anywhere_in_github_calls():
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "request" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant):
                    assert first.value in ("GET", "POST", "PATCH", "PUT"), f"{path.name}: {first.value}"


def test_admin_endpoints_are_exactly_the_four():
    admin = {(e.method, e.template) for e in ghmod.ENDPOINTS if e.admin}
    assert admin == EXPECTED_ADMIN
    for e in ghmod.ENDPOINTS:
        assert e.method != "DELETE"


def test_patch_and_protection_only_called_from_new_repo_path():
    tree = ast.parse((PKG / "github.py").read_text())
    callers = {}
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "request" and node.args:
                    a0 = node.args[0]
                    if isinstance(a0, ast.Constant) and a0.value in ("PATCH", "PUT"):
                        callers.setdefault(fn.name, set()).add(a0.value)
    assert callers == {"configure_new_repo": {"PATCH", "PUT"}}
    # and configure_new_repo / generate are only used from the bot's repo-creation flow
    bot_src = (PKG / "bot.py").read_text()
    tree = ast.parse(bot_src)
    users = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            for node in ast.walk(fn):
                if isinstance(node, ast.Attribute) and node.attr in ("configure_new_repo", "generate_repo_from_template"):
                    users.add(fn.name)
    assert users == {"_create_repo"}


def _gh():
    return GitHub(FakeServices(), org=ORG, app_id="1", installation_id="2", private_key_pem=PRIVATE_KEY_PEM)


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("DELETE", f"/repos/{ORG}/agentic-demo", None),
        ("PUT", f"/repos/{ORG}/agentic-demo/collaborators/evil", {}),
        ("POST", f"/repos/{ORG}/agentic-demo/hooks", {}),
        ("POST", f"/repos/{ORG}/agentic-demo/keys", {}),
        ("PUT", f"/repos/{ORG}/agentic-demo/actions/permissions", {}),
        ("POST", f"/repos/{ORG}/agentic-demo/rulesets", {}),
        ("POST", f"/repos/{ORG}/agentic-demo/transfer", {"new_owner": "x"}),
        ("PUT", f"/orgs/{ORG}/teams/x/repos/{ORG}/y", {}),
        ("DELETE", f"/repos/{ORG}/agentic-demo/branches/main/protection", None),
        ("PATCH", f"/repos/{ORG}/agentic-demo", {"private": True}),
        ("PATCH", f"/repos/{ORG}/agentic-demo", {**REPO_SETTINGS, "archived": True}),
        ("PUT", f"/repos/{ORG}/agentic-demo/branches/main/protection", {**BRANCH_PROTECTION, "enforce_admins": True}),
        ("POST", f"/repos/{ORG}/other-template/generate", {}),
        ("POST", "/repos/someone-else/app-template/generate", {}),
        ("POST", "/repos/someone-else/repo/issues", {}),
        ("GET", "/repos/kiukairor/homie", None),
        ("POST", f"/orgs/{ORG}/repos", {"name": "x", "private": True}),
        ("GET", f"/repos/{ORG}/..", None),
        ("GET", "/user", None),
        ("POST", f"/repos/{ORG}/release/issues", {"title": "x"}),
        ("GET", f"/repos/{ORG}/papl", None),
        ("POST", f"/repos/{ORG}/PAPL/labels", {}),
        ("GET", f"/repos/{ORG}/homelab-gitops/pulls", None),
    ],
)
def test_request_refuses_non_allowlisted(method, path, body):
    gh = _gh()
    with pytest.raises(EndpointNotAllowed):
        asyncio.run(gh.request(method, path, json=body))
    assert gh.transport.calls == []  # refused before any network call


def test_request_allows_allowlisted():
    gh = _gh()
    asyncio.run(gh.request("PATCH", f"/repos/{ORG}/agentic-demo", json=dict(REPO_SETTINGS)))
    asyncio.run(gh.request("PUT", f"/repos/{ORG}/agentic-demo/branches/main/protection", json=dict(BRANCH_PROTECTION)))
    paths = [c.path for c in gh.transport.calls]
    assert paths[0] == "/app/installations/2/access_tokens"
    assert paths[1:] == [f"/repos/{ORG}/agentic-demo", f"/repos/{ORG}/agentic-demo/branches/main/protection"]


def test_installation_token_cached_and_jwt_used_only_for_token():
    gh = _gh()
    for _ in range(3):
        asyncio.run(gh.repo_exists("agentic-demo"))
    token_calls = [c for c in gh.transport.calls if c.path.endswith("/access_tokens")]
    assert len(token_calls) == 1
    assert token_calls[0].headers["Authorization"].startswith("Bearer ")
    others = [c for c in gh.transport.calls if not c.path.endswith("/access_tokens")]
    assert all(c.headers["Authorization"].startswith("token ghs_") for c in others)
