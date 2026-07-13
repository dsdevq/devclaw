# How devclaw really works

> Status: CURRENT (2026-07-12). The one-sitting mental model — read this when you've
> lost the thread. It's the *map*; [`architecture-layers.md`](./architecture-layers.md)
> is the *locked contract* and the code is the *territory*. When they disagree, trust
> the code, then fix this doc.

## The one paragraph

devclaw is a **software-development agentic loop**. You hand it a durable *goal* with
verifiable completion criteria; a self-executing heartbeat carries it — **plan →
sandboxed execution → verify gate → evaluate → iterate** — with hard brakes (retry
caps, a no-progress watchdog, `stalled`/`needs_human` verdicts) so it never optimises
into the void. It sits **behind MCP** and is driven by an **OpenClaw "waiter" agent**
that turns chat into tool calls; **devclaw never talks to the user directly**.
Cognition is always `claude` over a Pro/Max **OAuth** session — **no API key, no
metered billing, ever**.

## The five layers (and the two chains)

The system is five layers below the user. Only layer 5 is an agent harness in the
technical sense — the rest is orchestration.

| # | Layer | Lives in | Owns |
|---|---|---|---|
| 1 | **MCP surface** | `devclaw/server/` | tools, auth, dashboard, transport — pure protocol |
| 2 | **GoalService + heartbeat** | `devclaw/goal/` | the goal state machine + the ~15-min tick |
| 3 | **Cognition callers** | `goal/{planner,evaluator,decomposer}.py`, `goal/phases/firming.py`, `devclaw/elicitation.py` | one-shot `claude --print` prompt/parse calls |
| 4 | **TaskQueue + engine** | `task_queue.py`, `devclaw/engine/` | dispatch, concurrency, the container launcher, the settle/gate path |
| 5 | **Worker harness** | `openhands-runner/runner.py` (inside the sandbox) | the in-sandbox agent turn-loop, skills, hooks, `verify_cmd` |

There are exactly **two paths through the stack**, and they never cross layers:

- **Cognition:** `1 → 2 → 3`. The heartbeat asks a one-shot `claude` call "what next?"
  and gets structured JSON back. No container, no dispatch.
- **Execution:** `1 → 2 → 4 → 5`. The heartbeat dispatches an *action* into the task
  queue, which launches a per-task docker sandbox, which runs the worker harness.

The chain is strict. Layer 1 must **not** dispatch tasks. Layer 2 must **not** spawn
containers itself — it goes through the engine (layer 4). No layer reaches through
another, and none of them cache another's state.

## The heartbeat is the whole machine

`devclaw/goal/tick.py` is the beating heart: one `tick_goal()` per goal, every
~15 minutes. Everything else is plumbing around it. The tick is a small state
machine over the goal lifecycle:

```
investigating → firming → executing → (done-gate) → done
     │              │          │            │
  repo/world     lock the   dispatch      grounded eval of the firmed
  research       contract   actions,      done_when; closes ONLY if the
                 (done_when) settle them   evaluator says "achieved"
```

Two properties make the heartbeat cheap and safe, and both are load-bearing:

1. **Zero-token idle guard.** An idle goal, or one whose work is still in flight,
   costs **~0 `claude` calls**. The cheap SQLite/timestamp checks run *before* any
   LLM call — this ordering is deliberate and tested (`FakeClaude.calls == 0` on idle
   paths). Adding a tick-path LLM call that fires on idle breaks the quota guarantee.
2. **Per-goal tick lock + CAS.** Only one tick runs per goal at a time, and every
   state write goes through `GoalStore.transition()` — a compare-and-swap against the
   `LEGAL` table in `goal/transitions.py`. A stale-snapshot write raises
   `TransitionConflict` and is abandoned, not silently clobbered. This is what lets
   `steer_goal`/`resume_goal`/`cancel_goal` (from the MCP path) write **concurrently**
   with the heartbeat without corruption.

Since this tranche, `tick.py` is a thin spine plus five modules that split by
concern: `tick_context` (primitives), `tick_guards` (watchdog + block-on-corrupt),
`tick_dispatch` (engine-launch paths), `tick_donegate` (the done-gate), `tick_settle`
(settle & recover). The spine keeps a re-export facade so the split is invisible to
callers.

## One task's journey

When the tick decides to *do* something (not just think):

1. **Branch selection** (`tick_dispatch._dispatch_action`) — a `DeliveryStrategy`
   (`goal/delivery_strategy.py`) decides the branch: checklist-mode goals accumulate
   every item's commits on one shared `goal/<id>` branch (one cumulative PR);
   legacy/per-action goals deliver each action as its own branch + PR.
2. **Prepare the workspace** — `prepare_workspace()` gives the engine a pristine
   checkout on the chosen branch.
3. **Atomic dispatch** — the task-row creation + the `DISPATCH_ACTION` transition +
   the log line commit as **one** SQLite transaction. A crash or CAS conflict rolls
   the whole unit back, so "task dispatched but the in-flight ref was lost" is
   structurally impossible.
4. **Run in a sandbox** — `TaskQueue` claims the row and launches a per-task
   `docker run --rm` (`engine/sandcastle.py`); the worker harness runs the agent turn
   loop and writes line-delimited JSON back on stdout.
