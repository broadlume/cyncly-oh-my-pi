# roboomp

Self-hosted GitHub triage bot. Drives [`omp --mode rpc`](https://github.com/can1357/oh-my-pi)
as a subprocess against a per-issue git worktree, then writes back to GitHub
through a sidecar that holds the PAT.

On `issues.opened` in an allowlisted repo it classifies the issue, labels it,
and branches:

- `bug` / `documentation` → reproduce, fix on a fresh branch, open a PR whose
  body has `## Repro` / `## Cause` / `## Fix` / `## Verification` and
  `Fixes #N`.
- `question` → one comment, suffixed with a 👎-to-keep-open prompt; if the
  issue author doesn't react 👎 within `ROBOMP_QUESTION_AUTOCLOSE_HOURS`
  (default 4), the issue auto-closes as `state_reason=completed`. A follow-up
  comment or external close cancels the schedule synchronously.
- `enhancement` / `proposal` → one comment, no PR.
- `invalid` / `duplicate` → one brief comment.

Incoming contributor PRs get a one-shot review on open; a maintainer (or configured reviewer bot) can force another pass with an explicit `@bot` re-review comment (e.g. `@roboomp please re-review`). Follow-up issue comments and PR review comments resume the same omp session
(`--continue` against the persisted JSONL transcript). On orchestrator
restart, in-flight events are re-queued and resume the same way.

## Architecture

Two containers, one trust boundary:

- **robomp** — FastAPI + sqlite event queue + `WorkerPool` running `omp` in
  per-issue worktrees under `/data/workspaces/`. Holds the HMAC key, never
  the PAT.
- **gh-proxy** — sibling on an `internal: true` network. Holds `GITHUB_TOKEN`,
  verifies HMAC-signed requests from robomp, executes REST + `git push`.
  Only egress to `api.github.com`.

Flow: webhook → HMAC verify → `github_events.route` → sqlite `events`
(dedup on `X-GitHub-Delivery`) → `WorkerPool` claims under
`BEGIN IMMEDIATE` with an in-process `_inflight` set per `(owner, repo, n)`
→ `sandbox.ensure_workspace` produces a worktree on `farm/<8hex>/<slug>`
→ `worker.run_task` spawns `omp --mode rpc` with `cwd=worktree`,
persistent `session_dir`, model randomly drawn from `ROBOMP_MODEL` (CSV).

The agent uses omp's built-in tools (`read`/`edit`/`bash`/`lsp`, scoped to
the worktree) plus the host tools in `src/host_tools.py` — the
exclusive surface for GitHub writes. Every host-tool invocation is audited
into the `tool_calls` table with credential-redacted args and results.

## Setup

Requires Docker Compose v2 and a LiteLLM-style proxy on the host. Put the agent
config the container should use in `python/robomp/agent-home-local/` (or point
`ROBOMP_AGENT_HOME_OVERRIDE` at another tree): `.omp/agent/models.yml` for
gateway routing and `.agent/AGENTS.md` for instructions. That tree is layer 2;
it is kept out of the host's own `~/.omp/agent/` so the host omp does not route
through the gateway. Layer 1 — skills, subagents, base `config.yml`/`mcp.json`,
base rules — is baked into the image from
`infra/cdk/robomp/assets/agent-bundle/`; see `infra/cdk/robomp/README.md`. The
image is self-contained: it builds the dashboard bundle and the omp_rpc wheel
from this repo and bakes the upstream `omp` release binary (pinned by the
`OMP_VERSION` build-arg in `Dockerfile.robomp`).

Bot account needs **Write** on every repo in `ROBOMP_REPO_ALLOWLIST`. A
fine-grained PAT with Contents / Issues / Pull requests RW + Metadata R is
enough.

```bash
cp .env.example .env
$EDITOR .env
openssl rand -hex 32              # ROBOMP_GH_PROXY_HMAC_KEY
openssl rand -hex 32              # GITHUB_WEBHOOK_SECRET

cd python/robomp && docker compose build
docker compose up -d
curl -fsS http://localhost:8080/healthz
```

The bundled `docker-compose.yml` runs in gh-proxy mode by default. To run
the orchestrator directly with the PAT in-process (host CLI, tests),
comment out `ROBOMP_GH_PROXY_URL` / `ROBOMP_GH_PROXY_HMAC_KEY` and set
`GITHUB_TOKEN`. The two modes are mutually exclusive (`config.py`
rejects a `.env` setting both).

