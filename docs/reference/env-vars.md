# DevClaw environment variables

Single source of truth for every env var the runtime reads — enforced by
`tests/test_env_vars_doc_sync.py` (a var read in code but missing here, or
documented here but read nowhere, fails the suite). Grouped by what each one
controls. Set in `.env` (devclaw loads it on startup), the systemd unit, the
compose file, or the shell — devclaw doesn't care.

**What earns a row here:** facts that genuinely differ per host (paths, ports,
binaries, images, capacity), operator cost/behavior levers, and migration
flags. Internal tuning (protocol timeouts, retry buffer sizes, breaker
thresholds) is **code constants, changed by PR** — that's how every such value
has actually been tuned in this repo's history. If you're looking for a knob
that used to be here (per-role `*_MODEL` vars, `*_TIMEOUT_MS`,
`DEVCLAW_RATE_LIMIT_*`, `DEVCLAW_WORKSPACE_BREAK_*`, per-flag env defaults for
project-overridable behavior), it's now a named constant next to its use site.

**Convention:** empty string or unset = "use the default in the table." Bools
are truthy unless explicitly `0` / `false`. **Real env vars always win** over
`.env` — `.env` is the per-machine default surface, not an override.

The committed [`.env.example`](../../.env.example) lists every var with its
default; copy it to `.env` and uncomment what you want to change.

## Server transport + auth

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_TRANSPORT` | `stdio` | `stdio` (local dev / tests) or `http` (long-running service) |
| `DEVCLAW_PORT` | `8000` | HTTP port when `DEVCLAW_TRANSPORT=http` |
| `DEVCLAW_HOST` | `0.0.0.0` | HTTP bind address. Set `127.0.0.1` to restrict to loopback. |
| `DEVCLAW_TOKEN` | — | Bearer-token gate for every HTTP route except `/health`. Sent as `Authorization: Bearer <token>` (MCP clients) or `?token=` (dashboard/SSE). Unset = no auth (local dev). |
| `DEVCLAW_DOTENV` | `.env` (repo cwd) | Path of the `.env` file loaded at startup. Must be set in the shell to bootstrap (it can't live in the file it locates). |

## State + concurrency

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_DB` | `./devclaw.db` | SQLite path. Holds the task-queue tables (programs, tasks, events) AND, since Tranche 1, the goal-state tables (`goal_status`, `goal_steering`, `goal_log`, `goal_deliveries`, `goal_docs`, `goal_phase_history`) — `GoalStore` is wired onto this same `StateStore` in production. |
| `DEVCLAW_TICK_SECONDS` | `10` | Task-queue heartbeat interval. Advances DAGs + resumes recovered work. |
| `DEVCLAW_MAX_CONCURRENT` | `4` | Global cap on concurrently-running tasks — size to the host. |
| `DEVCLAW_MAX_CONCURRENT_PER_PROGRAM` | `2` | Per-program concurrency cap. |
| `DEVCLAW_MAX_RETRIES` | `1` | Re-runs of a gate-failing task before escalation. Each retry feeds the failure back as steering. Timeouts are never retried. `0` disables. |
| `DEVCLAW_TASK_TIMEOUT_S` | `1800` | Per-task wall-clock cap. Exceeded → cancelled, sandbox torn down, task `failed`. `<=0` disables. |
| `DEVCLAW_VERIFY_TIMEOUT_S` | `900` | Wall-clock cap for the `verify_cmd` step (the gate that runs after the agent finishes). |
| `DEVCLAW_TRACE_RETENTION_DAYS` | `30` | Days of `traces`-table telemetry to keep. The goal heartbeat prunes older rows once a day on its cheap path (batched DELETEs, pure SQLite, zero LLM). `0`, a negative value, or an unparseable value disables pruning gracefully. |

