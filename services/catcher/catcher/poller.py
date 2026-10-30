"""Outbound GitHub poller: PR/CI/review/preview/merge milestones for tracked issues.

GitHub-hosted runners can't reach the in-cluster Mattermost, so the bot polls (every 60 s,
backing off when ``X-RateLimit-Remaining`` gets low) and announces each milestone once.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .github import REVIEWER_LOGIN

log = logging.getLogger(__name__)

# Only these accounts may post the "Preview: https://..." comment we relay (repos are public:
# anyone can comment, and we must not relay a stranger's link as our preview).
PREVIEW_COMMENT_AUTHORS = frozenset({"kiu-homie-bot[bot]", "github-actions[bot]"})
_PREVIEW = re.compile(r"Preview:\s*(https://[A-Za-z0-9.-]+(?::\d+)?(?:/[^\s<>()\[\]`]*)?)")


def pr_matches_issue(pr: dict, issue: int) -> bool:
    body = pr.get("body") or ""
    head = (pr.get("head") or {}).get("ref") or ""
    return bool(re.search(rf"#{issue}(?!\d)", body)) or head.startswith(f"claude/issue-{issue}-")


class Poller:
    def __init__(self, catcher: Any) -> None:
        self.c = catcher
        self.db, self.gh, self.plane = catcher.db, catcher.gh, catcher.plane

    async def loop(self, interval: float = 60.0) -> None:
        while True:
            try:
                if self.gh.rate_limited():
                    log.warning("GitHub rate limit low (%s left); skipping poll", self.gh.rate_remaining)
                else:
                    await self.poll_once()
            except Exception:
                log.exception("poll failed")
            await asyncio.sleep(interval)

    async def poll_once(self) -> None:
        pulls_cache: dict[str, list[dict]] = {}
        for item in self.db.tracked_items():
            if self.gh.rate_limited():
                return
            repo = item["repo"]
            if repo not in pulls_cache:
                pulls_cache[repo] = await self.gh.list_pulls(repo)
            for pr in pulls_cache[repo]:
                if pr_matches_issue(pr, item["gh_issue_number"]):
                    await self.track_pr(item, pr)

    async def _plane(self, item, key: str) -> None:
        try:
            proj = await self.c.plane_project(item["repo"])
            await self.plane.set_state(proj, item["plane_item_id"], key)
        except Exception:
            log.exception("Plane state %s failed for item %s", key, item["id"])

    async def track_pr(self, item, pr: dict) -> None:
        iid, repo, n = item["id"], item["repo"], pr["number"]
        sha = (pr.get("head") or {}).get("sha")
        self.db.upsert_pr(repo, n, iid, sha, pr.get("state", ""))
        url = pr.get("html_url", "")

        if self.db.claim_milestone(iid, f"pr{n}:opened"):
            await self.c.announce(iid, f"🔧 PR #{n} opened: {url}")
            await self._plane(item, "in_progress")

        if sha:
            for run in await self.gh.check_runs(repo, sha, "ci"):
                if run.get("name") == "ci" and run.get("status") == "completed":
                    concl = run.get("conclusion") or "unknown"
                    if self.db.claim_milestone(iid, f"pr{n}:ci:{sha}:{concl}"):
                        icon = "✅" if concl == "success" else "❌"
                        await self.c.announce(iid, f"{icon} CI `{concl}` on PR #{n} ({sha[:7]})")
                    break

        for review in await self.gh.list_reviews(repo, n):
            if (review.get("user") or {}).get("login") != REVIEWER_LOGIN:
                continue
            state = review.get("state")
            if state in ("APPROVED", "CHANGES_REQUESTED") and self.db.claim_milestone(iid, f"pr{n}:review:{review.get('id')}"):
                verdict = "approved ✅" if state == "APPROVED" else "requested changes ✏️"
                await self.c.announce(iid, f"🔍 Reviewer {verdict} on PR #{n}")
                await self._plane(item, "in_review")

        labels = {lbl.get("name") for lbl in pr.get("labels") or []}
        if "needs-human" in labels and self.db.claim_milestone(iid, f"pr{n}:needs-human"):
            await self.c.announce(iid, f"🙋 {self.c.cfg.owner_mention} PR #{n} needs a human: {url}")

        for comment in await self.gh.list_issue_comments(repo, n):
            if (comment.get("user") or {}).get("login") not in PREVIEW_COMMENT_AUTHORS:
                continue
            m = _PREVIEW.search(comment.get("body") or "")
            if m and self.db.claim_milestone(iid, f"pr{n}:preview:{m.group(1)}"):
                await self.c.announce(iid, f"👀 Preview ready for PR #{n}: {m.group(1)}")

        if pr.get("merged_at") and self.db.claim_milestone(iid, f"pr{n}:merged"):
            await self.c.announce(iid, f"🎉 PR #{n} merged")
            await self._plane(item, "done")
            await self.on_merged(item, pr)
            self.db.transition(iid, ["approved"], "done")

    async def on_merged(self, item, pr: dict) -> None:
        """Deployed milestone hook.

        TODO(Phase 4): Argo CD deploys prod after merge. Add the "deployed" milestone here
        (e.g. poll the app's Argo CD Application sync/health or a deploy commit status) and
        announce it once with key f"pr{n}:deployed".
        """
        return None
