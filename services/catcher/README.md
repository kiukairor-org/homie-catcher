# catcher

Mattermost `#ideas` → headless Claude Code (read-only Plane MCP) → validated spec → Plane work item
(Triage) → **Approve / Reject** buttons → GitHub issue (label `agent`) in `kiukairor-org/<repo>`,
then milestones (PR, CI, review, preview, merge) polled from GitHub and posted to the thread and
`#agent-log`.

## Message forms (owner only, in `#ideas`)

| Message | Routing (done by the bot, never Claude) |
|---|---|
| `work on <repo>: <idea>` | existing org repo (case-insensitive). Unknown or excluded → "which app?" |
| `new idea: <idea>` | new app; Claude proposes `new_repo.name`, the bot validates it |
| anything else | "which app?" buttons (one per org repo + **New repo**) |
| thread reply | answers a pending clarifying question, or a follow-up idea for the thread's remembered repo |
| `name: <repo-name>` (thread reply) | when the proposed new-repo name was invalid/taken |

Excluded everywhere (`routing.EXCLUDED_REPOS`): `release`, `papl`, `homelab-gitops`,
`mattermost-arm64`, `homie-catcher`, `app-template`.

## Item lifecycle

`awaiting_repo` → `queued` → `running` → (`clarifying` → `queued` …) → `triage`
→ Approve → `approving` → `approved` → (poller) `done`
→ new app: Approve → `repo_pending` → Approve repo → `creating_repo` → `approved`
→ Reject → `rejected`. Failures: `error`, `approve_failed`, `repo_failed` (tap again to retry).
Every button is an atomic status transition, so a second click (or a replay) does nothing.

## Security model

