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
| Settle detection + user notification | **DevClaw TaskQueue** (in-process on-settle hook, not a poller) |
| Interface to OpenClaw | **DevClaw MCP server** |

DevClaw stops being an execution engine. OpenHands owns the hard part.

---

## Architecture

```
You (Telegram / voice / chat)
  │
  ▼
OpenClaw waiter agent
  └── MCP call → DevClaw MCP server (devclaw-mcp — long-lived Python process)
                    │
             DevClaw Runtime
             ├── GoalService + heartbeat   durable goals → plan → dispatch → evaluate
             ├── planner / evaluator        one-shot `claude --print` cognition
             ├── state store   SQLite — programs, tasks, append-only events
             ├── TaskQueue     in-process dispatch; on-settle hook wakes the heartbeat
             └── engine/sandcastle.py       `docker run --rm` — one ephemeral container per task
                    │  (devclaw spawns it itself; there is no OpenHands service to poll)
                    ▼
             ephemeral sandbox container (per task, self-destructs on exit)
             ├── ENTRYPOINT     openhands-runner/runner.py
             ├── OpenHands SDK  thin ACP turn-loop (Conversation + ACPAgent)
             ├── claude-agent-acp → claude-code CLI   the actual agent loop
             └── LLM            Claude via ACP over Pro/Max OAuth (no API key)
```

**How the engine actually works (not a service, not a wire).** DevClaw does **not**
run OpenHands as a long-lived REST server and does **not** poll it. The engine
(`devclaw/engine/sandcastle.py`) issues a `docker run --rm` per task; the container's
ENTRYPOINT runs `openhands-runner/runner.py`, which embeds the OpenHands SDK as a thin
ACP turn-loop around `claude`. Events stream back as line-delimited JSON on the
container's **stdout** (`event:` lines + one terminating `result:` line) — a process
boundary, not a network one. A task "settling" is an in-process SQLite write plus an
on-settle hook (`TaskQueue.set_on_settle`) that wakes the goal heartbeat immediately.
There is no poller, no `OPENHANDS_URL`, no callback URL between devclaw and its engine.

---

## OpenHands

OpenHands (formerly OpenDevin) is an autonomous coding agent runtime. It takes a task description and executes it autonomously in an isolated sandbox — cloning repos, writing code, running tests, opening PRs.

**Docs:** https://docs.openhands.dev/overview/introduction
**Repo:** https://github.com/All-Hands-AI/OpenHands

### Why OpenHands fits

DevClaw embeds the **OpenHands Python SDK** *inside the per-task sandbox*
(`openhands-runner/runner.py`) — not as an external service it talks to over HTTP.
What the SDK buys us:

- **A ready agent loop** — `Conversation` + `ACPAgent` drive the reason → act →
  observe cycle so devclaw doesn't hand-roll one.
- **ACP delegation to `claude`** — the agent's cognition is `claude-agent-acp` →
  `claude-code`, i.e. Claude over Pro/Max OAuth, **no `ANTHROPIC_API_KEY`**. Same
  auth model as the rest of the stack. See: https://docs.openhands.dev/usage/acp
- **A clean event stream** — the runner emits line-delimited JSON on stdout
  (`event:` lines + one terminating `result:` line), which `sandcastle.py` parses.
  That is the entire devclaw↔runner protocol.

### Why OpenHands and sandbox isolation are orthogonal

The agent (what reasons and edits code) and the box it runs in (the isolation
boundary) are **different layers**. OpenHands is the agent loop; the sandbox is the
container. DevClaw owns the box and *hosts* the agent inside it, rather than letting
the agent framework own isolation. That is why devclaw calls `docker run --rm` itself
(in `engine/sandcastle.py`) instead of depending on a sandbox-provider library
(`@ai-hero/sandcastle`) or on OpenHands' own remote runtime: the container lifecycle,
the mounts, the read-only `~/.claude` allowlist, and teardown are devclaw's contract
to keep. The day the agent inside is swapped (codex, gemini-cli, an open-source loop),
the box is unchanged.

