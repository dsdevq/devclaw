# DevClaw Architecture v2

**Status:** Live. This is the current architecture; it superseded and replaced the v1 skill-based + cron-driven approach, which has been removed (see git history for the prior art).
**Decision:** Adopt OpenHands as the execution engine. DevClaw is a thin orchestration layer.

---

## Why the current approach feels wrong

The current DevClaw is a collection of OpenClaw workspace skills glued together with cron ticks. Every dispatch tick burns LLM tokens to do work Python would do in milliseconds. The execution logic is too custom, too fragile, and too coupled to OpenClaw internals.

The core problem: **DevClaw was trying to be both an orchestrator and an execution engine.** These are different concerns and need different tools.

---

## The split

| Concern | Owner |
|---|---|
| Execution engine (agent loop, sandbox, coding, git) | **OpenHands** |
| Goal decomposition (Goal → Tasks) | **DevClaw planner** |
| Task state (what's running, done, blocked) | **DevClaw state store** |
| Progress polling + user notification | **DevClaw poller** |
| Interface to OpenClaw | **DevClaw MCP server** |

DevClaw stops being an execution engine. OpenHands owns the hard part.

---

## Architecture

```
You (Telegram / Claude Code)
  │
  ▼
OpenClaw
  └── MCP call → DevClaw MCP server
                    │
             DevClaw Runtime
             ├── planner       Goal → Milestones → Tasks
             ├── state store   SQLite — task status, history, results
             ├── poller        polls OpenHands, fires notifications
             └── REST client   submits tasks to OpenHands API
                    │
             OpenHands (Docker, self-hosted on VPS)
             ├── agent loop    autonomous reasoning + action
             ├── sandbox       isolated Docker container per task
             ├── tools         git, gh, bash, file edit, browser
             └── LLM backend   Claude Max via ACP (no API key needed)
```

---

## OpenHands

OpenHands (formerly OpenDevin) is an autonomous coding agent runtime. It takes a task description and executes it autonomously in an isolated sandbox — cloning repos, writing code, running tests, opening PRs.

**Docs:** https://docs.openhands.dev/overview/introduction
**Repo:** https://github.com/All-Hands-AI/OpenHands

### Why OpenHands fits

- **REST API** — programmatic task submission, status polling, result retrieval
- **Docker sandbox** — isolated execution per task, no contamination between runs
- **Async execution** — submit a task, poll for completion, no blocking
- **Pause & resume** — durable state across container restarts
- **ACP integration** — can delegate to Claude Code as the LLM backend, which means Claude Max OAuth, no API keys needed. See: https://docs.openhands.dev/usage/acp
- **Headless mode** — no UI required, runs as a pure backend service
- **Sub-agent delegation** — internally handles multi-step, multi-agent work

### LLM backend: Claude Max via ACP

OpenHands supports ACP (Agent Communication Protocol), which lets it delegate LLM calls to Claude Code instead of calling the Anthropic API directly. This means:

- Uses the existing Claude Max subscription — zero marginal inference cost for autonomous overnight runs
- No `ANTHROPIC_API_KEY` needed
- Same auth model as the rest of the stack

### What OpenHands owns

- The agent loop (reasoning → action → observe → repeat)
- Sandbox isolation (Docker container per task)
- All coding operations: `git clone`, `git branch`, code edits, `pytest`, `gh pr create`
- Multi-step execution, replanning, retrying within a task

### What OpenHands does NOT own

- Goal decomposition — DevClaw planner breaks the goal into tasks before handing off
- Task state outside its own run — DevClaw tracks status, history, results
- User notifications — DevClaw polls and notifies via OpenClaw → Telegram
- Scheduling — DevClaw owns when tasks run

---

## DevClaw internals

### 1. MCP server — the interface

Exposes capabilities to OpenClaw (and any other MCP client):

```
create_project(name, description)         → project_id
implement_feature(project_id, goal)       → task_id
fix_bug(project_id, description)          → task_id
review_repository(repo)                   → task_id
run_tests(project_id)                     → task_id
get_status(task_id)                       → status, progress, result
list_tasks(project_id?)                   → [task]
```

OpenClaw calls these. The implementation is hidden. OpenClaw never knows OpenHands exists.

**Source:** https://docs.openhands.dev/usage/rest-api (OpenHands API reference)

### 2. Planner — Goal → Tasks

For goals that are too large for a single OpenHands run (e.g. "build a YouTube clone"), the planner decomposes them:

```
Goal: "Build a YouTube clone"
  └── Program: youtube-clone
        ├── Task 1: scaffold project + CI (→ OpenHands)
        ├── Task 2: implement auth (→ OpenHands)
        ├── Task 3: video upload + processing (→ OpenHands)
        ├── Task 4: frontend player (→ OpenHands)
        └── Task 5: deploy pipeline (→ OpenHands)
```

Each task is a single OpenHands run with explicit acceptance criteria. The planner generates the DAG; the poller walks it as tasks complete.

For small, bounded goals ("fix typo in README"), the planner passes directly to OpenHands as a single task — no decomposition needed.

### 3. State store — SQLite

Tracks everything DevClaw owns:

```
projects      id, name, description, status, created_at
tasks         id, project_id, goal, kind, status, openhands_run_id,
              result, pr_url, created_at, completed_at
events        task_id, event, payload, ts   (append-only audit log)
```

Single-writer. All state transitions go through one path.

### 4. Poller — completion detection + notification

DevClaw polls OpenHands for task status (OpenHands self-hosted has no push callbacks). On completion:

1. Writes result to state store
2. Advances the DAG (unblocks dependent tasks if any)
3. Notifies OpenClaw → Telegram via the callback URL passed at task creation

The callback is passed by OpenClaw when it calls `implement_feature()`:

```python
implement_feature(
    project_id="youtube-clone",
    goal="implement auth",
    notify_url="https://openclaw.internal/notify/abc123"
)
```

DevClaw calls `notify_url` when done or blocked. OpenClaw forwards to Telegram.

---

## Deployment

OpenHands runs as a sibling container in the Docker Compose stack:

```yaml
services:
  openhands:
    image: ghcr.io/all-hands-ai/openhands:latest
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # sandbox spawning
      - openhands-data:/root/.openhands
    environment:
      - LLM_BACKEND=acp            # use Claude Code, not API key
    expose:
      - "3000"                     # internal only, no public port

  devclaw:
    build: ./devclaw
    environment:
      - OPENHANDS_URL=http://openhands:3000
    volumes:
      - devclaw-data:/data
    expose:
      - "8000"                     # MCP server, internal only
```

Both are loopback-only. OpenClaw reaches DevClaw via `http://devclaw:8000/mcp/`. DevClaw reaches OpenHands via `http://openhands:3000`.

---

## Autonomous loop

The key behavior: you kick off a goal and DevClaw keeps working until done.

```
implement_feature("build YouTube clone")
  │
  ▼
DevClaw planner decomposes → 5 tasks, DAG stored
  │
  ▼
Task 1 submitted to OpenHands → running
  │
  ▼  (OpenHands executes autonomously — no babysitting)
  │
  ▼
Task 1 done → PR opened → DevClaw notifies you
  │
  ▼
Task 2 unblocked → submitted to OpenHands → running
  │
  ▼
  ... continues until all tasks done or one blocks ...
  │
  ▼
Goal complete → "5/5 tasks done. PRs: [list]"

or

Task 3 blocked → DevClaw notifies you → waits for decision → resumes
```

You don't babysit. DevClaw reports. You decide only when there's a real blocker.

---

## The goal layer (durable goals + direction evaluation)

**Folded in from the former standalone `goalclaw` service (2026-06-06).** DevClaw is
now the software-development *project manager*: it owns durable goals, kicks off the
work, and evaluates project direction. OpenHands stays the executor.

Two altitudes, one service:

- **`program`** — a bounded, one-shot DAG decomposed from a goal that runs to
  completion in a single async session. Unchanged.
- **`goal`** — an open-ended *standing intent* advanced across many heartbeats,
  steerable and **direction-evaluated**, that persists until its `done_when` is
  genuinely satisfied. Lives on disk under `DEVCLAW_GOALS_DIR`
  (`<id>/goal.yaml · STATUS.md · log.md · inbox.md · deliveries.md`), git-synced
  like the rest of the vault — ephemeral body, durable mind.

These are different time-scales/state-lifecycles; the goal layer sits *above* the
task/program engine and dispatches into it **in-process** (`goal.engine.py`). When
goalclaw was a separate service it dispatched over HTTP MCP and re-derived "delivered"
from a camelCase blob + a separate `/wake` callback — that whole transport, its
bearer token, and the "polled `done` before `pr_url` was written" race are gone by
construction now that it's one process: dispatch is a function call, poll is a SQLite
read, and a task settling fires an in-process hook (`TaskQueue.set_on_settle`) that
wakes the goal heartbeat immediately.

### How a goal is driven (the heartbeat), and the quota guard

`goal.tick.tick_goal` runs the same mechanism/cognition split as everything else,
ordered so **idle ticks cost ~0 `claude` calls** (the load-bearing quota guard —
burned this way 2026-05-18):

1. **Progress check** (Python, every tick, 0 tokens) — poll the in-flight ref via a
   local SQLite read. Running → return. Idle + cadence-not-due → return.
2. **Per-delivery evidence** (in-proc, 0 tokens) — on a finished action, read the
   *full* task `result_json` (the agent's own output + the verify-gate output) and
   append a grounded record to `deliveries.md`. This is richer than the wire ever
   exposed, and it's the substrate the evaluator reads.
3. **Next-action plan** (`goal.planner`, 1 LLM call, only past the gate) — choose the
   single next action from backlog/steering and dispatch it. JSON-validated.
4. **Direction evaluation** (`goal.evaluator`, periodic LLM call) — every
   `DEVCLAW_GOAL_EVAL_EVERY` deliveries (or on direction-steering), judge whether the
   *delivered work* is achieving the objective, grounded in `deliveries.md` — not by
   counting backlog items. `off_track` → corrections written to `inbox.md` as
   steering (the evaluator steers the goal the way Denys would); `stalled`/
   `needs_human` → block + notify; `on_track` → record and continue.

### The done-gate (why "done" is trustworthy now)

The old "done = shipped-PRs ≥ backlog" check was shallow: a PR can be gate-green but
wrong, the backlog can drift from the real intent, and *done ≠ good*. The planner's
`done` is therefore only a **proposal**. It dispatches a read-only
`review_repository(focus=done_when)` and enters a `verifying` phase; when that review
returns, the evaluator judges the *actual repo* against `done_when` and the goal
closes **only on `achieved`**. Otherwise the corrections are steered back in and the
goal keeps going. Done is gated on grounded evaluation, not on counting. (Disable the
review run with `DEVCLAW_GOAL_VERIFY_DONE=0` for an artifact-only done eval.)

### Steer / observe surface

MCP tools `create_goal` / `get_goal` / `list_goals` / `steer_goal` / `evaluate_goal`
let an operator register a goal, ask what's going on / what direction, correct it, or
force a direction evaluation on demand — so there is always one thing to talk to for
a piece of software: *"DevClaw, take care of this project."*

## What this is NOT

- **Not a chatbot.** OpenClaw is the chatbot. DevClaw is what runs behind it.
- **Not a general-purpose AI assistant.** It executes software development goals — nothing else.
- **Not another OpenClaw.** No channels, no skills, no cron. DevClaw is a backend service.
- **Not a rebuild of OpenHands.** OpenHands owns the execution engine. DevClaw owns the orchestration above it.

---

## Migration from v1 (complete)

The cutover from the v1 skill-based + cron-driven approach to this design is done. The original path — deploy OpenHands, wire the ACP backend, build the MCP server / planner / poller, cut `implement_feature` over from the skills, then retire the skills — has run to completion. The v1 orchestrator and skills have been removed from the repo; they remain in git history as prior art.

---

## Further reading

- [OpenHands introduction](https://docs.openhands.dev/overview/introduction)
- [OpenHands ACP integration](https://docs.openhands.dev/usage/acp)
- [OpenHands REST API](https://docs.openhands.dev/usage/rest-api)
- [OpenHands SDK](https://docs.openhands.dev/usage/sdk)
- [OpenHands Docker deployment](https://docs.openhands.dev/usage/docker)
