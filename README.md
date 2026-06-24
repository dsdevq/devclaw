# devclaw

> **DevClaw is the chef. The waiter — an OpenClaw agent — takes orders; devclaw cooks.**

DevClaw owns the **craft of software development as a service**: durable goals, planning, decomposition, sandbox execution (via OpenHands), pre-PR review, gate verification, durable Tailscale deploys, and grounded direction evaluation. It sits behind MCP and is called by an **OpenClaw waiter agent** that translates Denys's chat into structured tool calls — devclaw doesn't talk to the user, it cooks.

Cognition is always `claude` over a Pro/Max OAuth session — **no `ANTHROPIC_API_KEY`, no metered billing** for autonomous runs.

It is **not** a chatbot and **not** a rebuild of OpenHands. OpenHands owns the agent loop (tool use, code edits, git). DevClaw owns everything *around* it: durable goals + direction evaluation, decomposition, state, isolation, observability, delivery, and deploy.

```
Denys
  │  (chat / voice / Telegram)
  ▼
OpenClaw waiter agent          ← translates chat ↔ MCP, doesn't decide
  │
  ▼
DevClaw (the chef — this repo, FastMCP)
  ├── goal/    durable goals → heartbeat tick → plan + dispatch + evaluate
  ├── server/  FastMCP stdio + streamable-HTTP, dashboard + SSE, auth
  ├── loom/    reusable orchestration core (failure classification, test integrity)
  ├── planner.py · review_gate.py · delivery.py · deploy.py · …
  └── sandcastle_runner — `docker run --rm` per task; RO ~/.claude mount; destroyed on exit
        │
        ▼
  OpenHands (Python SDK) — agent loop, runs `claude` via ACP (Pro OAuth)
```

## The split

| Concern | Owner |
|---|---|
| Conversation with Denys | **OpenClaw waiter agent** (system prompt + tool calls) |
| Agent loop, sandbox coding, git | **OpenHands** |
| Goal → tasks decomposition, direction eval, review gate | DevClaw |
| Task/program state | DevClaw state store (SQLite) |
| Per-task isolation | DevClaw sandcastle runner (`docker run`) |
| Durable hosting / handoff | DevClaw deploy (Tailscale, reboot-surviving) |
| Interface to the waiter | DevClaw FastMCP server |

The full rationale — including why OpenHands and sandbox isolation are **orthogonal** layers (the agent vs. the box it runs in), and why this calls `docker run` directly instead of depending on `@ai-hero/sandcastle` — lives in [`docs/architecture-v2.md`](./docs/architecture-v2.md).

## Layout

```
devclaw/
├── server/             # MCP server (FastMCP) — split by job:
│   ├── __init__.py     #   re-exports + load-order
│   ├── _state.py       #   FastMCP instance + long-lived services + env
│   ├── tools.py        #   every @mcp.tool decorator (the chef's menu)
│   ├── http.py         #   every @mcp.custom_route (dashboard, SSE, /goals/answer)
│   └── lifecycle.py    #   main() + serve loops + bearer-token auth middleware
├── goal/               # the durable goal layer (folded-in goalclaw):
│   ├── service.py      #   GoalService — the facade the server wires up
│   ├── tick.py         #   one heartbeat: check → plan → evaluate → dispatch → done-gate
│   ├── planner.py      #   next-action cognition (one claude --print per tick past the gate)
│   ├── evaluator.py    #   direction evaluation, grounded in deliveries.md
│   ├── store.py        #   on-disk mind: goal.yaml · STATUS.md · log.md · inbox.md · deliveries.md
│   ├── engine.py       #   in-process dispatch into the task queue
│   ├── grill.py        #   per-goal scope grill (off by default)
│   ├── research.py · merge.py · notify.py · summary.py · models.py
├── loom/               # reusable orchestration core:
│   ├── limits.py       #   usage-/rate-limit failure classifier (pure)
│   └── test_integrity.py # gate guard: flags deleted/weakened tests in a diff (pure)
├── planner.py          # spec / program planner (claude --print) → task DAG
├── state_store.py      # SQLite: programs, tasks, append-only events
├── task_queue.py       # async task lifecycle, concurrency, on-settle hook → goal poke
├── sandcastle_runner.py# docker run --rm per task; events stream from the runner
├── delivery.py         # engineer-authored commit → branch → push → PR
├── review_gate.py      # pre-PR adversarial diff review (claude)
├── deploy.py           # durable Tailscale deploy hosting (reboot-surviving)
├── project_registry.py # control plane: repos → driving goals → live status rollup
├── cli.py              # devclaw projects … (terminal face of the control plane)
└── workspace.py        # per-action pristine git checkout (devclaw owns the checkout)
openhands-runner/runner.py  # OpenHands SDK inside the sandbox; emits event/result lines
.sandcastle/Dockerfile      # per-task sandbox image
tests/                      # pytest — stubbed engine; no docker, no claude
docs/architecture-v2.md     # architectural contract — read before touching the runner/store/sandbox
```