- **Chat text is untrusted data.** It goes to Claude inside `<idea>`/`<answer>` tags (HTML-escaped so it
  can't close them), with a system prompt saying it is data, never instructions.
- **Claude can't act.** `--tools ""` (no built-in tools), `--strict-mcp-config` with only the Plane MCP
  server, `--allowedTools` = 10 read-only Plane tools (`catcher/plane_mcp_tools.py`), every other Plane tool
  plus `Bash Edit Write WebFetch WebSearch NotebookEdit Task` in `--disallowedTools`,
  `--permission-mode dontAsk` + `--permission-prompts none` (nothing can prompt; it's denied),
  `--setting-sources ""`, empty temp working dir, `--max-turns 8`. Its child env holds only the OAuth token.
- **The bot does all writes**, from the strictly validated JSON (`catcher/spec.py`, `additionalProperties:
  false`, one retry, then an error note). Claude's `target_repo` is ignored; routing is the bot's.
  Text from the model is de-fanged before posting (no `@channel`/`@user` pings, no HTML comments).
- **Buttons**: context is HMAC-SHA256 signed with `CATCHER_ACTION_SECRET` and bound to the action name;
  bad signature or a `user_id` not in `OWNER_USER_IDS` → ephemeral "not allowed", nothing else happens.
  Mattermost doesn't sign its callbacks, so also restrict who can reach `:8080` with a NetworkPolicy
  (ingress only from the `mattermost` namespace).
- **GitHub** (`catcher/github.py`): App JWT → installation token (cached until 5 min before expiry), an
  explicit endpoint allowlist enforced in `request()`, owner pinned to `GITHUB_ORG`. The only
  Administration calls are `POST /repos/{org}/app-template/generate`, `POST /orgs/{org}/repos` (allowlisted,
  public only, unused), `PATCH /repos/{o}/{r}` and `PUT /repos/{o}/{r}/branches/main/protection`, with
  bodies pinned to constants and used only from the new-repo flow. Tests scan the package for any other
  Administration endpoint.
- **Logs**: a redaction filter removes every configured secret value plus token shapes (`ghs_…`,
  `sk-ant-…`, JWTs, PEM keys, `Bearer …`).

## Configuration (environment only)

| Variable | Required | Meaning |
|---|---|---|
| `MM_URL` | yes | Internal Mattermost URL, e.g. `http://mattermost.mattermost.svc.cluster.local:8065` (websocket derived from it) |
| `MM_BOT_TOKEN` | yes, secret | Personal access token of the `catcher` bot account |
| `MM_TEAM` | yes | Team name (URL slug) holding `#ideas` and `#agent-log` |
| `OWNER_USER_IDS` | yes | Comma-separated Mattermost user IDs allowed to post ideas and press buttons |
| `CATCHER_ACTION_SECRET` | yes, secret | ≥ 32 chars; HMAC key for button contexts (`openssl rand -hex 32`) |
| `PLANE_BASE_URL` | yes | Internal Plane URL (API + MCP), e.g. `http://plane-api.plane.svc.cluster.local:8000` |
| `PLANE_PUBLIC_URL` | yes | Browser URL for links, `https://plane.tail8e3e5b.ts.net` |
| `PLANE_API_KEY` | yes, secret | `catcher-bot` Plane API token (also handed to the Plane MCP server) |
| `PLANE_WORKSPACE_SLUG` | yes | `homie` |
| `GITHUB_ORG` | yes | `kiukairor-org` |
| `GITHUB_APP_ID` | yes | kiu-homie-bot App ID (JWT `iss`) |
| `GITHUB_INSTALLATION_ID` | yes | kiu-homie-bot installation ID on the org |
| `GITHUB_APP_PRIVATE_KEY_PATH` | yes | Path of the App private key PEM (mount the Secret as a file) |
| `CLAUDE_CODE_OAUTH_TOKEN` | yes, secret | From `claude setup-token`; passed only to the `claude` child process |
| `DB_PATH` | yes | SQLite file on the PVC (image default `/data/catcher.db`) |
| `CLAUDE_RUNS_PER_HOUR` | no (10) | Claude run limit; extra ideas wait in the queue (retries count) |
| `CLAUDE_MODEL` | no | `--model` for Claude (default: the CLI's default) |
| `CATCHER_ACTION_BASE_URL` | no | Button callback base, default `http://catcher.catcher.svc.cluster.local:8080` |
| `MM_IDEAS_CHANNEL` / `MM_AGENT_LOG_CHANNEL` | no | Channel names, default `ideas` / `agent-log` |
| `OWNER_MENTION` | no (`@kiukairor`) | Who to ping on `needs-human` |
| `CLAUDE_BIN` / `PLANE_MCP_BIN` | no | Set by the image |
| `LOG_LEVEL` | no (`INFO`) | Python log level |

Mattermost must allow the callback host: System Console → Developer → *Allow untrusted internal
connections to* `catcher.catcher.svc.cluster.local`.

## Plane notes (CE v1.4.2)

The bot creates one project per repo (identifier = repo name, alphanumerics upper-cased, max 12; for a
new app the proposed name), states **Triage** (backlog group), **Ready for agent** (unstarted),
**In progress**, **In review** (started), **Done** (completed), **Rejected** (cancelled), reusing
same-named existing ones, and labels `feature`, `bug`, `chore`. Plane reserves the `triage` group for
Intake (the API refuses to create such states and won't assign them), and every new project already
has a hidden state named "Triage", so in practice our triage state is created as **"Needs triage"**.
Links use `https://plane.tail8e3e5b.ts.net/homie/projects/<project_id>/issues/<issue_id>`, which Plane
redirects to `/homie/browse/<IDENT>-<n>/`.

## Run

```bash
cd services/catcher
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest && ruff check . && ruff format --check .
# real run (needs every variable above, and `claude` + `plane-mcp-server` on PATH):
python -m catcher
```

Endpoints on `:8080`: `GET /healthz`, `POST /actions/<choose_repo|approve|reject|approve_repo|reject_repo>`.

## Image

`Dockerfile` (multi-arch base `python:3.12.14-slim-bookworm` pinned by digest; CI builds `linux/arm64`):

- **Claude Code 2.1.312**: native binary from `downloads.claude.ai/claude-code-releases/2.1.312/linux-arm64/claude`,
  checked against the GPG-signed `manifest.json` (key fingerprint `31DD DE24 DDFA B679 F42D 7BD2 BAA9 29FF 1A7E CACE`,
  per code.claude.com "Binary integrity"). No Node.js needed; auto-update disabled.
- **plane-mcp-server 0.2.10** in its own venv `/opt/plane-mcp` from `requirements-mcp.txt` (full lock, every
  package ≥ 2 weeks old). 0.2.10 is the last release with one tool per operation; from 0.3.0 reads and writes
  share one tool per resource, which a tool-name allowlist can't restrict. Re-check before bumping.
- Runs as UID 10001. Claude needs a writable `$HOME` (`/home/catcher`): with a read-only root filesystem,
  mount an `emptyDir` there and at `/tmp`. Mount the PVC at `/data`.

CI (`.github/workflows/ci.yml`): ruff + pytest on `ubuntu-24.04-arm`; on `main` pushes
`ghcr.io/kiukairor-org/homie-catcher:<short-sha>` and `:main`.

## Milestones (poller, every 60 s)

For each approved issue: PRs whose body has `#N` or whose head is `claude/issue-N-*` →
PR opened (Plane In progress), check run `ci` result on the head SHA, reviews by
`kiu-homie-reviewer[bot]` (Plane In review), label `needs-human` (pings `OWNER_MENTION`),
`Preview: https://…` comments by `kiu-homie-bot[bot]` / `github-actions[bot]` only, merged (Plane Done).
Each is announced once (table `milestones`). Polling pauses while `X-RateLimit-Remaining` < 100.
**Deployed**: `Poller.on_merged` is the Phase 4 hook (TODO).