Build invalidation is bounded: editing roboomp Python touches only the
runtime layer; the web bundle and omp_rpc wheel rebuild only when their
sources change.

### AWS (hardened EC2 + ALB)

See [`infra/cdk/robomp/README.md`](../../infra/cdk/robomp/README.md) for a TypeScript CDK stack that deploys robomp + gh-proxy + LiteLLM on an isolated private EC2 behind an HTTPS ALB (path `/webhook/github` only). The instance role can only read the stack secret; SSH/SSM/ECR/S3 are denied.

### Public URL

roboomp does not ship a tunnel. Cloudflare, smee, ngrok are all fine. The
recommended ingress rule restricts the public hostname to
`/webhook/github` exactly; `/healthz`, `/events`, `/issues`, `/replay`
stay localhost-only.

### GitHub webhook

In *Settings → Webhooks*: payload URL `https://…/webhook/github`, content
type `application/json`, secret = `GITHUB_WEBHOOK_SECRET`, events =
*Issues, Issue comments, Pull requests, Pull request reviews, Pull
request review comments*. GitHub's `ping` should produce
`POST /webhook/github 202` within a second.

### Configuration

See `.env.example` for the authoritative variable list. The shipped
`docker-compose.yml` uses per-service `environment:` allowlists rather
than `env_file:`, so `GITHUB_TOKEN` only reaches the gh-proxy container.

## CLI

The container entrypoint is `python -m robomp serve`. Other commands run
inside the running container:

```bash
docker compose exec robomp robomp triage  owner/repo#123   # synthesize an issues.opened and wait
docker compose exec robomp robomp replay  <delivery_id>    # re-enqueue a stored event and wait
docker compose exec robomp robomp status                   # dump issues table
docker compose exec robomp robomp cleanup owner/repo#123   # force workspace removal, state=abandoned
```

Lifecycle commands run through Docker Compose from `python/robomp/`
(`docker compose build`, `up -d`, `down`, `logs -f`, `restart`).

## Tests

```bash
pytest -x tests/                              # unit suite, no network
ROBOMP_INTEGRATION=1 pytest -x tests/test_worker_smoke.py
```

The integration test spawns a real `omp --mode rpc` against an
`httpx.MockTransport` GitHub and a local bare repo, so it needs `omp` on
`PATH`.

## Security posture

- `GITHUB_TOKEN` lives only in the gh-proxy container. The orchestrator
  refuses to start if it sees `GITHUB_TOKEN` in its own environment.
- Orchestrator → gh-proxy is HMAC-SHA256 signed with a ±30s skew window
  and constant-time compare.
- `git push` inside gh-proxy uses `git -c http.extraheader=…` with the
  token passed through an ephemeral process env var; the remote URL in
  `.git/config` stays token-free.
- gh-proxy has no host port. The `robomp_internal` network is
  `internal: true` (no ingress, no egress); gh-proxy joins `default`
  only to reach `api.github.com`.
- Agent subprocess env is scrubbed of `GITHUB_TOKEN` /
  `ROBOMP_GH_PROXY_HMAC_KEY` / friends via `worker._SCRUBBED_ENV_KEYS`.
- Webhook signatures: bad sig → `401` (so GitHub stops retrying), never
  `5xx`.
- `git` errors flow through `git_ops.GitCommandError` which redacts
  `https://user:pw@host` to `https://***@host` from argv, stdout, stderr
  before raising. `host_tools._audit` only records agent-supplied args.
- Pre-push gates (`gh_push_branch`): branch matches the workspace
  branch, working tree clean, every *unpublished* commit carries
  `ROBOMP_GIT_AUTHOR_NAME` + `ROBOMP_GIT_AUTHOR_EMAIL`. The gate range
  is `origin/<head-branch>..HEAD` when the head branch already exists
  on origin (follow-ups on a Dependabot or human PR), else
  `origin/<default>..HEAD`; commits the bot did not author and did not
  create are never judged or rewritten. Commit messages carrying
  shell-literal `\n` escapes (agents quoting `git commit -m 'a\n\nb'`)
  are rewritten to real newlines — message-only, trees/identities/dates
  preserved, unpublished commits only.