DevClaw is all Python. The only language boundary left is the process boundary: `openhands-runner/runner.py` runs the OpenHands SDK *inside* the sandbox container, isolated from the long-running host process — it talks to the host over a line-delimited JSON protocol on stdout.

## MCP tools (the chef's menu)

| Tool | Does |
|---|---|
| `implement_feature(workspace_dir, goal, …)` | Run a single feature task |
| `fix_bug(workspace_dir, description, …)` | Run a single bug-fix task |
| `review_repository(workspace_dir, …)` | Read-only review (no writes — invariant runtime-checked) |
| `onboard(workspace_dir, …)` | Analyze a repo and write a draft `AGENTS.md` (comprehension only) |
| `setup_cicd(workspace_dir)` | Commit a self-hosted GitHub Actions workflow if none exists |
| `create_repo(name, …)` | Stand up a fresh GitHub repo for a from-scratch goal |
| `start_program(workspace_dir, goal, …)` | Decompose a large goal into a task DAG and run it |
| `get_program(program_id)` / `list_programs()` | Program status + task DAG |
| `get_status(task_id)` / `list_tasks(...)` / `get_events(...)` | Task history + replayable event feed (live SSE over HTTP) |
| `cancel_task(task_id)` / `cancel_program(program_id)` | Abort in-flight work — tears down the sandbox |

Async by default: a tool call returns a `task_id` immediately and the work runs in the background. Pass a `notify_url` to get a callback on completion/block instead of polling.

### Durable goals (the goal layer)

A `program` runs once to completion; a **goal** is a standing intent DevClaw advances across many heartbeats — and judges for *direction*, not just shipped PRs.

| Tool | Does |
|---|---|
| `create_goal(goal_id, objective, workspace_dir, done_when, backlog, …)` | Register a durable goal DevClaw drives over time |
| `get_goal(goal_id)` | Objective, phase, what's in flight, the latest direction verdict, recent log |
| `list_goals()` | All goals + phase + direction |
| `steer_goal(goal_id, message)` | Correct/redirect — recorded as steering, honored on the next tick |
| `tail_goal(goal_id, …)` | Deep read-only feed: deliveries tail (what each action actually shipped) + recent events |
| `answer_goal(goal_id, answer)` | Reply to a goal waiting on the owner (Telegram answer channel for scope questions) |
| `cancel_goal(goal_id)` | Permanently stop a goal — terminal `cancelled`, tears down any in-flight action |

**How a goal is driven (per heartbeat):**
1. **Cheap check** (0 tokens) — poll the in-flight action via a local SQLite read.
2. **Per-delivery evidence** (0 tokens) — on a finished action, read the *full* task result (agent output + gate verdict) and append a grounded note to `deliveries.md`.
3. **Next-action plan** (1 LLM call, only past the gate) — pick the single next action from the backlog/steering and dispatch it in-process.
4. **Direction evaluation** (periodic LLM call) — every `DEVCLAW_GOAL_EVAL_EVERY` deliveries, judge whether the *delivered work* is achieving the objective; corrections are fed back as steering, a hard verdict blocks.
5. **Done-gate** — the planner's `done` is only a *proposal*; it triggers a read-only `review_repository` against `done_when`, and the goal closes **only if the evaluator confirms `achieved`** from that review. "Done" is gated on grounded evaluation, not on counting PRs.

