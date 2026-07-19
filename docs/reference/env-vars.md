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
| `DEVCLAW_ITEM_MAX_ATTEMPTS` | `3` | Structural per-checklist-item circuit breaker: after this many straight FAILED settles of the same item, it flips to `blocked` and the goal is parked for a human (`needs_human`) instead of the planner re-picking the same failing ticket. `<=0` disables. |
| `DEVCLAW_TASK_TIMEOUT_S` | `1800` | Per-task wall-clock cap. Exceeded → cancelled, sandbox torn down, task `failed`. `<=0` disables. |
| `DEVCLAW_VERIFY_TIMEOUT_S` | `900` | Wall-clock cap for the `verify_cmd` step (the gate that runs after the agent finishes). |
| `DEVCLAW_TRACE_RETENTION_DAYS` | `30` | Days of `traces`-table telemetry to keep. The goal heartbeat prunes older rows once a day on its cheap path (batched DELETEs, pure SQLite, zero LLM). `0`, a negative value, or an unparseable value disables pruning gracefully. |
| `DEVCLAW_EVENTS_RETENTION_DAYS` | `30` | Days of `events`-table history to keep (raw runner SDK events, one row per agent action — the highest-volume append-only log after traces). Pruned by the same daily, batched, pure-SQLite, zero-LLM heartbeat pass as the trace prune, on an independent watermark. `0`, a negative value, or an unparseable value disables pruning gracefully. |
| `DEVCLAW_DB_SIZE_ALERT_MB` | `2000` | Size (MB) at which the heartbeat pings the owner ONCE that `devclaw.db` (incl. the WAL sidecar) has grown too large despite retention+VACUUM — the loud-not-silent guard against a silent disk-fill wedge. Re-arms when size drops back under. `0`, a negative value, or an unparseable value disables the alarm gracefully. |

