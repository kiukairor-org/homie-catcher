"""SRE investigations on demand (D75): the catcher starts one Kubernetes Job per run in namespace `sre`,
watches it, meters its Claude usage and posts the findings in the Mattermost thread.

Who may wake it:
- the OWNER, by an order in an app channel ~<repo> ("investigate why…", "check the logs and fix it"):
  mode `owner` (may open ONE fix/revert PR) or `fix` (also one rollout restart; owner asked for a fix);
- a New Relic alert handled by nr-triage (only if SRE_ON_ALERTS=true): mode `alert`, investigation only;
  the fix then goes through the alert item's owner Approve (agent-originated work, D73).
Bounds: budget gate before start (feature `sre`), max 1 run per repo and 2 in total, Job activeDeadlineSeconds,
claude --max-turns + timeout inside the Job, ttlSecondsAfterFinished.
Truthfulness: actions are reported from the tools' own action log, not from Claude's text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from .text import clip

log = logging.getLogger(__name__)

NS = "sre"
JOB_DEADLINE_S = 900  # 15 minutes wall clock for the whole Job
CLAUDE_TIMEOUT_S = 780  # claude is stopped before the Job deadline so the result line is always printed
TTL_AFTER_FINISHED_S = 3600
POLL_S = 10
MAX_RUNNING_TOTAL = 2
SRE_LABEL = "sre"
MODES = ("alert", "owner", "fix")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sre_runs(
  id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  mode TEXT NOT NULL,
  root_post_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  item_id INTEGER,
  status TEXT NOT NULL,
  task TEXT NOT NULL,
  result_json TEXT,
  error TEXT,
  started_at REAL NOT NULL,
  finished_at REAL
);
"""

_RESULT = re.compile(r"^SRE_RESULT (\{.*\})\s*$", re.M)


def parse_result(log_text: str) -> dict | None:
    matches = list(_RESULT.finditer(log_text or ""))
    m = matches[-1] if matches else None
    if not m:
        return None
    try:
        out = json.loads(m.group(1))
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def findings_markdown(run_id: str, out: dict | None, mode: str) -> str:
    """The thread post, written by the bot from the structured output and the tool action log."""
    from .brainstorm import chat_safe, guard_evidence  # local import: brainstorm imports nothing from here

    if not out or (out.get("error") and not out.get("output")):
        err = (out or {}).get("error") or "no result from the Job"
        return f"⚠️ SRE run `{run_id}` ended without findings: {clip(str(err), 300)}"
    o = out.get("output") or {}
    lines = [f"🔎 **SRE findings** (run `{run_id}`, confidence: **{o.get('confidence', '?')}**)", "", f"**{o.get('summary', '')}**", "",
             f"**Cause:** {o.get('cause', '')}", "", "**Evidence:**"]  # fmt: skip
    for e in o.get("evidence") or []:
        lines.append(f"- {e.get('claim', '')} — _{e.get('source', '')}_")
    if o.get("proposed_fix"):
        lines += ["", f"**Proposed fix:** {o['proposed_fix']}"]
    if o.get("not_verified"):
        lines += ["", "**Not verified:** " + "; ".join(o["not_verified"])]
    body = "\n".join(lines)
    acts = out.get("actions") or []
    act_lines = []
    for a in acts:
        if a.get("action") in ("fix_pr", "revert_pr"):
            kind = "Fix PR" if a["action"] == "fix_pr" else "Revert PR (rollback)"
            note = "" if a.get("auto_merge", True) else " (⚠️ auto-merge could not be enabled: merge needs a human)"
            act_lines.append(f"- 🔧 {kind} opened: {a.get('url')}{note}")
        elif a.get("action") == "restart":
            act_lines.append(f"- 🔄 Rollout restart of `{a.get('deployment')}` at {a.get('at')}")
    if act_lines:
        body += "\n\n**Actions taken (from the tool log):**\n" + "\n".join(act_lines)
    elif mode == "alert":
        body += "\n\n_Investigation only (woken by an alert). To fix it, press **Approve** on the alert item._"
    else:
        body += "\n\n_No PR was opened and nothing was restarted._"
    return chat_safe(guard_evidence(clip(body, 12_000)))