## Engine selection

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_ENGINE` | *(unset)* | `(unset)` → OpenHands in a per-task docker sandbox (production). `host` → OpenHands on the host with **no** sandbox (dev/CI, agent has full FS access). `stub` → deterministic stub (harness validation, no docker, no claude). `claude_sdk` → `claude --print` inside the sandcastle (spike; see [decisions/0002-engine-mode.md](../decisions/0002-engine-mode.md)). |
| `DEVCLAW_COGNITION` | `claude` | Which `Cognition` impl every role's `default_caller` routes through. `claude` → `claude --print` over Pro/Max OAuth (production). `stub` → deterministic canned responses (offline harnesses + eval scaffolding). `agent_sdk` → **OPT-IN** streaming backend over `claude-agent-sdk.query()` (same Pro/Max OAuth session, native liveness + structured usage/rate-limit events; requires the optional `agent-sdk` extra — `pip install -e ".[agent-sdk]"`). **Not yet live-shaken.** Unknown values fail loud at first use. |
| `DEVCLAW_COGNITION_TIMEOUT_S` | `180` | Inactivity/overall budget (seconds) for the `agent_sdk` backend: each yielded message resets an inactivity window; no message within it closes the stream (killing the spawned `claude`) and raises a timed-out `PlannerError`. Invalid/unset → 180. A per-call `timeout_ms` overrides it. (The `claude` backend's timeout is `PLANNER_TIMEOUT_MS` in `call_claude`, not this var.) |

## Model tiering (cognition cost lever)

Cognition cost is steered by **three tiers**, not per-role vars. Which role
runs at which tier is a code decision — the table in
[`devclaw/model_tiers.py`](../../devclaw/model_tiers.py) — changed by PR (the
twelve per-role vars this replaced were never once set on any host). Tier
values are `claude --model` inputs: an alias (`haiku`/`sonnet`/`opus`) or a
full id. Empty → account default. **No API key = the constraint is your
session quota, not a bill.**

| Var | Default | Runs |
|---|---|---|
| `DEVCLAW_MODEL_DEEP` | `opus` | Rare, high-leverage calls: plan_goal, firming, decomposer, world-research. |
| `DEVCLAW_MODEL_STANDARD` | `sonnet` | Judgment at volume: goal planner, direction evaluator, scope grill, review gate, trend classification. |
| `DEVCLAW_MODEL_LIGHT` | `haiku` | Mechanical prose: per-delivery summaries, failure-analysis judge. |
| `DEVCLAW_EXEC_MODEL` | `claude-sonnet-4-6` | **The in-sandbox coding agent — the token/quota bulk.** Full id, not alias. Set `claude-opus-4-8` to opt a run up to Opus. Empty → ACP server's default. |

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
| `DEVCLAW_SKILLS_DIR` | `/opt/devclaw/skills` | (In-sandbox, read by `openhands-runner/runner.py`.) Universal skill bundles baked into the sandbox image, prepended per task kind. |
| `DEVCLAW_HOOKS_DIR` | `/opt/devclaw/hooks` | (In-sandbox.) Universal pre/post hook `.sh` files, run alongside any per-repo `.agent/hooks/`. |

## Auth (Pro OAuth posture)

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_CLAUDE_BIN` | `claude` | The `claude` binary the planner / Claude-SDK engine drives. |
| `DEVCLAW_TAILSCALE_BIN` | `tailscale` | Tailscale CLI used by `deploy.py` for `tailscale serve`. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | — | **Actively refused.** The sandbox runner strips these from the env before spawning the container. Set anywhere = no effect; the design pillar is Pro/Max OAuth, not metered billing. |
| `GITHUB_TOKEN` / `GH_TOKEN` | — | Repo push + PR access for `open_pr` delivery (or use a logged-in `gh`). Git access only — not cognition billing. |
| `DEVCLAW_GITHUB_OWNER` | — | GitHub account/org `create_repo` creates under (falls back to `gh`'s active login). |

## Goal layer

Behavior that a **project** can own is not env anymore: `automerge`,
`merge_strategy`, `review_gate`, `verify_done`, `autodeploy` and the CI-gate
stance resolve as *code default → project-registry override* (set via
`register_project` / `update_project`). The env middle-layer was removed —
three precedence layers with divergent defaults was a debugging trap.

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_GOALS_DIR` | `~/memory/goals` | Root holding one folder per durable goal. `goal.yaml` (facts), `spec.md`, `discovery.md` are plain files; `STATUS.md` / `log.md` / `inbox.md` / `deliveries.md` / `checklist.yaml` / `firmed-draft.yaml` are generated **views** over the SQLite goal-state tables (`DEVCLAW_DB`) — human-readable, never read back for decisions. |
| `DEVCLAW_GOAL_TICK_SECONDS` | `900` | Goal heartbeat interval. Also poked in-process the moment a task settles. |
| `DEVCLAW_GOAL_EVAL_EVERY` | `3` | Deliveries between periodic direction evaluations (quota lever). `0` → evaluate only at the done-gate. |
| `DEVCLAW_GOAL_FIRMING` | `0` | Migration flag: insert the `firming` phase between investigation and execution (surfaces named unknowns, owner answers via `answer_unknowns`). `0`/unset → investigation resolves straight to `executing`. |
| `DEVCLAW_GOAL_DECOMPOSE` | `0` | Migration flag: an executing goal's `done_when` is decomposed into a structured checklist up front (Shape 2 / Pillar 1) instead of the planner inventing actions one at a time. |
| `DEVCLAW_GOAL_INVESTIGATE` | `1` | Migration flag: a new outcome goal investigates the repo before executing (one-shot discovery brief). |
| `DEVCLAW_GOAL_REMOTE_CHECKS` | `1` | Ops kill-switch: whether the done-gate also queries the repo's remote CI (GitHub Actions) state. `0` disables — the internal verify gate is the only check. |
| `DEVCLAW_GOAL_NO_PROGRESS_S` | `21600` | Wall-clock seconds an executing goal may go without a delivery before the watchdog pings the owner once. Zero-token check; complements the per-task timeout. `0` disables. |
| `DEVCLAW_GOAL_NOTIFY_URL` | — | Notify-relay endpoint for goal-level Telegram messages (free-text `/text` passthrough). |
| `DEVCLAW_GOAL_PLAIN_SUMMARY` | `1` | One-line plain-prose summary per delivery for `deliveries.md` (quota lever — one `claude` call per delivery). |
| `DEVCLAW_NOTIFY_ALTITUDE` | `owner` | Floor for goal-layer notifications: `owner` (only real blockers / direction questions / completions) or `task` (also includes per-task chatter). |

## Deploy hosting

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_DEPLOY_IMAGE` | falls back to `DEVCLAW_SANDBOX_IMAGE` | Image used for durable deploys. |
| `DEVCLAW_DEPLOY_PORT_BASE` | `8200` | Lower bound of the per-slug deterministic deploy port range. |
| `DEVCLAW_DEPLOY_PORT_SPAN` | `200` | Number of slots in the deploy port range (so `8200`–`8399` by default). |
| `DEVCLAW_DEPLOY_MEMORY` | `512m` | Per-deploy memory ceiling. |
| `DEVCLAW_DEPLOY_CPUS` | `1.0` | Per-deploy CPU limit. |
| `DEVCLAW_DEPLOY_MAX` | `5` | Max concurrent durable deploys on the VPS. |

## Trend detection (self-observation)

Detects recurring failure/friction patterns across goals (e.g. the same class of
steer landing repeatedly) and writes them to the owner's vault for review — a
zero-token-by-default background signal, not a cognition role.

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_TREND_ENABLED` | `1` | Master switch for trend detection. `0` disables entirely. |
| `DEVCLAW_TREND_DISABLE` | — | Comma-separated signal ids to mute individually (e.g. `R2,H4`) while a signal is being calibrated, without disabling the rest. |
| `DEVCLAW_TREND_HARNESS_SELF_FILE` | `~/memory/projects/devclaw/trends.md` | Where detected trends are appended for Denys to review. |

## What's NOT here on purpose

- The waiter agent's env (model, profile, allowed tools) lives in OpenClaw's `openclaw.json` on the VPS — not this repo. See [runbooks/vps-waiter-deploy.md](../runbooks/vps-waiter-deploy.md).
- Per-project verify commands and goal `done_when` strings are runtime arguments to MCP tools, not env. They belong with the project, not the host.
- The eval-harness `MEASURE_*` vars (see `evals/measure_passrate.py`) and the test-suite gates (`DEVCLAW_RUN_COGNITION_EVALS`, `DEVCLAW_TEST_*`) — offline tooling, not the runtime.
- Internal tuning constants (timeouts, retry buffers, breaker thresholds, review diff caps) — named constants at their use sites, tuned by PR.
