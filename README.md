# devclaw

> **DevClaw is the software-development project manager: it owns durable goals, drives them to verified PRs, and evaluates whether the work is going the right direction — autonomously, with no API key.**

DevClaw sits in front of [OpenHands](https://github.com/All-Hands-AI/OpenHands) (the engineer) as the PM. Two altitudes:

- **Task / program** (one-shot): hand it a bounded goal over MCP (`implement_feature`, `fix_bug`, `review_repository`) or a larger one to decompose (`start_program`); it plans, runs OpenHands inside a per-task ephemeral Docker sandbox, gate-verifies, and delivers a reviewable PR.
- **Goal** (durable): hand it a standing objective (`create_goal`); it persists, and across a heartbeat it plans the single next action, dispatches it into the task layer, records what actually shipped, and **evaluates direction** — only closing the goal when a grounded review confirms `done_when` is met. You steer it any time (`steer_goal`) and ask what's going on (`get_goal` / `evaluate_goal`). *(This is the folded-in goalclaw — one service to talk to for software.)*

Cognition is always `claude` over a Pro/Max OAuth session — **no `ANTHROPIC_API_KEY`, no metered billing** for autonomous runs.

It is **not** a chatbot and **not** a rebuild of OpenHands. OpenHands owns the hard part (the agent loop, tool use, code edits, git). DevClaw owns everything *around* it: the interface, durable goals + direction evaluation, goal/task decomposition, state, isolation, and observability.

```
MCP client (OpenClaw / Claude Code / any MCP host)
  │   create_goal · steer_goal · get_goal       (durable goal layer)
  │   implement_feature / fix_bug / start_program …  (one-shot task layer)
  ▼
DevClaw  (Python)
  ├── goal layer     durable goals → heartbeat tick → next-action plan +
  │                  direction evaluation → dispatch (in-process) → done-gate review
  ├── MCP server     FastMCP — stdio + streamable-HTTP, dashboard + SSE
  ├── planner        Goal → task DAG (single-task passthrough for small goals)
  ├── state store    SQLite — programs, tasks, append-only events
  └── sandcastle     `docker run --rm` per task — RO ~/.claude mount, destroyed on exit
        │
        ▼
  OpenHands (Python SDK)  ── agent loop, runs `claude` via ACP (Pro OAuth)
```

## The split

| Concern | Owner |
|---|---|
| Agent loop, sandbox coding, git | **OpenHands** |
| Goal → tasks decomposition | DevClaw planner |
| Task/program state | DevClaw state store (SQLite) |
| Per-task isolation | DevClaw sandcastle runner (`docker run`) |
| Progress + notification | DevClaw event stream + `notify_url` callbacks |
| Interface to clients | DevClaw MCP server |

The full rationale — including why OpenHands and sandbox isolation are **orthogonal** layers (the agent vs. the box it runs in), and why this calls `docker run` directly instead of depending on `@ai-hero/sandcastle` — lives in [`docs/architecture-v2.md`](./docs/architecture-v2.md).

## Layout

```
devclaw/
├── server.py            # FastMCP server — task + goal tools, dashboard + SSE, bearer-auth middleware
├── planner.py           # Goal → task DAG (shells out to `claude --print`)
├── state_store.py       # SQLite: programs, tasks, append-only events
├── task_queue.py        # async task lifecycle (asyncio) + concurrency + on-settle hook
├── sandcastle_runner.py # `docker run --rm` per task; streams events from the runner
│   # --- the goal layer (folded-in goalclaw) ---
├── goal_service.py      # wires the layer: heartbeat loop, in-proc wake, the create/get/steer/eval surface
├── goal_tick.py         # one heartbeat: cheap check → plan → evaluate → dispatch → done-gate
├── goal_planner.py      # next-action cognition (goal+state → one action), JSON-validated
├── goal_evaluator.py    # direction evaluation (is this achieving the objective?), grounded in deliveries
├── goal_engine.py       # in-process dispatch into the task queue (replaces goalclaw's HTTP MCP client)
├── goal_store.py        # durable mind on disk: goal.yaml · STATUS.md · log.md · inbox.md · deliveries.md
├── goal_models.py       # Goal / GoalStatus / Action / PlanResult / EvalResult
└── workspace.py         # per-action pristine git checkout (devclaw owns the checkout)
openhands-runner/
├── runner.py            # OpenHands SDK invocation; emits event/result lines on stdout
└── requirements.txt     # openhands-sdk
.sandcastle/Dockerfile   # per-task sandbox image
tests/                   # pytest — planner, state store, queue/DAG (stubbed; no docker)
docs/architecture-v2.md  # the architectural contract — read before touching the runner/store/sandbox
```

DevClaw is all Python. The only language boundary left is the process boundary: `openhands-runner/runner.py` runs the (Python-only) OpenHands SDK *inside* the sandbox container, isolated from the long-running host process — it talks to the host over a line-delimited JSON protocol on stdout.

## MCP tools

| Tool | Does |
|---|---|
| `implement_feature(workspace_dir, goal, …)` | Run a single feature task |
| `fix_bug(workspace_dir, description, …)` | Run a single bug-fix task |
| `review_repository(workspace_dir, …)` | Read-only review (no writes — invariant runtime-checked) |
| `onboard(workspace_dir, …)` | Analyze a repo and write a draft `AGENTS.md` (comprehension only) for you to review |
| `start_program(workspace_dir, goal, …)` | Decompose a large goal into a task DAG and run it |
| `get_program(program_id)` / `list_programs()` | Program status + the task DAG |
| `get_status(task_id)` | One task's status / result / PR |
| `list_tasks(...)` | Task history, filterable |
| `get_events(...)` | Replayable event feed (also a live SSE stream over HTTP) |
| `cancel_task(task_id)` / `cancel_program(program_id)` | Abort in-flight work — tears down the sandbox, marks it `cancelled` (terminal; not retried or recovered) |

Async by default: a tool call returns a `task_id` immediately and the work runs in the background. Pass a `notify_url` to get a callback on completion/block instead of polling.

### Durable goals (the goal layer)

A `program` runs once to completion; a **goal** is a standing intent DevClaw advances across many heartbeats — and judges for *direction*, not just shipped PRs.

| Tool | Does |
|---|---|
| `create_goal(goal_id, objective, workspace_dir, done_when, backlog, …)` | Register a durable goal DevClaw drives over time |
| `get_goal(goal_id)` | Objective, phase, what's in flight, the latest direction verdict, recent log |
| `list_goals()` | All goals + phase + direction |
| `steer_goal(goal_id, message)` | Correct/redirect — recorded as steering, honored on the next tick (poked immediately); unblocks a blocked goal |
| `evaluate_goal(goal_id)` | Force a direction evaluation now — "is this going the right way?" — judged against `done_when`, grounded in what shipped |

**How a goal is driven (per heartbeat):**
1. **Cheap check** (0 tokens) — poll the in-flight action via a local SQLite read.
2. **Per-delivery evidence** (0 tokens) — on a finished action, read the *full* task result (agent output + gate verdict) and append a grounded note to `deliveries.md`.
3. **Next-action plan** (1 LLM call, only past the gate) — pick the single next action from the backlog/steering and dispatch it in-process.
4. **Direction evaluation** (periodic LLM call) — every `DEVCLAW_GOAL_EVAL_EVERY` deliveries, judge whether the *delivered work* is achieving the objective; corrections are fed back as steering, a hard verdict blocks.
5. **Done-gate** — the planner's `done` is only a *proposal*; it triggers a read-only `review_repository` against `done_when`, and the goal closes **only if the evaluator confirms `achieved`** from that review. "Done" is gated on grounded evaluation, not on counting PRs.

The zero-token idle guard is load-bearing: an idle goal and an in-flight-still-running goal cost **0 `claude` calls** (the heartbeat is mechanism; cognition runs only when there's real work).

### Build a project from scratch

For a whole project (not one task), DevClaw **grills you to a shared spec first**, then builds it:

| Tool | Does |
|---|---|
| `build_project(idea, workspace_dir)` | Start a project; returns a `project_id` + the first question |
| `answer_question(project_id, answer)` | Answer the current question → the next one, or `status: ready` + the spec |
| `get_project(project_id)` | Full state — idea, transcript, spec, the running program |
| `approve_spec(project_id)` | Approve the spec → plan it into a milestone DAG → start the build (returns `program_id`) |
| `steer(project_id, message)` | Redirect a *running* build — folds direction into not-yet-started tasks (running/done untouched) |

The grill (one question at a time, each with a recommended answer) adapts [Matt Pocock's MIT `grill-me`](https://github.com/mattpocock/skills); the build runs as a program you can watch with `get_program` / the dashboard. The agreed spec + interview transcript are written to `$DEVCLAW_STATE/projects/<id>/` for the human record.

## Auth (the design constraint)

DevClaw inherits a `claude` OAuth session — it never uses an API key. `ANTHROPIC_API_KEY` is **actively refused** at both the host (planner) and sandbox layers so a stray key can't silently switch autonomous runs onto metered billing. All you need is a logged-in `claude` CLI: the planner shells out to it, and the per-task sandbox bind-mounts `~/.claude` **read-only** (auth works; nothing else from the host is mounted, and the container is destroyed on exit so no state escapes).

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                       # the host orchestrator (FastMCP + httpx)
pip install -r openhands-runner/requirements.txt   # only needed inside the sandbox image
npm install -g @agentclientprotocol/claude-agent-acp   # ACP adapter (one-time, global)

DEVCLAW_TRANSPORT=stdio devclaw-mcp        # local dev (MCP over stdio)
# or HTTP for a long-running service:
DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000 devclaw-mcp
#   → MCP at /mcp, live dashboard at /dashboard, SSE at /programs/:id/events
```

(`devclaw-mcp` is the console script; `python -m devclaw.server` works too.)

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
| `DEVCLAW_TOKEN` | — | When set, the HTTP transport requires it on every route except `/health` — via `Authorization: Bearer <token>` or a `?token=` query param. Unset = no auth (local dev). |
| `DEVCLAW_DB` | `./devclaw.db` | SQLite path for state |
| `DEVCLAW_STATE` | `./.devclaw-state` | dir for build-from-scratch project files (idea/transcript/spec) |
| `DEVCLAW_MAX_GRILL_QUESTIONS` | `20` | cap on grill questions before the spec is force-finalized |
| `DEVCLAW_MAX_CONCURRENT` | `4` | global cap on concurrently-running tasks (backpressure) |
| `DEVCLAW_MAX_CONCURRENT_PER_PROGRAM` | `2` | per-program concurrency cap |
| `DEVCLAW_TICK_SECONDS` | `10` | task-queue heartbeat interval — advances DAGs and resumes recovered work from DB state |
| `DEVCLAW_GOALS_DIR` | `~/memory/goals` | root holding one folder per durable goal (`<id>/goal.yaml` …) |
| `DEVCLAW_GOAL_TICK_SECONDS` | `900` | goal heartbeat interval — also woken in-process the moment a task settles |
| `DEVCLAW_GOAL_EVAL_EVERY` | `3` | deliveries between periodic direction evaluations (`0` → evaluate only at the done-gate) |
| `DEVCLAW_GOAL_NO_PROGRESS_S` | `21600` | wall-clock seconds an executing goal may go without a delivery before the no-progress watchdog pings the owner once (zero tokens; `0` disables). Complements the per-task timeout. |
| `DEVCLAW_GOAL_VERIFY_DONE` | `1` | when set, a planner `done` proposal dispatches a read-only review of the repo vs `done_when` and the evaluator must confirm `achieved` before the goal closes (`0` → trust an artifact-only done eval) |
| `DEVCLAW_GOAL_NOTIFY_URL` | — | notify-relay endpoint for goal-level Telegram messages (free-text `/text` passthrough) |
| `DEVCLAW_TASK_TIMEOUT_S` | `1800` | per-task wall-clock cap — a run exceeding it is cancelled (its sandbox torn down) and the task marked `failed`, so a hung agent fails cleanly instead of burning quota. `<=0` disables. |
| `DEVCLAW_MAX_RETRIES` | `1` | re-runs of a task that fails its verify gate (or errors), each with the failure fed back into the goal, before escalating. `0` disables. Timeouts are never retried. |
| `GITHUB_TOKEN` / `GH_TOKEN` | — | repo push + PR access for `open_pr` delivery (or use a logged-in `gh`). Separate from the Claude OAuth pillar — this is git access, not cognition billing. |
| `DEVCLAW_VERIFY_TIMEOUT_S` | `900` | wall-clock cap for the verify-gate command (the `verify_cmd` run after the agent finishes); on expiry the gate fails the task. |
| `DEVCLAW_SANDBOX_IMAGE` | `devclaw-sandbox:latest` | per-task sandbox image (see `.sandcastle/Dockerfile`) |
| `DEVCLAW_CLAUDE_BIN` | `claude` | the `claude` binary the planner drives |
| `DEVCLAW_HOST_CLAUDE_DIR` | `~/.claude` | host path bind-mounted read-only into each sandbox |
| `DEVCLAW_SANDBOX_CLAUDE_ALLOWLIST` | `.credentials.json,.claude.json` | comma-separated entries **under** `~/.claude` bound (read-only) into each sandbox. Default = the OAuth **identity pair**: the credential token plus `.claude.json` (account identity — the ACP agentic loop hangs without it). The rest of the host `~/.claude` (skills, plugins + their MCP servers, the global `CLAUDE.md`, `projects/` history) is deliberately **not** projected into the agent. Add entries only with intent — they must exist on the host. |

### Model tiering

Cognition is tiered per role so an autonomous run doesn't burn the Pro/Max quota on Opus where a lighter model does the job (no API key = the limit is your session quota, not a bill). Host roles take a `claude --model` value (alias like `sonnet`/`opus`); the exec engine takes a full model id. Set any to empty to fall back to the account default.

| Var | Default | Role |
|---|---|---|
| `DEVCLAW_PLANNER_MODEL` | `opus` | planner (`plan_goal`/`plan_spec`) — rare, high-leverage decomposition |
| `DEVCLAW_GRILL_MODEL` | `sonnet` | the build-from-scratch elicitation grill |
| `DEVCLAW_JUDGE_MODEL` | `haiku` | the eval failure-analysis judge |
| `DEVCLAW_EXEC_MODEL` | `claude-sonnet-4-6` | **the OpenHands coding agent — the token/quota bulk.** Set `claude-opus-4-8` to opt a run up to Opus. |
| `DEVCLAW_GOAL_PLANNER_MODEL` | `sonnet` | the goal layer's next-action planner (light, bounded JSON) |
| `DEVCLAW_GOAL_EVAL_MODEL` | `sonnet` | the direction evaluator (judging delivered work vs intent — bump to `opus` per goal for hard direction calls) |

## Tests

```bash
pip install -e ".[dev]"
pytest          # planner + state store + queue/DAG + grill, all stubbed — no docker, no claude
```

To validate the **real** pipeline (a logged-in `claude` driving OpenHands in a docker sandbox), follow the layered runbook in [`docs/live-shakedown.md`](./docs/live-shakedown.md).

## Status

DevClaw is the live runtime. It was rewritten from TypeScript to all-Python (FastMCP) — the host orchestration now matches the language of the OpenHands SDK it drives, so there's a single toolchain. The original v1 (a LangGraph orchestrator + markdown skills driven by cron) was retired earlier and lives only in git history.

## What this is NOT

- **Not a chatbot.** It's a backend service other agents call.
- **Not a general assistant.** It executes software-development goals, nothing else.
- **Not a rebuild of OpenHands.** OpenHands is the execution engine; DevClaw is the orchestration above it.

## License

[MIT](./LICENSE). Copyright 2026 Denys Sychov.