class SreManager:
    def __init__(self, catcher: Any, k8s: Any) -> None:
        self.c = catcher
        self.cfg = catcher.cfg
        self.k8s = k8s
        self.db = catcher.db
        self.db._conn.executescript(SCHEMA_SQL)
        self._tasks: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------ storage
    def run(self, run_id: str):  # noqa: ANN201
        return self.db._x("SELECT * FROM sre_runs WHERE id=?", (run_id,)).fetchone()

    def running(self, repo: str | None = None) -> list:
        sql, args = "SELECT * FROM sre_runs WHERE status IN ('starting','running')", ()
        if repo:
            sql, args = sql + " AND repo=?", (repo,)
        return self.db._x(sql, args).fetchall()

    def _set(self, run_id: str, **f: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in f)
        self.db._x(f"UPDATE sre_runs SET {cols} WHERE id=?", (*f.values(), run_id))  # noqa: S608 - fixed column names

    async def _say(self, channel: str, root: str, text: str) -> None:
        await self.c.mm.post(channel, text, root_id=root)

    # ------------------------------------------------------------ start
    def enabled(self) -> bool:
        return bool(self.cfg.sre_image) and self.k8s is not None

    async def start(self, *, repo: str, root: str, channel: str, task: str, mode: str, thread_link: str = "") -> str | None:
        """Start a run (the caller already checked who asked). Returns the run id, or None (reason posted)."""
        if mode not in MODES:
            raise ValueError(mode)
        if not self.enabled():
            await self._say(channel, root, "⚠️ The SRE agent is not enabled on this catcher. Nothing was started.")
            return None
        if not self.c.usage.allow("sre"):
            await self._say(channel, root, self.c.usage.paused_text())
            return None
        if self.running(repo):
            r = self.running(repo)[0]
            await self._say(channel, root, f"⏳ An SRE run for `{repo}` is already going (run `{r['id']}`); wait for its findings.")
            return None
        if len(self.running()) >= MAX_RUNNING_TOTAL:
            await self._say(channel, root, f"⏳ {MAX_RUNNING_TOTAL} SRE runs are already going; try again in a few minutes.")
            return None
        run_id = f"{repo[:24]}-{int(self.c.clock() * 1000) % 10**10:010d}"
        self.db._x("INSERT INTO sre_runs(id, repo, mode, root_post_id, channel_id, status, task, started_at) VALUES(?,?,?,?,?,?,?,?)",
                   (run_id, repo, mode, root, channel, "starting", task, self.c.clock()))  # fmt: skip
        try:
            issue = None
            if mode != "alert":  # owner order: a tracking issue (label `sre`, NOT `agent`: nothing builds it)
                await self.c.gh.ensure_label(repo, SRE_LABEL, "d93f0b", "SRE investigation (catcher, D75)")
                body = (f"SRE investigation `{run_id}` ordered by the owner in Mattermost: {thread_link}\n\n"
                        f"> {clip(' '.join(task.split()), 1500)}\n\nFindings are posted in the Mattermost thread. "
                        "A fix PR (if any) closes this issue.")  # fmt: skip
                issue = await self.c.gh.create_issue(repo, clip(f"SRE: {' '.join(task.split())}", 120), body, [SRE_LABEL])
                self.db.upsert_thread(root, channel)
                item_id = self.db.create_item(root, f"SRE: {task}", "investigating")
                self.db.update_item(item_id, repo=repo, gh_issue_number=issue["number"], gh_issue_url=issue["html_url"])
                self._set(run_id, item_id=item_id)
            token = await self.c.gh.sre_token(repo, write=mode != "alert")
            job = await self.k8s.create(
                f"/apis/batch/v1/namespaces/{NS}/jobs", self.job_manifest(run_id, repo, mode, task, thread_link, issue)
            )
            await self.k8s.create(f"/api/v1/namespaces/{NS}/secrets", {
                "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": f"sre-run-{run_id}", "namespace": NS, "labels": {"app": "sre-agent", "homie/run": run_id},
                             "ownerReferences": [{"apiVersion": "batch/v1", "kind": "Job", "name": job["metadata"]["name"],
                                                  "uid": job["metadata"]["uid"]}]},
                "stringData": {"token": token},
            })  # fmt: skip
        except Exception as exc:
            log.exception("sre %s: start failed", run_id)
            self._set(run_id, status="error", error=clip(str(exc), 500), finished_at=self.c.clock())
            await self._say(channel, root, f"⚠️ Could not start the SRE run ({clip(str(exc), 200)}). Nothing is running.")
            return None
        self._set(run_id, status="running")
        extra = f" Tracking issue: {issue['html_url']}" if issue else ""
        await self._say(channel, root, f"🔎 Investigating… (run `{run_id}`, mode `{mode}`, ≤15 min).{extra}")
        self.watch(run_id)
        return run_id

    def job_manifest(self, run_id: str, repo: str, mode: str, task: str, thread_link: str, issue: dict | None) -> dict:
        sa = f"sre-{repo}-fix" if mode == "fix" else f"sre-{repo}"
        issue_no = str(issue["number"]) if issue else ""
        env = {"REPO": repo, "MODE": mode, "RUN_ID": run_id, "THREAD_LINK": thread_link, "ISSUE_NUMBER": issue_no,
               "SRE_TASK": clip(task, 6000), "GITHUB_ORG": self.cfg.github_org, "GITHUB_TOKEN_FILE": "/secrets/github/token",
               "NR_KEY_FILE": "/secrets/newrelic/NEW_RELIC_API_KEY", "NEW_RELIC_ACCOUNT_ID": self.cfg.nr_account_id,
               "NEW_RELIC_REGION": self.cfg.nr_region, "ACTIONS_FILE": "/work/actions.jsonl", "WORK_DIR": "/work",
               "SRE_MAX_TURNS": str(self.cfg.sre_max_turns), "SRE_TIMEOUT_S": str(CLAUDE_TIMEOUT_S), "HOME": "/home/catcher"}  # fmt: skip
        if self.cfg.claude_model:
            env["CLAUDE_MODEL"] = self.cfg.claude_model
        labels = {"app": "sre-agent", "homie/run": run_id, "homie/repo": repo}
        vol = lambda name, path: {"name": name, "mountPath": path}  # noqa: E731
        return {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": f"sre-{run_id}"[:63], "namespace": NS, "labels": labels},
            "spec": {
                "backoffLimit": 0, "activeDeadlineSeconds": JOB_DEADLINE_S, "ttlSecondsAfterFinished": TTL_AFTER_FINISHED_S,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": sa, "automountServiceAccountToken": True, "restartPolicy": "Never",
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                                            "seccompProfile": {"type": "RuntimeDefault"}},
                        "containers": [{
                            "name": "sre", "image": self.cfg.sre_image, "workingDir": "/app",
                            "command": ["/opt/venv/bin/python", "-m", "catcher.sre_job"],
                            "env": [{"name": k, "value": v} for k, v in env.items()],
                            "envFrom": [{"secretRef": {"name": "sre-claude-token"}}],
                            "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}},
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "capabilities": {"drop": ["ALL"]}},
                            "volumeMounts": [vol("work", "/work"), vol("home", "/home/catcher"), vol("tmp", "/tmp"),
                                             {**vol("github", "/secrets/github"), "readOnly": True},
                                             {**vol("newrelic", "/secrets/newrelic"), "readOnly": True}],
                        }],
                        "volumes": [
                            {"name": "work", "emptyDir": {"sizeLimit": "64Mi"}}, {"name": "home", "emptyDir": {"sizeLimit": "256Mi"}},
                            {"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}},
                            {"name": "github", "secret": {"secretName": f"sre-run-{run_id}", "defaultMode": 0o400}},
                            {"name": "newrelic", "secret": {"secretName": "sre-newrelic", "defaultMode": 0o400}},
                        ],
                    },
                },
            },
        }  # fmt: skip

    # ------------------------------------------------------------ watch / finish
    def watch(self, run_id: str) -> None:
        if run_id not in self._tasks or self._tasks[run_id].done():
            self._tasks[run_id] = asyncio.create_task(self._watch(run_id))

    def resume(self) -> None:
        """After a catcher restart: keep watching runs that were in flight."""
        for r in self.running():
            self.watch(r["id"])

    async def _watch(self, run_id: str) -> None:
        r = self.run(run_id)
        job = f"sre-{run_id}"[:63]
        deadline = r["started_at"] + JOB_DEADLINE_S + 180
        state = None
        while self.c.clock() < deadline:
            try:
                st = (await self.k8s.get(f"/apis/batch/v1/namespaces/{NS}/jobs/{job}")).get("status") or {}
                if st.get("succeeded") or st.get("failed"):
                    state = "succeeded" if st.get("succeeded") else "failed"
                    break
            except Exception as exc:  # noqa: BLE001
                if getattr(exc, "status", None) == 404:
                    state = "gone"
                    break
                log.warning("sre %s: job status: %s", run_id, exc)
            await self.c.sleep(POLL_S)
        await self.finish(run_id, state or "timeout")

    async def finish(self, run_id: str, state: str) -> None:
        r = self.run(run_id)
        if r is None or r["status"] not in ("starting", "running"):
            return
        out = None
        try:
            pods = await self.k8s.get(f"/api/v1/namespaces/{NS}/pods?labelSelector=homie%2Frun%3D{run_id}")
            for p in (pods or {}).get("items") or []:
                out = parse_result(await self.k8s.log(f"/api/v1/namespaces/{NS}/pods/{p['metadata']['name']}/log?tailLines=50")) or out
        except Exception as exc:  # noqa: BLE001
            log.warning("sre %s: reading the result failed: %s", run_id, exc)
        if out is None:
            out = {"error": f"Job {state}; no result line (e.g. the 15-minute limit)"}
        if isinstance(out.get("result"), dict):
            res = out["result"]
            self.c.usage.record("sre", res, ok=not res.get("is_error") and not out.get("error"), repo=r["repo"])
        self._set(run_id, status="done" if out.get("output") else "error", result_json=json.dumps(out)[:200_000],
                  error=out.get("error"), finished_at=self.c.clock())  # fmt: skip
        await self._say(r["channel_id"], r["root_post_id"], findings_markdown(run_id, out, r["mode"]))
        await self._after(r, out)
        if state == "timeout":
            try:
                await self.k8s.delete(f"/apis/batch/v1/namespaces/{NS}/jobs/sre-{run_id}"[:200])
            except Exception as exc:  # noqa: BLE001
                log.warning("sre %s: delete job: %s", run_id, exc)

    async def _after(self, r: Any, out: dict) -> None:
        """Hand a fix PR to the normal milestone tracking, or close the tracking issue."""
        if not r["item_id"]:
            return
        item = self.db.item(r["item_id"])
        if any(a.get("action") in ("fix_pr", "revert_pr") for a in out.get("actions") or []):
            self.db.transition(item["id"], ["investigating"], "approved")  # poller: CI, review, merged, deployed
            return
        self.db.transition(item["id"], ["investigating"], "done")
        try:
            await self.c.gh.comment(r["repo"], item["gh_issue_number"], f"SRE run `{r['id']}` finished without a code change. "
                                    "Findings are in the Mattermost thread.")  # fmt: skip
            await self.c.gh.close_issue(r["repo"], item["gh_issue_number"])
        except Exception as exc:  # noqa: BLE001
            log.warning("sre %s: closing the tracking issue: %s", r["id"], exc)

    def status_line(self) -> str:
        rows = self.db._x("SELECT id, repo, mode, status, started_at FROM sre_runs ORDER BY started_at DESC LIMIT 5").fetchall()
        return "; ".join(f"{x['id']} {x['status']} ({time.strftime('%H:%MZ', time.gmtime(x['started_at']))})" for x in rows)
