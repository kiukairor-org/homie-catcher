"""New Relic -> catcher: forward open NR issues for triage (run by a CronJob every 5 min).

    python -m catcher.nr_triage            # query NerdGraph, POST open issues to the catcher
    python -m catcher.nr_triage --simulate # POST one fake issue (demo), no New Relic call

Outbound only. Query shape verified against "NerdGraph tutorial: Issue and alert event query
APIs" (https://docs.newrelic.com/docs/apis/nerdgraph/examples/nerdgraph-issues-api-via-github/):
``actor { account(id) { aiIssues { issues(filter: FilterIssues, cursor) { issues {...} nextCursor } } } }``
with ``FilterIssues { entityGuids, entityTypes, ids, priority, states }`` and the Issue fields
issueId, title ([String!]!), priority, state, entityNames, entityGuids, activatedAt, createdAt
(EpochMilliseconds). Without a timeWindow NerdGraph returns the last 24 hours, which is enough for
a 5-minute poll (the catcher de-duplicates by issue id).

Filter values are inlined as GraphQL literals (validated first) so we don't depend on the
schema's internal input-type names.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from .logsetup import setup_logging

log = logging.getLogger("catcher.nr_triage")

ENDPOINTS = {"US": "https://api.newrelic.com/graphql", "EU": "https://api.eu.newrelic.com/graphql"}
CONSOLE = {"US": "https://one.newrelic.com", "EU": "https://one.eu.newrelic.com"}
DEFAULT_INGEST_URL = "http://catcher.catcher.svc.cluster.local:8080/ingest/alert"
OPEN_STATES = ("ACTIVATED", "CREATED")
MAX_PAGES = 5
_GUID = re.compile(r"^[A-Za-z0-9+/=_-]{8,200}$")


class NRConfigError(ValueError):
    pass


def build_query(account_id: int, entity_guids: list[str] | None = None, cursor: str | None = None) -> str:
    if not isinstance(account_id, int) or account_id <= 0:
        raise NRConfigError("NEW_RELIC_ACCOUNT_ID must be a positive integer")
    parts = [f"states: [{', '.join(OPEN_STATES)}]"]
    if entity_guids:
        for g in entity_guids:
            if not _GUID.match(g):
                raise NRConfigError(f"invalid entity GUID {g!r}")
        parts.append("entityGuids: [" + ", ".join(json.dumps(g) for g in entity_guids) + "]")
    args = f"filter: {{{', '.join(parts)}}}"
    if cursor:
        args += f", cursor: {json.dumps(cursor)}"
    fields = "issueId title priority state entityNames entityGuids activatedAt createdAt"
    return f"{{ actor {{ account(id: {account_id}) {{ aiIssues {{ issues({args}) {{ issues {{ {fields} }} nextCursor }} }} }} }} }}"


def _iso(ms: Any) -> str | None:
    if isinstance(ms, (int, float)) and ms > 0:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _strs(v: Any, limit: int = 20) -> list[str]:
    return [str(x) for x in v[:limit]] if isinstance(v, list) else []


def parse_issues(resp: Mapping[str, Any], *, account_id: int, region: str = "US") -> tuple[list[dict], str | None]:
    """NerdGraph response -> ([issue dict], nextCursor). Raises on GraphQL errors."""
    if resp.get("errors"):
        msgs = "; ".join(str(e.get("message", e))[:200] for e in resp["errors"][:3])
        raise RuntimeError(f"NerdGraph errors: {msgs}")
    block = (((resp.get("data") or {}).get("actor") or {}).get("account") or {}).get("aiIssues") or {}
    issues = block.get("issues") or {}
    out = []
    for raw in issues.get("issues") or []:
        if not isinstance(raw, dict) or not raw.get("issueId") or raw.get("state") not in OPEN_STATES:
            continue
        titles = _strs(raw.get("title"), 5)
        out.append(
            {
                "issue_id": str(raw["issueId"]),
                "title": titles[0] if titles else "New Relic issue",
                "priority": str(raw.get("priority") or "UNKNOWN"),
                "entity_names": _strs(raw.get("entityNames")),
                "entity_guids": _strs(raw.get("entityGuids")),
                "activated_at": _iso(raw.get("activatedAt")) or _iso(raw.get("createdAt")),
                "url": f"{CONSOLE.get(region, CONSOLE['US'])}/alerts-ai?account={account_id}",
            }
        )
    return out, issues.get("nextCursor") or None


def _post_json(url: str, body: Any, headers: dict[str, str], timeout: float = 30) -> Any:
    if not url.startswith(("https://", "http://")):
        raise NRConfigError("URL must be http(s)")
    req = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read().decode() or "null")


def fetch_open_issues(
    *, api_key: str, account_id: int, region: str, entity_guids: list[str] | None, post: Callable = _post_json
) -> list[dict]:
    url = ENDPOINTS[region]
    found: list[dict] = []
    cursor = None
    for _ in range(MAX_PAGES):
        resp = post(url, {"query": build_query(account_id, entity_guids, cursor)}, {"API-Key": api_key})
        page, cursor = parse_issues(resp, account_id=account_id, region=region)
        found += page
        if not cursor:
            break
    return found


def simulated_issue(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "issue_id": f"simulated-{uuid.uuid4()}",
        "title": "Simulated: error rate > 5% for 5 minutes on 'agentic-demo'",
        "priority": "HIGH",
        "entity_names": ["agentic-demo"],
        "entity_guids": [],
        "activated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "url": "https://one.newrelic.com/alerts-ai",
        "simulated": True,
    }


def _post_with_retry(post: Callable, url: str, body: Any, headers: dict[str, str], attempts: int = 6, delay: float = 5.0) -> Any:
    """k3s's NetworkPolicy controller admits a new pod's IP a few seconds after it starts, so the first
    connection from a fresh Job pod can be refused. Retry connection errors only (never HTTP errors)."""
    for attempt in range(1, attempts + 1):
        try:
            return post(url, body, headers)
        except urllib.error.HTTPError:
            raise
        except OSError as exc:  # URLError is an OSError
            if attempt == attempts:
                raise
            log.warning("ingest not reachable yet (%s), retry %d/%d in %.0fs", exc, attempt, attempts - 1, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None, post: Callable = _post_json) -> int:
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(prog="python -m catcher.nr_triage")
    ap.add_argument("--simulate", action="store_true", help="send one fake issue to the catcher; don't call New Relic")
    args = ap.parse_args(argv)

    ingest_url = env.get("CATCHER_INGEST_URL", DEFAULT_INGEST_URL)
    token = env.get("CATCHER_INGEST_TOKEN", "")
    api_key = env.get("NEW_RELIC_API_KEY", "")
    setup_logging([token, api_key], env.get("LOG_LEVEL", "INFO"))
    if not token:
        log.error("CATCHER_INGEST_TOKEN is not set")
        return 2

    if args.simulate:
        issues = [simulated_issue()]
    else:
        region = env.get("NEW_RELIC_REGION", "US").upper()
        if region not in ENDPOINTS:
            log.error("NEW_RELIC_REGION must be US or EU")
            return 2
        try:
            account_id = int(env.get("NEW_RELIC_ACCOUNT_ID", ""))
        except ValueError:
            log.error("NEW_RELIC_ACCOUNT_ID must be an integer")
            return 2
        if not api_key:
            log.error("NEW_RELIC_API_KEY is not set")
            return 2
        guids = [g.strip() for g in env.get("NR_ENTITY_GUIDS", "").split(",") if g.strip()] or None
        issues = fetch_open_issues(api_key=api_key, account_id=account_id, region=region, entity_guids=guids, post=post)

    if not issues:
        log.info("no open issues")
        return 0
    result = _post_with_retry(post, ingest_url, {"issues": issues}, {"X-Catcher-Ingest-Token": token})
    log.info("sent %d issue(s): %s", len(issues), json.dumps(result)[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