5. **The verify gate decides, not the agent** — after the agent finishes, the
   `verify_cmd` runs; its exit code settles done-vs-failed. The agent's self-report is
   never trusted. **The gate fails CLOSED**: a crash *in* the gate settles the task
   `failed`, not approved.
6. **Deliver, then settle** — for `deliver=True` tasks the change becomes a branch/PR
   *before* `done` is observable, so a poller never reads "done without a PR". A
   delivery that can't push/PR settles `failed`, never a silent success.
7. **Settle atomically** — settlement row + delivery row + log + checklist update +
   the `ACTION_SETTLED` transition, as one unit (`tick_settle`). Auto-merge and
   program-stack reconcile run strictly *after* the settle commits.

## Where state lives

**SQLite (`devclaw.db`) is the single source of truth.** Since Tranche 1 the goal
layer lives in the same DB as the task queue: `goal_status`, `goal_steering`,
`goal_log`, `goal_deliveries`, `goal_docs`, `goal_phase_history`. The familiar files —
`STATUS.md`, `log.md`, `inbox.md`, `deliveries.md`, `checklist.yaml`,
`firmed-draft.yaml` — are **generated views**: human- and rollback-readable, **never
read back for decisions**. Only `goal.yaml`, `spec.md`, `discovery.md` stay plain
files.

**Single writer.** Only the `TaskQueue` mutates task rows; `StateStore` is an
append-only event log and its views are projections. Goal state is owned by
`GoalStore` and mutated only through the CAS'd `transition()`.

## The invariants you must not break

- **OAuth only.** `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are *actively stripped*
  at the planner, host engine, and sandbox. A stray key must never silently flip an
  autonomous run onto metered billing.
- **Model-agnostic worker.** Skills are plain markdown; hooks are bash `.sh` files
  invoked by `runner.py` (no `settings.json`); cross-tool capability goes through MCP,
  not vendor tool-wiring. Swapping `claude-code` for another agent should change only
  the `ACPAgent` call.
- **"Done" is a proposal, gated on grounded evaluation.** The planner's `done`
  triggers a read-only review against the firmed `done_when`; the goal closes **only
  if the evaluator confirms `achieved`**. Never gate completion on counting PRs.
- **Loud failure over silent degradation.** Verification fails closed (#186); broken
  delivery fails, never "done without a PR" (#183); lost/corrupt state blocks legibly
  with an owner ping (#185/#188); a usage-limit hit *pauses-and-resumes* (one
  account-wide `paused_until` gates both queue and heartbeat; zero tokens while
  paused; auto-resumes when the cap resets — #189/#190/#191). Blocks carry a
  structured `blocked_kind`, and the two re-checkable mechanical kinds **auto-heal**
  (zero LLM, damped by a persisted per-goal `heal_attempts` budget):
  `mechanical:corrupt_doc` once the contract file parses again (the tick's contract
  probe is the recheck — free, every tick; cap 3), and `mechanical:prep` via a
  `git ls-remote` recheck on a persisted exponential backoff (`next_heal_at`,
  30 min → 6 h; cap 5 — between windows a blocked goal stays a zero-subprocess
  tick). Past its cap a goal parks for a human with one plain ping. `needs_answer`,
  `bug`, `mechanical:lost_ref`, and `mechanical:dispatch_cap` blocks stay
  human-gated on purpose.

## The code map (post-consolidation)

```
devclaw/
├── server/          layer 1 — MCP tools, HTTP/SSE routes, auth+serve
├── goal/            layer 2/3 — the heartbeat + cognition callers
│   ├── tick.py + tick_{context,guards,dispatch,donegate,settle}.py   the loop
│   ├── store/       GoalStore package (base · status[CAS] · content)
│   ├── planner.py · evaluator.py · decomposer.py · transitions.py    cognition + the LEGAL table
│   └── delivery_strategy.py · merge.py · engine.py                   dispatch seams
├── engine/          layer 4 — sandcastle.py (docker run --rm), host.py, stub.py
├── delivery/        commit → branch → push → PR; deploy.py; repo.py
├── quality/         gates past green tests — pre-PR review, eval_judge
├── loom/            engine-agnostic substrate — limits, test_integrity, trace
├── state_store/     StateStore package (rows · control · core) — the append-only log
├── task_queue.py + task_{git,notify}.py    layer 4 — dispatch, concurrency, settle
└── prompts/         every system prompt as a .md file (load_prompt(slug))
openhands-runner/runner.py    layer 5 — the in-sandbox harness
```

## If you're still lost

- **What runs when** → re-read "The heartbeat is the whole machine" above, then
  `goal/tick.py`'s `_tick_goal_impl`.
- **Where a change belongs** → the layer table above; details in
  [`architecture-layers.md`](./architecture-layers.md).
- **How one task flows end to end** → [`task-execution-flow.md`](./task-execution-flow.md).
- **How a dispatch becomes a PR** → [`delivery-flows.md`](./delivery-flows.md).
- **Every doc, with a currency tag** → [`INDEX.md`](./INDEX.md) — read it before
  trusting any other doc.
