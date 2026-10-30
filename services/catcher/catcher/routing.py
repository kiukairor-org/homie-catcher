"""Repo routing: done by the bot from the message text, never by Claude."""

from __future__ import annotations

import re
from dataclasses import dataclass

# Never offered in "which app?" and never targeted, whatever the message says.
EXCLUDED_REPOS: frozenset[str] = frozenset({"release", "papl", "homelab-gitops", "mattermost-arm64", "homie-catcher", "app-template"})

NEW_REPO_SENTINEL = "__new__"

_WORK_ON = re.compile(r"^\s*work\s+on\s+([A-Za-z0-9._-]+)\s*:\s*(.*)$", re.I | re.S)
_NEW_IDEA = re.compile(r"^\s*new\s+idea\s*:\s*(.*)$", re.I | re.S)
_NAME_REPLY = re.compile(r"^\s*name\s*:\s*([A-Za-z0-9_. -]+?)\s*$", re.I)
REPO_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}$")
MAX_SUFFIX = 9


@dataclass(frozen=True)
class Route:
    kind: str  # "repo" | "new" | "none"
    text: str
    repo: str | None = None


def parse_message(text: str) -> Route:
    m = _NEW_IDEA.match(text)
    if m:
        return Route("new", m.group(1).strip())
    m = _WORK_ON.match(text)
    if m:
        return Route("repo", m.group(2).strip(), m.group(1))
    return Route("none", text.strip())


def parse_name_reply(text: str) -> str | None:
    m = _NAME_REPLY.match(text)
    return normalize_repo_name(m.group(1)) if m else None


def resolve_repo(name: str, available: list[str]) -> str | None:
    """Case-insensitive match against the org's repos, never an excluded one."""
    lowered = name.strip().lower()
    for repo in available:
        if repo.lower() == lowered and repo.lower() not in EXCLUDED_REPOS:
            return repo
    return None


def selectable_repos(available: list[str]) -> list[str]:
    return sorted(r for r in available if r.lower() not in EXCLUDED_REPOS)


def normalize_repo_name(name: str) -> str:
    n = name.strip().lower()
    n = re.sub(r"[\s_.]+", "-", n)
    n = re.sub(r"-{2,}", "-", n).strip("-")
    return n


def valid_repo_name(name: str) -> bool:
    return bool(REPO_NAME_RE.match(name)) and not name.endswith("-") and name not in EXCLUDED_REPOS


def candidate_names(suggested: str) -> list[str]:
    base = normalize_repo_name(suggested)
    if not valid_repo_name(base):
        return []
    out = [base]
    for i in range(2, MAX_SUFFIX + 1):
        cand = f"{base[: 39 - len(str(i)) - 1]}-{i}"
        if valid_repo_name(cand):
            out.append(cand)
    return out


async def pick_free_repo_name(github, suggested: str | None) -> str | None:
    """First free valid name among suggested, suggested-2, ... suggested-9; None -> ask the owner."""
    if not suggested:
        return None
    for cand in candidate_names(suggested):
        if not await github.repo_exists(cand):
            return cand
    return None