### LLM backend: Claude via ACP

OpenHands supports ACP (Agent Communication Protocol), which lets it delegate LLM calls to `claude-code` instead of calling the Anthropic API directly. This means:

- Uses the existing Claude Pro/Max subscription — zero marginal inference cost for autonomous overnight runs
- No `ANTHROPIC_API_KEY` needed (and it is actively stripped — see the auth section of the README)
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

The layer contracts live in [`architecture-layers.md`](./architecture-layers.md); this
is the decision-record summary of what devclaw owns around the OpenHands worker.

### 1. MCP server — the interface

Exposes tools to the OpenClaw waiter (and any other MCP client). Async by default:
a task call returns a `task_id` immediately; pass a `notify_url` to get a callback
instead of polling. The real surface (see `devclaw/server/tools.py`):

```
dispatch_task(kind, workspace_dir, goal, …)   → task_id   (kind ∈ implement_feature / fix_bug / review_repository)
create_goal(goal_id, objective, workspace_dir, done_when, …)   register a durable goal
get_goal / list_goals / steer_goal / answer_unknowns / cancel_goal   drive a goal
start_program(workspace_dir, goal, …)         → program_id   (decompose into a task DAG)
get_status(task_id) / list_tasks(…) / get_events(…)           task history + live SSE
register_project / list_projects / link_goal / …             the control-plane registry
deploy_project / deploy_status / stop_deploy                  durable Tailscale hosting
```

The waiter calls these; OpenHands is invisible to it.

### 2. Planner — Goal → Tasks

For goals too large for a single sandbox run (e.g. "build a YouTube clone"), the
planner decomposes them into a task DAG:

```
Goal: "Build a YouTube clone"
  └── Program: youtube-clone
        ├── Task 1: scaffold project + CI  (→ sandbox)
        ├── Task 2: implement auth         (→ sandbox)
        ├── Task 3: video upload + processing (→ sandbox)
        ├── Task 4: frontend player        (→ sandbox)
        └── Task 5: deploy pipeline        (→ sandbox)
```

Each task is a single sandbox dispatch with explicit acceptance criteria. The
`TaskQueue` walks the DAG as tasks settle. For small, bounded goals the planner
dispatches a single task — no decomposition needed.

### 3. State store — SQLite

Tracks everything DevClaw owns (`devclaw/state_store/`), one `devclaw.db`:

```
programs        id, goal, status, created_at, …
tasks           id, program_id, parent_goal_id, kind, goal, status,
                result_json, pr_url, created_at, settled_at, …
events          task_id, event, payload, ts   (append-only audit log)
goal_status     goal_id, phase, lifecycle, in_flight_*, state, version, …  (Tranche 1)
goal_steering · goal_log · goal_deliveries · goal_docs · goal_phase_history  (Tranche 1)
```

**Single-writer per table family:** only the `TaskQueue` mutates task rows; `events` is
append-only and the status views are projections. Goal state (Tranche 1, see
[`architecture-layers.md`](./architecture-layers.md)) is owned by `GoalStore`, wired
onto this SAME `StateStore` — `goal_status`'s phase/lifecycle/in_flight changes go
through a CAS'd `GoalStore.transition()`, not a single-caller assumption, because
`steer_goal`/`cancel_goal` can write concurrently with the heartbeat. There is **no**
`openhands_run_id` — devclaw doesn't track a remote run, because there is no remote
run; the sandbox is ephemeral and its whole output is the stdout stream.

### 4. Settle — completion detection + notification (in-process, no poller)

When a sandbox container exits, `sandcastle.py` has already consumed its stdout and
holds the terminal `result:` line. Settling is entirely in-process:

1. The `EngineResult` is written to the task row (single writer).
2. The DAG advances (dependent tasks unblock).
3. The on-settle hook (`TaskQueue.set_on_settle`) wakes the goal heartbeat **in the
   same process** — the goal reads the full `result_json` (agent output + gate verdict)
   via a SQLite read and appends grounded evidence to `deliveries.md`.