## Engine selection

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_ENGINE` | *(unset)* | `(unset)` → OpenHands in a per-task docker sandbox (production). `host` → OpenHands on the host with **no** sandbox (dev/CI, agent has full FS access). `stub` → deterministic stub (harness validation, no docker, no claude). `claude_sdk` → `claude --print` inside the sandcastle (spike; see [decisions/0002-engine-mode.md](../decisions/0002-engine-mode.md)). |
| `DEVCLAW_COGNITION` | `claude` | Which `Cognition` impl every role's `default_caller` routes through. `claude` → `claude --print` over Pro/Max OAuth (production). `stub` → deterministic canned responses (offline harnesses + eval scaffolding). `agent_sdk` → **OPT-IN** streaming backend over `claude-agent-sdk.query()` (same Pro/Max OAuth session, native liveness + structured usage/rate-limit events; requires the optional `agent-sdk` extra — `pip install -e ".[agent-sdk]"`). **Not yet live-shaken.** Unknown values fail loud at first use. |
| `DEVCLAW_COGNITION_TIMEOUT_S` | `180` | Default budget (seconds) for one cognition call, read by both backends. `claude`: wall-clock cap on one `claude --print` call (`PLANNER_TIMEOUT_MS` in `call_claude`) — a peak-hour latency lever (successful calls measured 50–78s while the old hardcoded 90s cap timed five out at exactly the cap, 2026-07-14/15). `agent_sdk`: inactivity window — each yielded message resets it; a stall closes the stream (killing the spawned `claude`) and raises a timed-out `PlannerError`. Roles with their own budget (review 180s, decomposer 300s) are unaffected; a per-call `timeout_ms` overrides it. Invalid / `<=0` / unset → 180. |

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
| `DEVCLAW_MODEL_DEEP` | `opus` | Rare, high-leverage calls: firming, decomposer (the one planning spine — goals AND programs), world-research. |
| `DEVCLAW_MODEL_STANDARD` | `sonnet` | Judgment at volume: goal planner, direction evaluator, scope grill, review gate, trend classification. |
| `DEVCLAW_MODEL_LIGHT` | `haiku` | Mechanical prose: per-delivery summaries, failure-analysis judge. |
| `DEVCLAW_EXEC_MODEL` | `claude-sonnet-4-6` | **The in-sandbox coding agent — the token/quota bulk.** Full id, not alias. Set `claude-opus-4-8` to opt a run up to Opus. Empty → ACP server's default. |
| `DEVCLAW_ACP_COMMAND` | *(unset)* → `claude-agent-acp` | **The ACP agent command the worker session runs on** — the layer-5 replaceability seam. A string like `my-acp --profile x`; the runner shlex-splits it. Read host-side and threaded via the runner JSON payload (host env does NOT cross the container boundary; the runner's own env read only serves manual `docker run` / host-engine runs). Scope caveat: this swaps the *command only* — the `acp_env` (CLAUDE_* vars), the `~/.claude` auth mounts, `DEVCLAW_EXEC_MODEL`'s claude model ids, and the auth/rate-limit classifiers are still claude-shaped, and the alternate binary must be baked into the sandbox image. |

## Quality gate (pre-PR review)

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_REVIEW_PANEL_N` | `1` | Panelists in the pre-PR adversarial review panel (`quality.review_panel`). `1` = the single reviewer, byte-identical to the historical gate (delegates straight to `review_diff`). `N>=2` fans out N reviewers in parallel over the SAME diff under **diverse lenses** (`correctness`, `regression_risk`, `meets_acceptance_criteria`, round-robin if `N` exceeds the lens set); their blocking issues are **unioned** (evidence wins), so the panel is strictly ≥ as strict as one reviewer. Fails **CLOSED on sub-quorum**: fewer than `ceil(N/2)` valid votes (a panelist crash/unparseable output is a non-vote) raises, failing the task closed and fast — never an approval. Prefer an **odd** N (at even N the quorum `ceil(N/2)` tolerates only a bare minority of crashes; e.g. N=2 approves on one valid vote). **Opt-in**; recommended production value `3` after a live shakedown. Each panelist's vote is persisted as an append-only `review_vote` event. |
| `DEVCLAW_REVIEW_DEGRADE` | `1` | The pre-PR review gate's **cognition-timeout degradation ladder** (`quality.review_panel`). When the whole-diff review **times out** (a large-but-legitimate diff exhausts the review budget), the ladder splits the diff **one sub-diff per file**, reviews each independently, and **unions** the verdicts (same evidence-wins semantics as the panel — a single file's blocker forces `request_changes`), so a large diff can still earn a real verdict. (Trade-off: per-file review loses **cross-file context**, so a regression spanning two files can pass where a whole-diff review would block it — accepted because this path otherwise hard-fails the diff outright, and every fail-closed guarantee still holds.) Fail-closed is preserved end to end: the trigger is **timeout only** (an unparseable verdict re-raises unchanged); a per-file sub-review that still times out / can't parse **raises** → the whole diff fails closed (never an approval), carrying its raw response for the quota classifier; when the ladder can't help it re-raises the original timeout → the same crash-marker, no-agent-retry path (#186). `0`/`false`/`no`/`off` disables it (a review timeout re-raises immediately, byte-identical to the pre-ladder gate). |
| `DEVCLAW_REVIEW_DEGRADE_MAX_FILES` | `40` | Per-file fan-out cap for the degradation ladder above. A diff with **more** reviewable files than this is **not** degraded (the fan-out would be too large a burst of model calls); it fails closed and a human splits the commit. Clamped to `>=1`; unparseable → the default. |

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
| `DEVCLAW_GOAL_BROWSER_GATE` | `1` | Whether the settle path enforces the browser-E2E gate: a change touching a web-UI path must carry a passing real-browser Playwright run (proven via the runner's `browser_report` counts) before it ships. `0` disables — UI changes are gated by verify + review only (the pre-2026-07-17 behaviour). |
| `DEVCLAW_GOAL_BROWSER_GATE_MODE` | `flexible` | Browser-gate stance for a frontend change when the project has **no** Playwright suite at all (`absent`). `flexible` (default) falls through with a loud log so a not-yet-E2E'd project isn't wedged; `strict` blocks, forcing E2E adoption. A **failed or un-run** suite (`ran_failed`/`never_ran`) blocks in **both** modes — that's evidence, not capability uncertainty. Mirrors `DEVCLAW_GOAL_CI_GATE`. |
| `DEVCLAW_GOAL_BROWSER_REACHABILITY` | `1` | The reasoned escape valve for the browser gate's one false positive: a UI change **not rendered in the running app** (a library component no route imports yet). When the gate is about to block a *no-run* frontend change, an independent, grounded judge (the `reachability` cognition role) may clear it — but **only** on a proven `reachable == "no"`; reachable / unknown / a real browser failure (`ran_failed`) / a judge crash all leave the fail-closed block standing. Strictly safe (can only relax a would-be block). `0` reverts to the pure mechanical gate. Moot when `DEVCLAW_GOAL_BROWSER_GATE=0`. |
| `DEVCLAW_GOAL_NO_PROGRESS_S` | `21600` | Wall-clock seconds an executing goal may go without a delivery before the watchdog pings the owner once. Zero-token check; complements the per-task timeout. `0` disables. |
| `DEVCLAW_GOAL_NOTIFY_URL` | — | Notify-relay endpoint for goal-level Telegram messages (free-text `/text` passthrough). |
| `DEVCLAW_GOAL_PLAIN_SUMMARY` | `1` | One-line plain-prose summary per delivery for `deliveries.md` (quota lever — one `claude` call per delivery). |
| `DEVCLAW_NOTIFY_ALTITUDE` | `owner` | Floor for goal-layer notifications: `owner` (only real blockers / direction questions / completions) or `task` (also includes per-task chatter). |
| `DEVCLAW_SELF_TRIAGE` | `1` | Propose-only self-triage interceptor: before an **eligible** owner ping fires (slice 1 allowlist = the DB-size alarm), a bounded triage cognition step dedupes it against the `problems` catalog and drafts a proposed fix, delivering "problem + proposed fix + how to approve" instead of a bare alert. Runs ONLY when a real ping fires (never idle — zero-token guard intact); any triage failure falls back to the raw ping (loud, not silent). `0`/`false` reverts every eligible ping to the raw path. |

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