The zero-token idle guard is load-bearing: an idle goal and an in-flight-still-running goal cost **0 `claude` calls** (the heartbeat is mechanism; cognition runs only when there's real work).

### The project registry (control plane)

The single source of truth for **"which repos is devclaw working on, and what's the status of each"** — one entity above the tasks/programs/goals primitives, drivable from chat, API, *and* CLI. A `Project` is a thin record (repo · workspace · status · the goal(s) driving it); it links goals **by id** and joins their live status on read, so it never caches phase and never rots.

| Tool | Does |
|---|---|
| `register_project(project_id, name, …)` | Register a repo in the portfolio (slug id; optional repo_url / workspace_dir) |
| `list_projects(status?)` | Every project + a live rollup: each linked goal's phase/direction + derived health |
| `project_status(project_id)` | Full status of one project (facts + live goal status) |
| `update_project(project_id, …)` | Update facts — pause/archive, fix repo/workspace |
| `link_goal(project_id, goal_id, unlink?)` | Attach/detach a durable goal (by id; status joined live) |
| `delete_project(project_id)` | Hard-delete a project record (goals untouched) |

Same control plane from a terminal (talks to the same stores; no server needed):

```bash
devclaw projects list                 # or: python -m devclaw.cli projects list
devclaw projects show todo-fullstack-demo
devclaw projects register todo "Todo App" --repo-url git@github.com:me/todo.git
devclaw projects link todo-fullstack-demo todo-quality-audit
```

…and a portfolio view at **`/projects`** on the HTTP dashboard.

### Durable deploy hosting

The handoff for an `achieved` goal: a running product the owner opens, not a diff to read.

| Tool | Does |
|---|---|
| `deploy_project(workspace_dir, slug)` | Durable deploy → stable Tailscale `https://<node>.<tailnet>.ts.net:<port>/` URL that survives reboots. Auto-fires when a goal reaches `achieved`. |
| `deploy_status(slug)` / `list_deploys()` | Status of one deploy (exists / running / ready + stable URL) / list them all |
| `stop_deploy(slug)` | Stop a deploy, tear down its Tailscale serve, free its VPS resources |

Tailscale wiring is best-effort + graceful-degradation: `deploy_project` attempts `tailscale serve` and, if devclaw's container can't reach tailscaled, returns the one-time serve command (which then persists across reboots). Mounting the tailscaled socket into the devclaw-mcp container makes it fully automatic with no code change.

### Reliability & quality

Built to run unattended, and to ship code worth merging:

- **Survives usage limits.** A quota / rate-limit pause is *classified*, not treated as a failure: the task is requeued and a single account-wide `paused_until` gates **both** the task queue and the goal heartbeat, which auto-resume when the cap resets — zero tokens while paused, the owner pinged once.
- **No-progress watchdog.** An executing goal that ships nothing for `DEVCLAW_GOAL_NO_PROGRESS_S` (default 6h) pings the owner once — a zero-token wall-clock check that complements the per-task timeout.
- **In-house quality gate (no third-party QC).** The engineer is briefed to *audit before extending*, and the verify gate runs a **test-integrity** check that fails the gate on deleted / skipped / weakened tests, closing the "go green by gutting the tests" path.
- **Pre-PR review gate — green means *reviewed*.** A green gate proves behaviour, not quality; it can't see a dead-code line or a frontend change it never exercises. So after the gate + test-integrity pass and **before** the PR opens, a separate `claude` pass *reads the diff* against the ticket + quality bar and returns `approve` / `request_changes`. A `request_changes` verdict feeds its located issues back through the same retry loop as a gate failure (then escalates).

## Auth (the design constraint)

DevClaw inherits a `claude` OAuth session — it never uses an API key. `ANTHROPIC_API_KEY` is **actively refused** at both the host (planner) and sandbox layers so a stray key can't silently switch autonomous runs onto metered billing. All you need is a logged-in `claude` CLI: the planner shells out to it, and the per-task sandbox bind-mounts an explicit allowlist under `~/.claude` **read-only** (the credential token + `.claude.json` identity by default; nothing else).

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
pip install -r openhands-runner/requirements.txt   # only inside the sandbox image
npm install -g @agentclientprotocol/claude-agent-acp

DEVCLAW_TRANSPORT=stdio devclaw-mcp        # local dev (MCP over stdio)
# or HTTP for a long-running service:
DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000 devclaw-mcp
#   → MCP at /mcp, dashboards at /dashboard (programs) · /goals · /projects, SSE at /programs/:id/events
```

(`devclaw-mcp` is the console script for the server; `devclaw` is the control-plane CLI; `python -m devclaw.server` / `python -m devclaw.cli` work too.)

### Engine modes (`DEVCLAW_ENGINE`)

| Value | Engine | Isolation | Use |
|---|---|---|---|
| *(unset)* | OpenHands in a per-task **docker sandbox** | ✅ full | production |
| `host` | OpenHands **on the host** (no container) | ⚠ **none** — agent has full filesystem access | dev/CI/validation where docker is unavailable |
| `stub` | deterministic stub (no OpenHands, no claude) | n/a | harness validation (`evals/`) |

### Useful env

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_TRANSPORT` | `stdio` | `stdio` or `http` |
| `DEVCLAW_PORT` | `8000` | HTTP port |
| `DEVCLAW_HOST` | `0.0.0.0` | HTTP bind address (set `127.0.0.1` to restrict to loopback) |
| `DEVCLAW_TOKEN` | — | Bearer-token gate for HTTP routes except `/health` (via `Authorization: Bearer <token>` or `?token=`). Unset = no auth (local dev). |
| `DEVCLAW_DB` | `./devclaw.db` | SQLite path for state |
| `DEVCLAW_MAX_CONCURRENT` | `4` | global cap on concurrently-running tasks |
| `DEVCLAW_MAX_CONCURRENT_PER_PROGRAM` | `2` | per-program concurrency cap |
| `DEVCLAW_TICK_SECONDS` | `10` | task-queue heartbeat interval |
| `DEVCLAW_SQLITE_BUSY_TIMEOUT_MS` | `5000` | how long a blocked SQLite writer waits for the lock |
| `DEVCLAW_GOALS_DIR` | `~/memory/goals` | root holding one folder per durable goal |
| `DEVCLAW_GOAL_TICK_SECONDS` | `900` | goal heartbeat interval — also woken in-process the moment a task settles |
| `DEVCLAW_GOAL_EVAL_EVERY` | `3` | deliveries between periodic direction evaluations (`0` → evaluate only at the done-gate) |
| `DEVCLAW_GOAL_NO_PROGRESS_S` | `21600` | wall-clock seconds an executing goal may go without a delivery before the watchdog pings the owner once (`0` disables) |
| `DEVCLAW_GOAL_VERIFY_DONE` | `1` | done-gate: planner `done` triggers a grounded review vs `done_when` before closing (`0` → trust artifact-only done eval) |
| `DEVCLAW_GOAL_NOTIFY_URL` | — | notify-relay endpoint for goal-level Telegram messages |
| `DEVCLAW_TASK_TIMEOUT_S` | `1800` | per-task wall-clock cap — a hung run is cancelled, sandbox torn down, task marked `failed` |
| `DEVCLAW_MAX_RETRIES` | `1` | re-runs of a gate-failing task, each with the failure fed back as steering, before escalating |
| `DEVCLAW_REVIEW_GATE` | `1` | the pre-PR adversarial review gate — `0` disables (escape hatch + quota lever) |
| `DEVCLAW_REVIEW_MODEL` | `sonnet` | model tier for the review-gate `claude` pass |
| `GITHUB_TOKEN` / `GH_TOKEN` | — | repo push + PR access for `open_pr` delivery (or use a logged-in `gh`) |
| `DEVCLAW_VERIFY_TIMEOUT_S` | `900` | wall-clock cap for the `verify_cmd` run after the agent finishes |
| `DEVCLAW_SANDBOX_IMAGE` | `devclaw-sandbox:latest` | per-task sandbox image |
| `DEVCLAW_CLAUDE_BIN` | `claude` | the `claude` binary the planner drives |
| `DEVCLAW_HOST_CLAUDE_DIR` | `~/.claude` | host path bind-mounted read-only into each sandbox |
| `DEVCLAW_SANDBOX_CLAUDE_ALLOWLIST` | `.credentials.json,.claude.json` | comma-separated entries **under** `~/.claude` to bind into the sandbox |

### Model tiering

Cognition is tiered per role so an autonomous run doesn't burn quota on Opus where a lighter model does the job. Host roles take a `claude --model` value (alias like `sonnet`/`opus`); the exec engine takes a full model id. Set any to empty to fall back to the account default.

| Var | Default | Role |
|---|---|---|
| `DEVCLAW_PLANNER_MODEL` | `opus` | planner (`plan_goal`) — rare, high-leverage decomposition |
| `DEVCLAW_JUDGE_MODEL` | `haiku` | failure-analysis judge |
| `DEVCLAW_EXEC_MODEL` | `claude-sonnet-4-6` | **the OpenHands coding agent — the token/quota bulk.** Set `claude-opus-4-8` to opt a run up to Opus. |
| `DEVCLAW_GOAL_PLANNER_MODEL` | `sonnet` | the goal layer's next-action planner |
| `DEVCLAW_GOAL_EVAL_MODEL` | `sonnet` | the direction evaluator (bump to `opus` per goal for hard direction calls) |

## Tests

```bash
pip install -e ".[dev]"
pytest          # planner + state store + queue/DAG + goal layer, all stubbed — no docker, no claude
```

To validate the **real** pipeline (a logged-in `claude` driving OpenHands in a docker sandbox), follow the layered runbook in [`docs/live-shakedown.md`](./docs/live-shakedown.md).

## Status

DevClaw is the live runtime. As of mid-2026 it serves as the chef behind an OpenClaw waiter agent — the spec-kit elicitation flow (`build_project` / `answer_question` / `approve_spec`) and the preview hosting module were removed (drift; vault-rejected), and the durable goal layer + Tailscale deploys carry the load.

## What this is NOT

- **Not a chatbot.** It's a backend service the OpenClaw waiter calls.
- **Not a general assistant.** It executes software-development goals, nothing else.
- **Not a rebuild of OpenHands.** OpenHands is the execution engine; DevClaw is the orchestration above it.

## License

[MIT](./LICENSE). Copyright 2026 Denys Sychov.