- Clobber gate (`gh_push_branch`): the transport pushes with
  `--force-with-lease` pinned to the remote ref as of this event's fetch,
  so a reused (stale) worktree could otherwise drop commits pushed to the
  head branch since the bot's last push. Any commit on
  `origin/<head-branch>` that HEAD lacks and the bot did not author
  refuses the push; the agent must rebase onto the remote branch. Commits
  the bot itself authored may still be rewritten, which is what keeps
  `git commit --amend` recovery working.
- Pre-PR gates (`gh_open_pr`): when the repo defines them, `bun run fix`
  runs first (any diff amended into the agent's HEAD commit — no
  standalone `style:` noise commits) and then
  `bun check`. A failing `bun check` returns to the agent as
  `RpcCommandError` for iteration.
- `gh_open_pr` validates `## Repro` / `## Cause` / `## Fix` /
  `## Verification` headers and a `Fixes`/`Closes`/`Resolves #N`
  reference before opening.

## Operational notes

- **One PR per issue.** Follow-up events push amendments to the same
  `farm/<hex>/<slug>` branch.
- **No PR without a recorded repro.** Persona prompt requires
  `repro_record`; `mark_unable_to_reproduce` asks for missing details,
  marks the row `needs_info`, and resumes the same session on the next reply.
- **Crash recovery.** On startup, `db.reset_stuck_running()` flips
  `running` rows back to `queued`. Existing `<session_dir>/*.jsonl`
  triggers `--continue`. Drain bounded by
  `ROBOMP_SHUTDOWN_DRAIN_TIMEOUT_SECONDS` (25s) +
  `ROBOMP_SHUTDOWN_KILL_TIMEOUT_SECONDS` (5s); compose
  `stop_grace_period: 30s` covers both.
- **Logs.** Structured JSON on stdout, rotated to
  `/data/logs/robomp.log.jsonl`.
- **Inspection** (localhost only): `GET /events?limit=N`,
  `GET /issues?limit=N`, `GET /healthz`, `GET /readyz`, and the
  dashboard at `/`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `401 invalid signature` | `GITHUB_WEBHOOK_SECRET` mismatch with the repo webhook config. |
| `git push: Authentication required` | Bot PAT lacks push, or `ROBOMP_BOT_LOGIN` does not identify the PAT account's mention handle (production: `roboomp`, no `@`/`[bot]`). |
| `refusing to push: commit author identity mismatch` | One of *your own* new commits is not authored as `ROBOMP_GIT_AUTHOR_*`. The error lists the offending shas; `git commit --amend --reset-author --no-edit`. Commits already on the remote head branch are excluded, so never rewrite published history to satisfy this gate. |
| `refusing to push: this push would discard commits` | Someone pushed to the head branch after the bot's last push and the reused worktree is behind. Rebase onto `origin/<head-branch>` and re-verify; never force past it. |
| `refusing to push: working tree is dirty` | Uncommitted agent edits. Or just call `gh_open_pr`, which auto-commits `bun run fix` output. |
| `bun check failed before PR creation` | Fix the reported failure and retry `gh_open_pr`. |
| `Failed to load pi_natives` | Wrong arch / missing native. `cd python/robomp && docker compose build` and restart. |
| `No API key found for <provider>` | `agent-home-local/.omp/agent/models.yml` missing from the override tree, or provider id mismatch with `ROBOMP_MODEL`. |

## Layout

```
src/
  server.py          FastAPI app, /webhook/github, /events, /issues, /replay, dashboard at /
  github_events.py   verify_signature + route()
  queue.py           WorkerPool, dispatch loop, per-issue _inflight serialization
  tasks.py           triage_issue, handle_comment, handle_pr_conversation, handle_review, cleanup_workspace
  worker.py          synchronous omp RPC driver, prompt assembly, env scrubbing
  host_tools.py      classify_issue, set_issue_labels, gh_post_comment, repro_record,
                     gh_push_branch, gh_open_pr, gh_request_review,
                     mark_unable_to_reproduce, abort_task, fetch_issue_thread
  sandbox.py         clone pool + worktree lifecycle
  github_client.py   typed httpx client; webhook payload parsing
  proxy_client.py    GitHubProxyClient + HMAC signer
  db.py              sqlite schema + DAOs
  config.py          pydantic Settings; mode-exclusive PAT vs gh-proxy validation
  cli.py             serve / triage / replay / status / cleanup
  prompts/           system_append.md + per-task kickoff templates
tests/               pytest unit suite + one ROBOMP_INTEGRATION=1 smoke test
web/                 vite + solid dashboard, built into src/static/
```

## License

MIT.
