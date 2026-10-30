"""The catcher's orchestration: chat events -> Claude spec -> Plane -> buttons -> GitHub.

Hard rules implemented here:
- Only posts in #ideas from OWNER_USER_IDS are handled (never the bot's own posts).
- Repo routing comes from the message text and button clicks, never from Claude.
- Claude's output is used only after strict schema validation, and only its data fields
  (title, story, criteria, labels, question, suggested repo name) - never as actions on
  anything other than this item. All writes are done here, with fixed code paths.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

from . import buttons
from .claude_runner import MAX_CLARIFY_ROUNDS, ClaudeError, RunContext, build_prompt
from .config import Config
from .db import DB
from .github import GitHub
from .mattermost import Mattermost
from .plane import Plane, Project
from .routing import (
    NEW_REPO_SENTINEL,
    Route,
    parse_message,
    parse_name_reply,
    pick_free_repo_name,
    resolve_repo,
    selectable_repos,
    valid_repo_name,
)
from .spec import Spec, SpecError, validate_spec
from .text import clip, defang, issue_body, md_escape, spec_html, spec_markdown

log = logging.getLogger(__name__)

REPO_CACHE_S = 300
MAX_REPO_BUTTONS = 24
NOT_ALLOWED = {"ephemeral_text": "not allowed"}
MAX_ALERTS_PER_REQUEST = 50
_NR_ISSUE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")
NR_CRITERIA = (
    "The root cause of the New Relic issue is identified and fixed",
    "The alert condition no longer fires for the same cause",
    "A regression test covers the failure where feasible",
)


class Catcher:
    def __init__(self, cfg: Config, db: DB, mm: Mattermost, plane: Plane, gh: GitHub, claude: Any,
                 clock: Callable[[], float] = time.time) -> None:  # fmt: skip
        self.cfg, self.db, self.mm, self.plane, self.gh, self.claude = cfg, db, mm, plane, gh, claude
        self.clock = clock
        self.ideas_channel_id: str | None = None
        self.agent_log_channel_id: str | None = None
        self._wake = asyncio.Event()
        self._repos: tuple[float, list[str]] = (0.0, [])
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ setup
    async def startup(self) -> None:
        await self.mm.me()
        self.ideas_channel_id = await self.mm.channel_id(self.cfg.mm_team, self.cfg.ideas_channel)
        self.agent_log_channel_id = await self.mm.channel_id(self.cfg.mm_team, self.cfg.agent_log_channel)
        n = self.db.requeue_interrupted()
        if n:
            log.info("re-queued %d interrupted runs", n)
            self._wake.set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Wait for background tasks (used by tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def org_repos(self) -> list[str]:
        ts, repos = self._repos
        if not repos or self.clock() - ts > REPO_CACHE_S:
            repos = await self.gh.list_installation_repos()
            self._repos = (self.clock(), repos)
        return repos

    # ------------------------------------------------------------ chat input
    def accepts(self, post: dict) -> bool:
        return (
            post.get("channel_id") == self.ideas_channel_id
            and post.get("user_id") in self.cfg.owner_user_ids
            and post.get("user_id") != self.mm.bot_user_id
            and not post.get("type")  # system messages
            and not (post.get("props") or {}).get("from_bot")
            and not (post.get("props") or {}).get("from_webhook")
            and isinstance(post.get("message"), str)
            and isinstance(post.get("id"), str)
        )

    async def on_event(self, event: dict) -> None:
        if event.get("event") != "posted":
            return
        try:
            post = json.loads(event.get("data", {}).get("post", ""))
        except (TypeError, ValueError):
            return
        await self.handle_post(post)

    async def handle_post(self, post: dict) -> None:
        if not self.accepts(post) or not self.db.mark_seen(post["id"]):
            return
        try:
            await self.mm.react(post["id"], "eyes")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not react: %s", exc)
        root = post.get("root_id") or post["id"]
        if post.get("root_id"):
            await self.handle_reply(root, post["message"])
        else:
            self.db.upsert_thread(root, post["channel_id"])
            await self.route_new_item(root, parse_message(post["message"]))

    async def say(self, root: str, message: str, attachments: list | None = None) -> None:
        th = self.db.thread(root)
        channel = th["channel_id"] if th else self.ideas_channel_id
        await self.mm.post(channel, message, root_id=root, attachments=attachments)

    async def route_new_item(self, root: str, route: Route) -> None:
        if not route.text:
            await self.say(root, "I need a description of the idea, e.g. `work on <repo>: <idea>` or `new idea: <idea>`.")
            return
        if route.kind == "new":
            self.db.set_thread_repo(root, None, True)
            self.enqueue(self.db.create_item(root, route.text, "queued"))
            return
        if route.kind == "repo":
            repo = resolve_repo(route.repo or "", await self.org_repos())
            if repo:
                self.db.set_thread_repo(root, repo, False)
                self.enqueue(self.db.create_item(root, route.text, "queued"))
                return
            item_id = self.db.create_item(root, route.text, "awaiting_repo")
            await self.ask_which_app(root, item_id, f"I don't know a repo named `{defang(route.repo or '')}`.")
            return
        th = self.db.thread(root)
        if th and (th["repo"] or th["is_new_repo"]):
            self.enqueue(self.db.create_item(root, route.text, "queued"))
            return
        item_id = self.db.create_item(root, route.text, "awaiting_repo")
        await self.ask_which_app(root, item_id)

    async def handle_reply(self, root: str, text: str) -> None:
        th = self.db.thread(root)
        if th is None:
            self.db.upsert_thread(root, self.ideas_channel_id or "")
            await self.route_new_item(root, parse_message(text))
            return
        item = self.db.latest_item(root)
        status = item["status"] if item else None
        if status == "clarifying":
            self.db.answer_clarification(item["id"], text.strip())
            if self.db.transition(item["id"], ["clarifying"], "queued"):
                self.enqueue(item["id"])
            return
        if status == "awaiting_name":
            await self.handle_name_reply(item, text)
            return
        if status == "awaiting_repo":
            route = parse_message(text)
            if route.kind == "new":
                self.db.set_thread_repo(root, None, True)
            else:
                repo = resolve_repo(route.repo or route.text, await self.org_repos())
                if not repo:
                    await self.ask_which_app(root, item["id"], "Pick the app with a button, or reply with its exact repo name.")
                    return
                self.db.set_thread_repo(root, repo, False)
            if self.db.transition(item["id"], ["awaiting_repo"], "queued"):
                self.enqueue(item["id"])
            return
        if status in ("queued", "running"):
            await self.say(root, "I'm still working on the previous message in this thread; post again once the spec is ready.")
            return
        # A follow-up idea in a known thread: the thread remembers its repo.
        await self.route_new_item(root, parse_message(text))

    async def ask_which_app(self, root: str, item_id: int, note: str = "") -> None:
        repos = selectable_repos(await self.org_repos())[:MAX_REPO_BUTTONS]
        secret, base = self.cfg.action_secret, self.cfg.action_base_url
        actions = [buttons.button(base, secret, r, "choose_repo", {"item": item_id, "repo": r}) for r in repos]
        actions.append(buttons.button(base, secret, "New repo", "choose_repo", {"item": item_id, "repo": NEW_REPO_SENTINEL}, "primary"))
        text = (note + " " if note else "") + "Which app?"
        await self.say(root, "", attachments=[{"text": text, "actions": actions}])

    # --------------------------------------------------------------- queue
    def enqueue(self, item_id: int) -> None:
        self.db.enqueue(item_id)
        self._wake.set()

    def rate_wait(self) -> float:
        since = self.clock() - 3600
        if self.db.runs_since(since) < self.cfg.claude_runs_per_hour:
            return 0.0
        oldest = self.db.oldest_run_start_since(since) or self.clock()
        return max(1.0, oldest + 3600 - self.clock())

    async def run_next(self) -> bool:
        """Process one queued run if allowed. Returns True if one ran."""
        run = self.db.next_queued()
        if run is None:
            return False
        if self.rate_wait() > 0:
            if self.db.claim_milestone(run["item_id"], "rate-limited"):
                item = self.db.item(run["item_id"])
                await self.say(item["root_post_id"],
                               f"⏳ Queued: the Claude limit of {self.cfg.claude_runs_per_hour} runs/hour is reached.")  # fmt: skip
            return False
        self.db.start_run(run["id"])
        try:
            await self.process_item(run["item_id"])
        except Exception:
            log.exception("processing item %s failed", run["item_id"])
        finally:
            self.db.finish_run(run["id"])
        return True

    async def worker_loop(self) -> None:
        while True:
            if await self.run_next():
                continue
            self._wake.clear()
            timeout = min(self.rate_wait() or 30.0, 30.0)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------- Claude + spec
    async def process_item(self, item_id: int) -> None:
        if not self.db.transition(item_id, ["queued"], "running"):
            return
        item = self.db.item(item_id)
        root = item["root_post_id"]
        th = self.db.thread(root)
        repo, is_new = th["repo"], bool(th["is_new_repo"])
        cached = self.db.plane_project(repo) if repo else None
        clar = [(c["question"], c["answer"]) for c in self.db.clarifications(item_id)]
        ctx = RunContext(repo, is_new, cached.identifier if cached else None, MAX_CLARIFY_ROUNDS - len(clar))
        prompt = build_prompt(item["idea"], clar, ctx)
        spec: Spec | None = None
        last_err: Exception | None = None
        for attempt in range(2):  # one retry on an invalid or failed run
            if attempt:
                self.db.record_run(item_id)  # the retry is a run too: it counts against the hourly limit
            try:
                spec = validate_spec(await self.claude.run(prompt))
                break
            except (ClaudeError, SpecError) as exc:
                last_err = exc
                log.warning("item %s: claude attempt %d failed: %s", item_id, attempt + 1, exc)
        if spec is None:
            self.db.update_item(item_id, error=str(last_err)[:500])
            self.db.transition(item_id, ["running"], "error")
            await self.say(root, "⚠️ I couldn't turn this into a valid spec (after one retry). Please rephrase and post it again.")
            return
        try:
            await self.apply_spec(item_id, spec, clarify_used=len(clar))
        except Exception as exc:
            log.exception("item %s: applying spec failed", item_id)
            self.db.update_item(item_id, error=str(exc)[:500])
            self.db.transition(item_id, ["running"], "error")
            await self.say(root, "⚠️ Something failed while creating the Plane item. See the catcher logs.")

    async def apply_spec(self, item_id: int, spec: Spec, clarify_used: int) -> None:
        item = self.db.item(item_id)
        root = item["root_post_id"]
        th = self.db.thread(root)
        repo, is_new = th["repo"], bool(th["is_new_repo"])
        self.db.update_item(item_id, spec_json=json.dumps(spec.raw))
        if spec.action == "clarify":
            if clarify_used >= MAX_CLARIFY_ROUNDS:
                self.db.transition(item_id, ["running"], "error")
                await self.say(root, "⚠️ Still too vague after the clarifying questions. Please post a more detailed idea.")
                return
            self.db.add_clarification(item_id, spec.question or "")
            self.db.transition(item_id, ["running"], "clarifying")
            await self.say(root, f"❓ {defang(spec.question or '')}\n\n_Reply in this thread._")
            return
        if spec.action == "choose_repo" and not repo and not is_new:
            self.db.transition(item_id, ["running"], "awaiting_repo")
            await self.ask_which_app(root, item_id)
            return
        # "create" (Claude's target_repo is ignored: routing is the bot's job)
        if is_new:
            suggested = spec.new_repo.name if spec.new_repo else None
            desc = (spec.new_repo.description if spec.new_repo else spec.title)[:350]
            self.db.update_item(item_id, new_repo_desc=desc)
            name = await pick_free_repo_name(self.gh, suggested)
            if not name:
                self.db.transition(item_id, ["running"], "awaiting_name")
                await self.say(
                    root,
                    f"The suggested repo name `{defang(suggested or '')}` is invalid or taken. "
                    "Reply with `name: <repo-name>` (lowercase, digits, hyphens; 2-39 chars).",
                )
                return
            self.db.update_item(item_id, new_repo_name=name)
            await self.create_triage(item_id, spec, name, from_status="running")
        else:
            self.db.update_item(item_id, repo=repo)
            await self.create_triage(item_id, spec, repo, from_status="running")

    async def handle_name_reply(self, item, text: str) -> None:
        root = item["root_post_id"]
        name = parse_name_reply(text)
        if not name or not valid_repo_name(name) or await self.gh.repo_exists(name):
            await self.say(root, "That name is invalid or taken. Reply with `name: <repo-name>`.")
            return
        if not self.db.transition(item["id"], ["awaiting_name"], "running"):
            return
        self.db.update_item(item["id"], new_repo_name=name)
        spec = validate_spec(json.loads(item["spec_json"]))
        await self.create_triage(item["id"], spec, name, from_status="running")

    async def plane_project(self, key: str) -> Project:
        proj = await self.plane.ensure_project(key, self.db.plane_project(key))
        self.db.save_plane_project(proj)
        return proj

    async def create_triage(self, item_id: int, spec: Spec, plane_key: str, from_status: str) -> None:
        item = self.db.item(item_id)
        root = item["root_post_id"]
        proj = await self.plane_project(plane_key)
        wi = await self.plane.create_work_item(
            proj, title=spec.title, description_html=spec_html(spec, item["idea"]), labels=list(spec.labels)
        )
        self.db.update_item(item_id, plane_project_id=proj.project_id, plane_item_id=wi["id"], plane_seq=wi.get("sequence_id"))
        self.db.transition(item_id, [from_status], "triage")
        url = self.plane.item_url(proj.project_id, wi["id"])
        if self.db.thread(root)["is_new_repo"]:
            target = f"new repo `{item['new_repo_name'] or plane_key}` (needs its own approval)"
        else:
            target = f"`{plane_key}`"
        secret, base = self.cfg.action_secret, self.cfg.action_base_url
        actions = [
            buttons.button(base, secret, "Approve", "approve", {"item": item_id}, "good"),
            buttons.button(base, secret, "Reject", "reject", {"item": item_id}, "danger"),
        ]
        await self.say(
            root,
            f"{spec_markdown(spec)}\n\nTarget: {target}\nPlane ({proj.identifier}-{wi.get('sequence_id')}, Triage): {url}",
            attachments=[{"text": "Approve to hand this to the agent.", "actions": actions}],
        )

    # ------------------------------------------------------------- buttons
    async def handle_action(self, name: str, payload: Any) -> dict:
        if name not in buttons.ACTION_NAMES or not isinstance(payload, dict):
            return NOT_ALLOWED
        ctx = buttons.verify(self.cfg.action_secret, name, payload.get("context"))
        user = payload.get("user_id")
        if ctx is None or user not in self.cfg.owner_user_ids:
            log.warning("refused button %s (valid_sig=%s, user_allowed=%s)", name, ctx is not None, user in self.cfg.owner_user_ids)
            return NOT_ALLOWED
        item_id = ctx.get("item")
        if not isinstance(item_id, int) or self.db.item(item_id) is None:
            return NOT_ALLOWED
        return await getattr(self, f"_on_{name}")(item_id, ctx)

    async def _on_choose_repo(self, item_id: int, ctx: dict) -> dict:
        item = self.db.item(item_id)
        repo = ctx.get("repo")
        if repo == NEW_REPO_SENTINEL:
            chosen, is_new = None, True
        else:
            chosen = resolve_repo(str(repo), await self.org_repos())
            if not chosen:
                return {"ephemeral_text": "That repo is not available."}
            is_new = False
        if not self.db.transition(item_id, ["awaiting_repo"], "queued"):
            return {"ephemeral_text": "Already handled."}
        self.db.set_thread_repo(item["root_post_id"], chosen, is_new)
        self.enqueue(item_id)
        return {"ephemeral_text": f"OK: {'a new repo' if is_new else chosen}. Writing the spec…"}

    async def _on_approve(self, item_id: int, ctx: dict) -> dict:
        item = self.db.item(item_id)
        if item["new_repo_name"] and not item["repo"]:
            if not self.db.transition(item_id, ["triage"], "repo_pending"):
                return {"ephemeral_text": "Already handled."}
            secret, base = self.cfg.action_secret, self.cfg.action_base_url
            actions = [
                buttons.button(base, secret, "Approve repo", "approve_repo", {"item": item_id, "repo": item["new_repo_name"]}, "good"),
                buttons.button(base, secret, "Reject", "reject_repo", {"item": item_id}, "danger"),
            ]
            name = item["new_repo_name"]
            self.spawn(self.say(item["root_post_id"], "", attachments=[{
                "text": f"Create repo `kiukairor-org/{name}` (public, from app-template)?", "actions": actions}]))  # fmt: skip
            return {"ephemeral_text": "Idea approved. Repo creation needs its own approval."}
        if not self.db.transition(item_id, ["triage", "approve_failed"], "approving"):
            return {"ephemeral_text": "Already handled."}
        self.spawn(self._approve_existing(item_id))
        return {"ephemeral_text": "Approved. Creating the GitHub issue…"}

    async def _approve_existing(self, item_id: int) -> None:
        item = self.db.item(item_id)
        try:
            await self._ready_and_issue(item_id, new_app=None)
            self.db.transition(item_id, ["approving"], "approved")
        except Exception as exc:
            log.exception("approve failed for item %s", item_id)
            self.db.update_item(item_id, error=str(exc)[:500])
            self.db.transition(item_id, ["approving"], "approve_failed")
            await self.say(item["root_post_id"], "⚠️ Approve failed. Tap Approve again to retry.")

    async def _ready_and_issue(self, item_id: int, new_app: str | None) -> None:
        item = self.db.item(item_id)
        repo = item["repo"]
        proj = await self.plane_project(new_app or repo)
        await self.plane.set_state(proj, item["plane_item_id"], "ready")
        if item["gh_issue_number"] is None:
            spec = validate_spec(json.loads(item["spec_json"]))
            url = self.plane.item_url(proj.project_id, item["plane_item_id"])
            await self.gh.ensure_label(repo, "agent")
            title = f"Build v1: {spec.title}" if new_app else spec.title
            body = issue_body(spec, url, item["root_post_id"], item["plane_item_id"], new_app=new_app)
            issue = await self.gh.create_issue(repo, title, body, ["agent"])
            self.db.update_item(item_id, gh_issue_number=issue["number"], gh_issue_url=issue["html_url"])
            item = self.db.item(item_id)
        await self.announce(item_id, f"📋 GitHub issue opened: {item['gh_issue_url']}")

    async def _on_reject(self, item_id: int, ctx: dict) -> dict:
        if not self.db.transition(item_id, ["triage", "approve_failed", "repo_pending", "repo_failed", "awaiting_name"], "rejected"):
            return {"ephemeral_text": "Already handled."}
        self.spawn(self._rejected(item_id))
        return {"ephemeral_text": "Rejected."}

    async def _on_reject_repo(self, item_id: int, ctx: dict) -> dict:
        if not self.db.transition(item_id, ["repo_pending", "repo_failed"], "rejected"):
            return {"ephemeral_text": "Already handled."}
        self.spawn(self._rejected(item_id))
        return {"ephemeral_text": "Repo creation rejected."}

    async def _rejected(self, item_id: int) -> None:
        item = self.db.item(item_id)
        try:
            if item["plane_item_id"]:
                proj = await self.plane_project(item["repo"] or item["new_repo_name"])
                await self.plane.set_state(proj, item["plane_item_id"], "rejected")
        except Exception:
            log.exception("could not set Plane state Rejected for item %s", item_id)
        await self.say(item["root_post_id"], "🛑 Rejected. Nothing was sent to GitHub.")

    async def _on_approve_repo(self, item_id: int, ctx: dict) -> dict:
        item = self.db.item(item_id)
        if ctx.get("repo") != item["new_repo_name"]:
            return NOT_ALLOWED
        if not self.db.transition(item_id, ["repo_pending", "repo_failed"], "creating_repo"):
            return {"ephemeral_text": "Already handled."}
        self.spawn(self._create_repo(item_id))
        return {"ephemeral_text": f"Creating kiukairor-org/{item['new_repo_name']}…"}

    async def _create_repo(self, item_id: int) -> None:
        item = self.db.item(item_id)
        root, name = item["root_post_id"], item["new_repo_name"]
        step = "validate"
        try:
            if not valid_repo_name(name):
                raise ValueError("invalid repo name")
            if not self.db.has_milestone(item_id, "repo:generated"):
                step = "check-free"
                if await self.gh.repo_exists(name):
                    raise ValueError(f"{name} already exists")
                step = "generate"
                await self.gh.generate_repo_from_template(name, item["new_repo_desc"] or "")
                self.db.claim_milestone(item_id, "repo:generated")
            step = "wait-for-main"
            await self.gh.wait_for_main(name)
            step = "settings+protection+labels"
            await self.gh.configure_new_repo(name)
            self.db.set_thread_repo(root, name, False)
            self.db.update_item(item_id, repo=name)
            step = "plane+issue"
            await self.say(root, f"📦 Repo created: https://github.com/{self.cfg.github_org}/{name}")
            await self._ready_and_issue(item_id, new_app=name)
            self.db.transition(item_id, ["creating_repo"], "approved")
        except Exception as exc:
            log.exception("repo creation failed for item %s at %s", item_id, step)
            self.db.update_item(item_id, error=f"{step}: {exc}"[:500])
            self.db.transition(item_id, ["creating_repo"], "repo_failed")
            await self.say(root, f"⚠️ Repo creation failed at step `{step}`. Tap Approve repo again to retry, or Reject.")

    # ------------------------------------------------------- New Relic alerts
    async def ingest_alerts(self, payload: Any) -> dict:
        """Alerts from catcher.nr_triage. All alert text is untrusted: it is only escaped, clipped and
        displayed - never sent to Claude, never interpreted. Each new issue becomes a Triage item plus a
        root post in #ideas with the usual owner-gated Approve/Reject. Nothing is ever auto-approved."""
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if not isinstance(issues, list) or len(issues) > MAX_ALERTS_PER_REQUEST:
            raise ValueError("expected {'issues': [...]} with at most 50 entries")
        created = duplicates = invalid = 0
        for raw in issues:
            alert = normalize_alert(raw)
            if alert is None:
                invalid += 1
                continue
            if not self.db.claim_nr_issue(alert["issue_id"]):
                duplicates += 1
                continue
            try:
                await self._triage_alert(alert)
                created += 1
            except Exception:
                log.exception("alert %s: triage failed", alert["issue_id"])
                self.db.release_nr_issue(alert["issue_id"])
        return {"created": created, "duplicates": duplicates, "invalid": invalid}

    async def _triage_alert(self, alert: dict) -> None:
        repo = self.cfg.nr_target_repo
        names = ", ".join(alert["entity_names"]) or "n/a"
        summary_plain = (
            f"New Relic issue {alert['issue_id']} ({alert['priority']}): {alert['title']}. "
            f"Entities: {names}. Activated: {alert['activated_at'] or 'n/a'}."
        )
        spec = validate_spec(
            {
                "action": "create",
                "title": clip("[NR] " + alert["title"], 200),
                "user_story": clip(f"As the owner of {repo}, I want this production alert investigated and fixed: {summary_plain}", 4000),
                "acceptance_criteria": list(NR_CRITERIA),
                "labels": ["bug"],
            }
        )
        header = (
            f"🚨 **New Relic issue** ({md_escape(alert['priority'])}) for `{repo}`\n"
            f"**{md_escape(alert['title'])}**\n"
            f"Entities: {md_escape(names)} · Activated: {md_escape(alert['activated_at'] or 'n/a')} · "
            f"Issue id: `{alert['issue_id']}`"
        )
        if alert["url"]:
            header += f"\nNew Relic: {alert['url']}"
        root_post = await self.mm.post(self.ideas_channel_id, header + "\n\n_Creating the triage item…_")
        root = root_post["id"]
        self.db.upsert_thread(root, self.ideas_channel_id)
        self.db.set_thread_repo(root, repo, False)
        item_id = self.db.create_item(root, summary_plain, "running")
        self.db.link_nr_issue(alert["issue_id"], item_id)
        self.db.update_item(item_id, repo=repo, spec_json=json.dumps(spec.raw))
        proj = await self.plane_project(repo)
        wi = await self.plane.create_work_item(
            proj, title=spec.title, description_html=spec_html(spec, summary_plain, "New Relic issue (untrusted text)"), labels=["bug"]
        )
        self.db.update_item(item_id, plane_project_id=proj.project_id, plane_item_id=wi["id"], plane_seq=wi.get("sequence_id"))
        self.db.transition(item_id, ["running"], "triage")
        url = self.plane.item_url(proj.project_id, wi["id"])
        secret, base = self.cfg.action_secret, self.cfg.action_base_url
        actions = [
            buttons.button(base, secret, "Approve", "approve", {"item": item_id}, "good"),
            buttons.button(base, secret, "Reject", "reject", {"item": item_id}, "danger"),
        ]
        await self.mm.patch_post(
            root,
            f"{header}\nPlane ({proj.identifier}-{wi.get('sequence_id')}, Triage): {url}",
            attachments=[{"text": "Approve to hand this to the agent (GitHub issue in " + repo + ").", "actions": actions}],
        )

    # ----------------------------------------------------------- milestones
    async def announce(self, item_id: int, text: str) -> None:
        item = self.db.item(item_id)
        await self.say(item["root_post_id"], text)
        if self.agent_log_channel_id:
            tag = f"[{item['repo']}#{item['gh_issue_number']}]" if item["gh_issue_number"] else f"[{item['repo']}]"
            await self.mm.post(self.agent_log_channel_id, f"{tag} {text}")


