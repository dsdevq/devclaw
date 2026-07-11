# DevClaw environment variables

Single source of truth for every env var the runtime reads. Grouped by what
each one controls. Set in `.env` (devclaw loads it on startup), the systemd
unit, the compose file, or the shell — devclaw doesn't care.

**Convention:** empty string or unset = "use the default in the table." Bools
are truthy unless explicitly `0` / `false`. **Real env vars always win** over
`.env` — `.env` is the per-machine default surface, not an override.

The committed [`.env.example`](../.env.example) lists every var with its
default; copy it to `.env` and uncomment what you want to change. Override the
search path via `DEVCLAW_DOTENV` (must be set in the shell to bootstrap).

## Server transport + auth

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_TRANSPORT` | `stdio` | `stdio` (local dev / tests) or `http` (long-running service) |
| `DEVCLAW_PORT` | `8000` | HTTP port when `DEVCLAW_TRANSPORT=http` |
| `DEVCLAW_HOST` | `0.0.0.0` | HTTP bind address. Set `127.0.0.1` to restrict to loopback. |
| `DEVCLAW_TOKEN` | — | Bearer-token gate for every HTTP route except `/health`. Sent as `Authorization: Bearer <token>` (MCP clients) or `?token=` (dashboard/SSE). Unset = no auth (local dev). |

## State + concurrency

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_DB` | `./devclaw.db` | SQLite path. Holds the task-queue tables (programs, tasks, events) AND, since Tranche 1, the goal-state tables (`goal_status`, `goal_steering`, `goal_log`, `goal_deliveries`, `goal_docs`, `goal_phase_history`) — `GoalStore` is wired onto this same `StateStore` in production. |
| `DEVCLAW_SQLITE_BUSY_TIMEOUT_MS` | `5000` | How long a blocked writer waits for the lock before raising. The CLI and the server share the file — non-zero lets a CLI write queue instead of fail-fast when the server holds the lock. |
| `DEVCLAW_TICK_SECONDS` | `10` | Task-queue heartbeat interval. Advances DAGs + resumes recovered work. |
| `DEVCLAW_MAX_CONCURRENT` | `4` | Global cap on concurrently-running tasks. |
| `DEVCLAW_MAX_CONCURRENT_PER_PROGRAM` | `2` | Per-program concurrency cap. |
| `DEVCLAW_MAX_RETRIES` | `1` | Re-runs of a gate-failing task before escalation. Each retry feeds the failure back as steering. Timeouts are never retried. `0` disables. |
| `DEVCLAW_TASK_TIMEOUT_S` | `1800` | Per-task wall-clock cap. Exceeded → cancelled, sandbox torn down, task `failed`. `<=0` disables. |
| `DEVCLAW_VERIFY_TIMEOUT_S` | `900` | Wall-clock cap for the `verify_cmd` step (the gate that runs after the agent finishes). |
| `DEVCLAW_RATE_LIMIT_PAUSE_S` | `1800` | Default pause length when a usage/rate-limit failure is classified (gates both the task queue and goal heartbeat — zero tokens while paused). |
| `DEVCLAW_RATE_LIMIT_MAX_PAUSE_S` | `3600` | Upper bound on the pause length. |
| `DEVCLAW_SKILL_LIBRARY` | `/opt/devclaw/skill-library` | Host-side curated skill library; `Goal.skills_required` slugs are provisioned from here into the workspace's `.agent/skills/`. The repo ships the curated content in `skill-library/`. Missing library degrades soft (warning, not rejection). |
| `DEVCLAW_MAX_PAUSE_REQUEUES` | `5` | Cap on how many times a single task may be requeued across usage-limit pauses before the queue gives up on it. |
| `DEVCLAW_RATE_LIMIT_STATED_MAX_S` | `86400` | Upper bound honored when a rate-limit error states its own reset time (caps a stated reset that's implausibly far out). |
| `DEVCLAW_WORKSPACE_BREAK_THRESHOLD` | `3` | Consecutive workspace-breaking failures (same workspace) before the breaker trips. |
| `DEVCLAW_WORKSPACE_BREAK_WINDOW_S` | `900` | Rolling window the breaker counts failures within. |
| `DEVCLAW_WORKSPACE_BREAK_HOLD_S` | `1800` | How long a tripped workspace breaker holds before allowing another attempt. |

## Engine selection

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_ENGINE` | *(unset)* | `(unset)` → OpenHands in a per-task docker sandbox (production). `host` → OpenHands on the host with **no** sandbox (dev/CI, agent has full FS access). `stub` → deterministic stub (harness validation, no docker, no claude). `claude_sdk` → `claude --print` inside the sandcastle (spike; see [engine-decision.md](./engine-decision.md)). |
| `DEVCLAW_EXEC_MODEL` | `claude-sonnet-4-6` | The in-sandbox coding agent's model id (full id, not alias). Set `claude-opus-4-8` to opt a run up to Opus. Empty → ACP server's default. |
| `DEVCLAW_AGENT_TIMEOUT_S` | `1800` | (Claude-SDK engine only) Per-task wall-clock for the agent run; the outer wait adds 60s of slop for the verify gate. |
| `DEVCLAW_PLANNER_TIMEOUT_MS` | `90000` | Wall-clock for one `claude --print` planner call. |

## Sandbox (auth + resources)

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_SANDBOX_IMAGE` | `devclaw-sandbox:latest` | Per-task sandbox image (built from `.sandcastle/Dockerfile`). |
| `DEVCLAW_DOCKER_BIN` | `docker` | docker binary to spawn. |
| `DEVCLAW_SANDBOX_MEMORY` | `2g` | Hard per-container memory ceiling. `--memory-swap == --memory` disables swap growth. |
| `DEVCLAW_SANDBOX_CPUS` | `2.0` | Per-container CPU limit. |
| `DEVCLAW_HOST_CLAUDE_DIR` | `~/.claude` | Host path bind-mounted read-only into each sandbox. |
| `DEVCLAW_SANDBOX_CLAUDE_ALLOWLIST` | `.credentials.json,.claude.json` | Comma-separated entries **under** `~/.claude` to bind in. Default = the OAuth identity pair (token + identity — both needed for the ACP agentic loop). Add more only with intent; missing entries surface as docker bind errors, not silent skips. |
| `DEVCLAW_CONTAINER_PATH_PREFIX` | — | When devclaw itself runs in a container, the workspace path the host sees ≠ what devclaw sees. Set this to devclaw's view; pair with `DEVCLAW_HOST_PATH_PREFIX`. |
| `DEVCLAW_HOST_PATH_PREFIX` | — | The host-side prefix that swaps in for `DEVCLAW_CONTAINER_PATH_PREFIX` when invoking `docker run`. |
| `DEVCLAW_RUNNER_PY` | `openhands-runner/runner.py` (resolved against repo) | OpenHands runner script path (host engine mode). |
| `DEVCLAW_RUNNER_PYTHON` | derived | Python interpreter the host engine spawns the runner with. |
| `DEVCLAW_TEARDOWN_TIMEOUT_S` | `30` | Wall-clock cap on tearing down a sandbox container (`docker stop`/`rm`) before giving up and logging it as a straggler. |
| `DEVCLAW_SKILLS_DIR` | `/opt/devclaw/skills` | (In-sandbox, read by `openhands-runner/runner.py`.) Universal skill bundles baked into the sandbox image, prepended per task kind. |
| `DEVCLAW_HOOKS_DIR` | `/opt/devclaw/hooks` | (In-sandbox.) Universal pre/post hook `.sh` files, run alongside any per-repo `.agent/hooks/`. |
| `DEVCLAW_HOOK_TIMEOUT_S` | `30` | (In-sandbox.) Wall-clock cap per hook invocation; a hook that hangs is killed, never blocks the task indefinitely. |

## Auth (Pro OAuth posture)

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_CLAUDE_BIN` | `claude` | The `claude` binary the planner / Claude-SDK engine drives. |
| `DEVCLAW_TAILSCALE_BIN` | `tailscale` | Tailscale CLI used by `deploy.py` for `tailscale serve`. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | — | **Actively refused.** The sandbox runner strips these from the env before spawning the container. Set anywhere = no effect; the design pillar is Pro/Max OAuth, not metered billing. |
| `GITHUB_TOKEN` / `GH_TOKEN` | — | Repo push + PR access for `open_pr` delivery (or use a logged-in `gh`). Git access only — not cognition billing. |
| `DEVCLAW_GITHUB_OWNER` | — | GitHub account/org `create_repo` creates under (falls back to `gh`'s active login). |

## Goal layer

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_GOALS_DIR` | `~/memory/goals` | Root holding one folder per durable goal. `goal.yaml` (facts), `spec.md`, `discovery.md` are plain files; `STATUS.md` / `log.md` / `inbox.md` / `deliveries.md` / `checklist.yaml` / `firmed-draft.yaml` are generated **views** over the SQLite goal-state tables (`DEVCLAW_DB`) — human-readable, never read back for decisions. |
| `DEVCLAW_GOAL_TICK_SECONDS` | `900` | Goal heartbeat interval. Also poked in-process the moment a task settles. |
| `DEVCLAW_GOAL_EVAL_EVERY` | `3` | Deliveries between periodic direction evaluations. `0` → evaluate only at the done-gate. |
| `DEVCLAW_GOAL_FIRMING` | `0` | Opt-in: insert the `firming` phase between investigation and execution (surfaces named unknowns, writes `firmed-draft.yaml`, owner answers via `answer_unknowns`). `0`/unset → investigation resolves straight to `executing`. |
| `DEVCLAW_GOAL_FIRMING_MODEL` | `opus` | Model tier for the firming cognition. |
| `DEVCLAW_GOAL_DECOMPOSE` | `0` | Opt-in: an executing goal's `done_when` is decomposed into a structured checklist up front (Shape 2 / Pillar 1) instead of the planner inventing actions one at a time. |
| `DEVCLAW_GOAL_DECOMPOSER_MODEL` | `opus` | Model tier for the decomposer cognition. |
| `DEVCLAW_GOAL_DECOMPOSER_TIMEOUT_MS` | `300000` | Wall-clock for one decomposer `claude --print` call. |
| `DEVCLAW_GOAL_WORLD_RESEARCH_MODEL` | `opus` | Model tier for the world-research cognition (investigation phase). |
| `DEVCLAW_GOAL_WORLD_RESEARCH_TIMEOUT_MS` | `180000` | Wall-clock for one world-research `claude --print` call. |
| `DEVCLAW_GOAL_REMOTE_CHECKS` | `1` | Whether the done-gate also queries the repo's remote CI (GitHub Actions) state. `0` disables — the internal verify gate is the only check. |
| `DEVCLAW_GOAL_CI_GATE` | `flexible` | Stance when remote CI infra is broken (no workflows / infra failure / gh error): `flexible` (default — degrades to the internal verify gate, doesn't wedge the goal) or `strict` (any non-green remote state blocks). Real test failures always block in both modes. |
| `DEVCLAW_TRACE_PERSIST` | `1` | Persist cognition/tick/dispatch run-traces to the `traces` table. `0` disables (test/local convenience). |
| `DEVCLAW_GOAL_VERIFY_DONE` | `1` | Done-gate: a planner `done` proposal triggers a grounded review vs `done_when` before closing. `0` → trust the artifact-only done eval. Devclaw-wide DEFAULT — a project may override per repo (`verify_done: on\|off` via `register_project`/`update_project` / `devclaw projects … --verify-done`); the project override wins when set. |
| `DEVCLAW_GOAL_NO_PROGRESS_S` | `21600` | Wall-clock seconds an executing goal may go without a delivery before the watchdog pings the owner once. Zero-token check; complements the per-task timeout. `0` disables. |
| `DEVCLAW_GOAL_NOTIFY_URL` | — | Notify-relay endpoint for goal-level Telegram messages (free-text `/text` passthrough). |
| `DEVCLAW_GOAL_INVESTIGATE` | `1` | Whether a new outcome goal investigates the repo before executing (one-shot discovery brief). |
| `DEVCLAW_GOAL_AUTODEPLOY` | `1` | When a goal reaches `achieved`, auto-fire `deploy_project`. `0` disables. Devclaw-wide DEFAULT — a project may override per repo (`autodeploy: on\|off`); the project override wins when set. |
| `DEVCLAW_GOAL_AUTOMERGE` | `0` | Devclaw-wide DEFAULT for auto-merging a delivered PR once its verify gate passes (owner-pinged, best-effort). Off by default. A project registered via `register_project`/`update_project` can override this per repo (`automerge: on\|off`) — the project override always wins over this default when set; this env var only applies to projects with no override. There is deliberately no per-goal `automerge` field in `goal.yaml` — automerge is an ops/repo-scope decision, not something a goal's own objective carries. |
| `DEVCLAW_GOAL_MERGE_STRATEGY` | `squash` | `gh pr merge --<strategy>`. Valid: `squash` / `merge` / `rebase`. Devclaw-wide DEFAULT — a project may override per repo (`merge_strategy: squash\|merge\|rebase`); the project override wins when set. |
| `DEVCLAW_GOAL_PLAIN_SUMMARY` | `1` | One-line plain-prose summary per delivery for `deliveries.md` (one `claude` call per delivery). |
| `DEVCLAW_NOTIFY_ALTITUDE` | `owner` | Floor for goal-layer notifications: `owner` (only real blockers / direction questions / completions) or `task` (also includes per-task chatter). |

## Pre-PR review gate

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_REVIEW_GATE` | `1` | The adversarial diff-review gate after the verify gate + test-integrity pass, before the PR opens. `0` disables (escape hatch + quota lever — one `claude` call per successful code task). Devclaw-wide DEFAULT — a project may override per repo (`review_gate: on\|off`); the project override wins when set. |
| `DEVCLAW_REVIEW_MODEL` | `sonnet` | Model tier for the review-gate `claude` pass. |
| `DEVCLAW_REVIEW_MAX_DIFF_CHARS` | `60000` | Truncation cap on the diff fed to the reviewer. |

## Model tiering (cognition cost lever)

Cognition is tiered per role so autonomous runs don't burn Pro/Max quota on Opus where a lighter model does the job. **No API key = the constraint is your session quota, not a bill.** Host roles take a `claude --model` value (alias like `sonnet`/`opus` OR a full id). The exec engine takes a full model id. Empty → account default.

| Var | Default | Role |
|---|---|---|
| `DEVCLAW_PLANNER_MODEL` | `opus` | Planner (`plan_goal`) — rare, high-leverage decomposition. |
| `DEVCLAW_GRILL_MODEL` | `sonnet` | The `scope_grill` MCP tool — the chef's one-question-at-a-time cognition the OpenClaw waiter calls before filing a goal. |
| `DEVCLAW_JUDGE_MODEL` | `haiku` | Failure-analysis judge. |
| `DEVCLAW_EXEC_MODEL` | `claude-sonnet-4-6` | **The in-sandbox coding agent — the token/quota bulk.** Full id, not alias. Set `claude-opus-4-8` to opt a run up to Opus. |
| `DEVCLAW_GOAL_PLANNER_MODEL` | `sonnet` | Goal layer's next-action planner (bounded JSON, light). |
| `DEVCLAW_GOAL_EVAL_MODEL` | `sonnet` | Direction evaluator — bump to `opus` per hard direction call. |
| `DEVCLAW_GOAL_SUMMARY_MODEL` | `haiku` | Plain-prose per-delivery summary. |
| `DEVCLAW_REVIEW_MODEL` | `sonnet` | (Repeated in [Pre-PR review gate](#pre-pr-review-gate).) |

## Deploy hosting

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_DEPLOY_IMAGE` | falls back to `DEVCLAW_SANDBOX_IMAGE` | Image used for durable deploys. |
| `DEVCLAW_DEPLOY_PORT_BASE` | `8200` | Lower bound of the per-slug deterministic deploy port range. |
| `DEVCLAW_DEPLOY_PORT_SPAN` | `200` | Number of slots in the deploy port range (so `8200`–`8399` by default). |
| `DEVCLAW_DEPLOY_MEMORY` | `512m` | Per-deploy memory ceiling. |
| `DEVCLAW_DEPLOY_CPUS` | `1.0` | Per-deploy CPU limit. |
| `DEVCLAW_DEPLOY_MAX` | `5` | Max concurrent durable deploys on the VPS. |

## Cognition backend (the LLM seam)

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_COGNITION` | `claude` | Which `Cognition` impl every role's `default_caller` routes through. `claude` → `claude --print` over Pro/Max OAuth (production). `stub` → deterministic canned responses (offline harnesses + eval scaffolding). Unknown values fail loud at first use. Future backends (HTTP, local model, recorded fixture) drop into this seam without touching role modules. |

## Scope grill (waiter-side conversation, chef-side craft)

The `scope_grill` MCP tool gives the OpenClaw waiter the chef's cognition for aligning scope before filing a goal. The waiter holds the conversation; these envs tune the chef's side.

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_MAX_GRILL_QUESTIONS` | `20` | Hard cap on grill turns before the spec is force-finalized — safety net against an infinite interview. |
| `DEVCLAW_GRILL_MODEL` | `sonnet` | Model tier for the grill cognition. Conversational + judgment, but not Opus-hard. (Listed under [Model tiering](#model-tiering-cognition-cost-lever) too.) |
| `DEVCLAW_GRILL_TIMEOUT_MS` | `180000` | Wall-clock for one `scope_grill` `claude --print` turn. |

## Trend detection (self-observation)

Detects recurring failure/friction patterns across goals (e.g. the same class of
steer landing repeatedly) and writes them to the owner's vault for review — a
zero-token-by-default background signal, not a cognition role.

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_TREND_ENABLED` | `1` | Master switch for trend detection. `0` disables entirely. |
| `DEVCLAW_TREND_DISABLE` | — | Comma-separated signal ids to mute individually (e.g. `R2,H4`) while a signal is being calibrated, without disabling the rest. |
| `DEVCLAW_TREND_MODEL` | `sonnet` | Model tier when a trend signal needs a `claude` call to summarize/classify. |
| `DEVCLAW_TREND_HARNESS_SELF_FILE` | `~/memory/projects/devclaw/trends.md` | Where detected trends are appended for Denys to review. |

## What's NOT here on purpose

- The waiter agent's env (model, profile, allowed tools) lives in OpenClaw's `openclaw.json` on the VPS — not this repo. See [vps-waiter-deploy.md](./vps-waiter-deploy.md).
- Per-project verify commands and goal `done_when` strings are runtime arguments to MCP tools, not env. They belong with the project, not the host.