4. If a `notify_url` was supplied, devclaw POSTs it (owner-facing blockers / direction
   questions / completions) → notify-relay → Telegram.

There is no polling loop against an OpenHands service — the "poll" is a local SQLite
read, and the wake is a function call.

---

## Deployment

There is **no** OpenHands sibling container. The only long-lived service is
`devclaw-mcp`; it mounts the docker socket and spawns per-task sandbox containers
itself. OpenHands ships *inside* the sandbox image (`.sandcastle/Dockerfile`), not as
a service:

```yaml
services:
  devclaw-mcp:
    build: .
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock   # to spawn per-task sandboxes
      - devclaw-data:/data
      - ~/.claude:/root/.claude:ro                  # OAuth identity, read-only
    environment:
      - DEVCLAW_TRANSPORT=http
      - DEVCLAW_SANDBOX_IMAGE=devclaw-sandbox:latest
    expose:
      - "8000"                     # MCP server + dashboards
```

The OpenClaw waiter reaches DevClaw at `http://devclaw-mcp:8000/mcp`. Each task is a
transient `docker run --rm devclaw-sandbox:latest '<payload>'` spawned by
`sandcastle.py` — it appears in `docker ps` only while the task runs, then vaporizes.
Access to the docker socket (and its GID) is what lets devclaw-mcp spawn siblings; see
[`task-execution-flow.md`](./task-execution-flow.md) for the node-by-node detail.

---

## Autonomous loop

The key behavior: you kick off a goal and DevClaw keeps working until done.

```
start_program("build YouTube clone")
  │
  ▼
DevClaw planner decomposes → 5 tasks, DAG stored
  │
  ▼
Task 1 dispatched to a fresh sandbox → running
  │
  ▼  (the sandbox agent executes autonomously — no babysitting)
  │
  ▼
Task 1 settles → gate + review pass → PR opened → DevClaw notifies you
  │
  ▼
Task 2 unblocked (on-settle hook) → dispatched to a sandbox → running
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
  genuinely satisfied. Lives under `DEVCLAW_GOALS_DIR` (`<id>/…`), git-synced
  like the rest of the vault — ephemeral body, durable mind. `goal.yaml` (the
  owner-authored facts), `spec.md`, and `discovery.md` are plain files; the
  machine state (`STATUS.md`), steering (`inbox.md`), event log (`log.md`),
  grounded evidence (`deliveries.md`), and the decomposer/firming contracts
  (`checklist.yaml`, `firmed-draft.yaml`) are SQLite tables (`goal_status`,
  `goal_steering`, `goal_log`, `goal_deliveries`, `goal_docs` — Tranche 1,
  living in the same `devclaw.db` the task queue uses) with those files
  regenerated as read-only, rollback-legible **views** on every write.

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

The cutover from the v1 skill-based + cron-driven approach to this design is done. The original path — embed the OpenHands SDK in the sandbox, wire the ACP backend to `claude`, build the MCP server / planner / task queue, cut `implement_feature` over from the skills, then retire the skills — has run to completion. (An early sketch imagined a standalone OpenHands *service* devclaw would poll; it was never built — the in-process `docker run` + on-settle model above is what shipped.) The v1 orchestrator and skills have been removed from the repo; they remain in git history as prior art.

---

## Further reading

- [OpenHands introduction](https://docs.openhands.dev/overview/introduction)
- [OpenHands ACP integration](https://docs.openhands.dev/usage/acp) — the path devclaw uses (agent delegated to `claude`)
- [OpenHands SDK](https://docs.openhands.dev/usage/sdk) — embedded in `openhands-runner/runner.py`

> DevClaw does **not** use OpenHands' REST API or its own docker/remote-runtime
> deployment — the SDK runs in-process inside a devclaw-spawned sandbox. Those docs
> describe a service topology this project deliberately does not run.