def _clean_list(v: Any, n: int = 10, width: int = 100) -> list[str]:
    return [clip(" ".join(str(x).split()), width) for x in v[:n]] if isinstance(v, list) else []


def normalize_alert(raw: Any) -> dict | None:
    """Shape-check and clip one alert; None if unusable. Values stay untrusted (escape on display)."""
    if not isinstance(raw, dict):
        return None
    issue_id = str(raw.get("issue_id") or "")
    if not _NR_ISSUE_ID.match(issue_id):
        return None
    url = str(raw.get("url") or "")
    if not re.match(r"^https://one(\.eu)?\.newrelic\.com/[^\s<>()\[\]`]*$", url):
        url = ""
    priority = str(raw.get("priority") or "UNKNOWN").upper()
    if priority not in ("LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN"):
        priority = "UNKNOWN"
    activated = str(raw.get("activated_at") or "")
    if not re.match(r"^\d{4}-\d{2}-\d{2}T[0-9:.]+Z$", activated):
        activated = ""
    return {
        "issue_id": issue_id,
        "title": clip(" ".join(str(raw.get("title") or "New Relic issue").split()), 180) or "New Relic issue",
        "priority": priority,
        "entity_names": _clean_list(raw.get("entity_names")),
        "entity_guids": _clean_list(raw.get("entity_guids"), width=200),
        "activated_at": activated,
        "url": url,
    }
