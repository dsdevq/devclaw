# devclaw architecture

> **The system doc.** Part I is the mental model — read it when you've lost the
> thread. Part II is the **locked contract** (layer boundaries, invariants,
> testability): changes that violate it are architectural changes, not feature
> changes. The code is the territory — when this doc and the code disagree,
> trust the code, then fix this doc. Historical rationale (why OpenHands, why
> Pro OAuth) lives in [`decisions/`](./decisions/), not here.

## The one paragraph

devclaw is a **software-development agentic loop**. You hand it a durable *goal*
with verifiable completion criteria; a self-executing heartbeat carries it —
**plan → sandboxed execution → verify gate → evaluate → iterate** — with hard
brakes (retry caps, a no-progress watchdog, `stalled`/`needs_human` verdicts) so
it never optimises into the void. It sits **behind MCP** and is driven by an
**OpenClaw "waiter" agent** that turns chat into tool calls; **devclaw never
talks to the user directly**. Cognition is always `claude` over a Pro/Max
**OAuth** session — **no API key, no metered billing, ever**.

---

# Part I — the mental model

## The five layers (and the two chains)

The system is five layers below the user. Only layer 5 is an agent harness in
the technical sense — the rest is orchestration.

| # | Layer | Lives in | Owns |
|---|---|---|---|
| 1 | **MCP surface** | `devclaw/server/` | tools, auth, dashboard, transport — pure protocol |
| 2 | **GoalService + heartbeat** | `devclaw/goal/` | the goal state machine + the ~15-min tick |
| 3 | **Cognition callers** | `goal/{planner,evaluator,decomposer,research,world_research,summary}.py`, `goal/phases/firming.py`, `devclaw/planner.py`, `devclaw/elicitation.py` | one-shot `claude --print` prompt/parse calls |
| 4 | **TaskQueue + engine** | `task_queue.py`, `devclaw/engine/` | dispatch, concurrency, the container launcher, the settle/gate path |
| 5 | **Worker harness** | `openhands-runner/runner.py` (inside the sandbox) | the in-sandbox agent turn-loop, skills, hooks, `verify_cmd` |

There are exactly **two paths through the stack**, and they never cross layers:

- **Cognition:** `1 → 2 → 3`. The heartbeat asks a one-shot `claude` call "what
  next?" and gets structured output back. No container, no dispatch.
- **Execution:** `1 → 2 → 4 → 5`. The heartbeat dispatches an *action* into the
  task queue, which launches a per-task docker sandbox, which runs the worker
  harness.

The chain is strict. Layer 1 must **not** dispatch tasks. Layer 2 must **not**
spawn containers itself — it goes through the engine (layer 4). No layer reaches
through another, and none of them cache another's state.

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

1. **Zero-token idle guard.** An idle goal, or one whose work is still in
   flight, costs **~0 `claude` calls**. The cheap SQLite/timestamp checks run
   *before* any LLM call — this ordering is deliberate and tested
   (`FakeClaude.calls == 0` on idle paths). Adding a tick-path LLM call that
   fires on idle breaks the quota guarantee.
2. **Per-goal tick lock + CAS.** Only one tick runs per goal at a time, and
   every state write goes through `GoalStore.transition()` — a compare-and-swap
   against the `LEGAL` table in `goal/transitions.py`. A stale-snapshot write
   raises `TransitionConflict` and is abandoned, not silently clobbered. This is
   what lets `steer_goal`/`resume_goal`/`cancel_goal` (from the MCP path) write
   **concurrently** with the heartbeat without corruption.

`tick.py` is a thin spine plus five modules split by concern: `tick_context`
(primitives), `tick_guards` (watchdog + block/auto-heal), `tick_dispatch`
(engine-launch paths), `tick_donegate` (the done-gate), `tick_settle` (settle &
recover). The spine keeps a re-export facade so the split is invisible to
callers.

## One task's journey

When the tick decides to *do* something (not just think):

1. **Branch selection** (`tick_dispatch._dispatch_action`) — a
   `DeliveryStrategy` (`goal/delivery_strategy.py`) decides the branch:
   checklist-mode goals accumulate every item's commits on one shared
   `goal/<id>` branch (one cumulative PR); legacy/per-action goals deliver each
   action as its own branch + PR.
2. **Prepare the workspace** — `prepare_workspace()` gives the engine a pristine
   checkout on the chosen branch.
3. **Atomic dispatch** — the task-row creation + the `DISPATCH_ACTION`
   transition + the log line commit as **one** SQLite transaction. A crash or
   CAS conflict rolls the whole unit back, so "task dispatched but the in-flight
   ref was lost" is structurally impossible.
4. **Run in a sandbox** — `TaskQueue` claims the row and launches a per-task
   `docker run --rm` (`engine/sandcastle.py`); the worker harness runs the agent
   turn loop and writes line-delimited JSON back on stdout.
5. **The verify gate decides, not the agent** — after the agent finishes, the
   `verify_cmd` runs; its exit code settles done-vs-failed. The agent's
   self-report is never trusted. **The gate fails CLOSED**: a crash *in* the
   gate settles the task `failed`, not approved.
6. **Deliver, then settle** — for `deliver=True` tasks the change becomes a
   branch/PR *before* `done` is observable, so a poller never reads "done
   without a PR". A delivery that can't push/PR settles `failed`, never a silent
   success.
7. **Settle atomically** — settlement row + delivery row + log + checklist
   update + the `ACTION_SETTLED` transition, as one unit (`tick_settle`).
   Auto-merge and program-stack reconcile run strictly *after* the settle
   commits.

The full temporal trace of one task, every hop, lives in
[`flows/task-execution.md`](./flows/task-execution.md); how dispatches become
PRs in [`flows/delivery.md`](./flows/delivery.md).

## Where state lives

**SQLite (`devclaw.db`) is the single source of truth.** Since Tranche 1 the
goal layer lives in the same DB as the task queue: `goal_status`,
`goal_steering`, `goal_log`, `goal_deliveries`, `goal_docs`,
`goal_phase_history`. The familiar files — `STATUS.md`, `log.md`, `inbox.md`,
`deliveries.md`, `checklist.yaml`, `firmed-draft.yaml` — are **generated
views**: human- and rollback-readable, **never read back for decisions**. Only
`goal.yaml`, `spec.md`, `discovery.md` stay plain files.

**Single writer.** Only the `TaskQueue` mutates task rows; `StateStore` is an
append-only event log and its views are projections. Goal state is owned by
`GoalStore` and mutated only through the CAS'd `transition()`.

---

# Part II — the locked contract

## Layer contracts

### Layer 1 — MCP surface

- **Public surface:** every `@mcp.tool` decorator in `devclaw/server/tools.py`.
  HTTP endpoints in `devclaw/server/http.py`.
- **Allowed to call:** layer 2 (`goals.create_goal(...)`, `goals.get_goal(...)`,
  etc.) and the project registry.
- **Forbidden:** reaching into layer 4 directly (e.g. dispatching tasks
  bypassing `GoalService`), touching goal state directly (must go through
  `GoalStore`).
- **Tested by:** `tests/test_dashboard.py`, `tests/test_console_prs_endpoint.py`
  — full HTTP/tool requests against the FastMCP app (via the in-process client
  in `conftest.py`) with the layers below stubbed. The general telemetry read
  surface (`GET /traces.json` + the `devclaw trace list`/`trace report` CLI —
  pure SELECTs over the `traces`/`tasks` tables, filters applied in SQL):
  `tests/test_trace_read_surface.py`.

### Layer 2 — Orchestrator (GoalService + heartbeat)

- **Public surface:** `GoalService` methods (`create_goal`, `get_goal`,
  `answer_unknowns`, `steer_goal`, `resume_goal`, `evaluate_goal`,
  `cancel_goal`, …). Plus the heartbeat loop owned by `serve_loop`.
- **Internal state:** `GoalStore`, backed by the goal-state tables inside the
  SAME `StateStore`/`devclaw.db` the task queue uses (see "Where state lives").
- **Allowed to call:** layer 3 (cognition callers) and layer 4 (via the
  in-process engine).
- **Forbidden:** spawning sandbox containers directly (must go through
  `TaskQueue` + `Engine`); calling `claude` directly (must go through a
  cognition caller); mutating `goal_status`'s phase/lifecycle/in_flight outside
  `GoalStore.transition()` (the CAS'd choke point).
- **Tested by:** `tests/test_goal_*.py` (e.g. `test_goal_tick.py`,
  `test_goal_engine.py`, `test_goal_reconcile.py`), `tests/test_firming_handler.py`,
  `tests/test_goal_tick_firming.py` — single ticks with stubbed cognition +
  stubbed engine. The SQLite substrate: `tests/test_goal_state.py`,
  `tests/test_goal_store.py`, `tests/test_goal_store_checklist.py`,
  `tests/test_goal_transitions.py` (the `LEGAL` table + CAS in isolation).

### Layer 3 — Cognition callers

- **Public surface:** each module exposes a `default_caller()` factory and a
  per-purpose `build_prompt()` + `parse_response()` pair. The protocol lives in
  `devclaw/cognition.py` (`Cognition` protocol).
- **Internal state:** none. Pure functions over (prompt-template + goal-state +
  a workspace snapshot collected at the call site) → (subprocess) → parsed
  output.
- **Allowed to call:** `claude --print` via subprocess (today); any LLM via the
  `Cognition` protocol. Snapshot collectors additionally shell out to `git`
  (read-only, best-effort, never-raises) — see the grounded-cognition
  invariant below.
- **Forbidden:** writing to the goal store directly (return parsed output, let
  layer 2 persist it); reaching into the task queue.
- **Tested by:** `tests/test_cognition.py`, `tests/test_goal_decomposer.py`,
  `tests/test_goal_evaluator.py` — prompt rendering + response parsing in
  isolation, LLM call stubbed.

### Layer 4 — TaskQueue + Engine

- **Public surface:** `Engine` protocol (`devclaw/engine/__init__.py`) — one
  async callable: `(EngineRequest) → EngineResult`. `TaskQueue` lifecycle
  methods (`submit`, `cancel`, on-settle callbacks).
- **Engine implementations:** `sandcastle.py` (production, docker per task),
  `claude_sdk.py` (in-sandbox claude --print spike), `host.py` (host-side, no
  sandbox — testing only), `stub.py` (deterministic, no LLM).
- **Allowed to call:** docker socket (sandcastle only), the workspace
  filesystem.
- **Forbidden:** reading the goal store (the orchestrator passes everything the
  engine needs in `EngineRequest`); writing event lines that aren't valid
  protocol.
- **Tested by:** queue lifecycle in `tests/test_queue_dag.py`,
  `tests/test_durability.py`, `tests/test_task_retry.py`,
  `tests/test_task_timeout.py`, `tests/test_rate_limit_pause.py`;
  engine/sandbox behavior in `tests/test_workspace_breaker.py`,
  `tests/test_sandbox_isolation.py`, `tests/test_container_hygiene.py`,
  `tests/test_stub_engine.py`, `tests/test_claude_sdk_engine.py`. The stub
  engine also drives all higher-layer tests so they need no docker / no claude.

### Layer 5 — Worker harness

- **Public surface:** the `runner.py` JSON-line stdout protocol (`event:` lines
  + a single terminating `result:` line). Layer 4 (sandcastle) consumes this.
- **Behavior:** concatenates the always-on **doctrine** skills from
  `/opt/devclaw/skills/` per kind (`_common` + the `_writes-code/*` tier for
  code-writing kinds + the `<kind>/*` tier) into the brief; the sibling
  `craft/` dir (self-selected how-to guides — `frontend-design`, `playwright`)
  is **not** concatenated — `_common` points the agent to `ls`/`cat` it for the
  guide a task needs (progressive disclosure). Drops `/workspace/.mcp.json` for
  Playwright MCP, fires pre/post hooks (universal + per-repo), runs the agent
  loop, runs `verify_cmd`, emits `result:`.
- **Allowed to depend on:** `claude-agent-acp`, `claude-code`, MCP servers, the
  per-task `/workspace` git checkout.
- **Forbidden:** importing anything from the devclaw Python package (different
  container; cross-process boundary). Writing files outside `/workspace`. Using
  claude-code-specific harness features (skills/hooks `settings.json`) — see
  the model-agnostic invariants.
- **Tested by:** `tests/test_runner_wrappers.py`, `tests/test_runner_skills.py`,
  `tests/test_runner_io.py` — import the module file directly and exercise pure
  functions with the SDK call stubbed.

## Invariants

### Layer separation

1. **No cross-layer reach-through.** The chain is strict: `1 → 2 → 3`
   (cognition) or `1 → 2 → 4 → 5` (execution).
2. **Single source of truth per state.** Goal state in `GoalStore`
   (SQLite-backed), task/program state in the same `StateStore`. Each owned by
   layer 2; no caching in upstream layers; generated `.md`/`.yaml` files are
   views, never read back for decisions.
3. **Engines are pure async callables.** An engine may not assume which
   orchestrator called it: `EngineRequest` in, `EngineResult` out, no
   back-channel.
4. **Cognition callers are stateless.** Every call gets the full prompt + state
   it needs as input. No process-level memory between calls.

### Grounded cognition

Every host-side cognition caller that reasons about the target repository —
planner (per-tick and `plan_goal`), evaluator, decomposer, firming, discovery
research, and the pre-PR review gate — is fed a **read-only git snapshot of the
goal's actual workspace** (`task_git._review_repo_context_sync`: remote, branch,
HEAD, key-file probes, tracked layout), and its prompt forbids inferring repo
facts from the host process, cwd, or remembered repositories. Collection is
best-effort and never raises (grounding can't fail a step), runs only where
cognition already runs (the zero-token idle guard is untouched), and adds no
LLM calls. Rationale: host-side `claude` inherits devclaw's own checkout as
ambient context — ungrounded, it can substitute the wrong codebase (the #227
wrong-codebase review bug and its siblings, fixed 2026-07-13).

### OAuth and billing

`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` are **actively stripped** at the
planner, host engine, and sandbox. A stray key must never silently flip an
autonomous run onto metered billing.

### Model-agnostic worker layer

The worker harness (layer 5) is the *only* place model-coupling is allowed.

1. **Skills are plain markdown.** No model-specific frontmatter, no native
   `Skill(...)` invocations.
2. **Hooks are bash `.sh` files**, not harness-native config (`settings.json`).
3. **Tools cross via MCP**, not vendor wiring.
4. **Per-repo discovery is `ls` + `cat`** — no agent-specific catalog API.

Swapping `claude-code` for another agent should change only the `ACPAgent`
call.

### Persistence

1. **Goals are durable.** Phase/lifecycle/in_flight changes go through
   `GoalStore.transition()` — CAS'd against the `LEGAL` table inside a
   `StateStore` transaction. NOT heartbeat-exclusive: `steer_goal`,
   `resume_goal`, and `cancel_goal` write from the MCP-tool path concurrently
   with the heartbeat; the CAS is what makes that safe. Views are written
   atomically (tmp-file + `os.replace`) after each transaction commits. There
   is **no** `update_goal`/field-patch surface: a wrong contract is
   cancel + recreate.
2. **Tasks are append-only events.** `StateStore`'s `events` table is an
   append-only log; state views are projections. (Goal-state tables:
   `goal_status`/`goal_docs` are mutable single-row-per-key, CAS'd or upserted;
   `goal_steering`/`goal_log`/`goal_deliveries`/`goal_phase_history` are
   append-only.)
3. **Hooks may write best-effort.** Pre/post-run hooks may write scratch files;
   nothing durable beyond `hook_warnings` in the runner result.

### "Done" is a proposal

The planner's `done` triggers a read-only review against the firmed
`done_when`; the goal closes **only if the evaluator confirms `achieved`** —
never on counting PRs or backlog items. The owner notification says
"(verified)" only when a repo review actually grounded the close; an
artifact-only close (per-project `verify_done` off) is labeled as such.

### Loud failure over silent degradation

Verification fails closed (#186); an unreviewable change fails closed *and
fast*, not forever (#223); broken delivery fails, never "done without a PR"
(#183); lost/corrupt state blocks legibly with an owner ping (#185/#188); a
usage-limit hit *pauses-and-resumes* (one account-wide `paused_until` gates
queue and heartbeat, zero tokens while paused, auto-resumes on cap reset —
#189/#190/#191).

Blocks carry a structured `blocked_kind`, and the two re-checkable mechanical
kinds **auto-heal** (zero LLM, damped by a persisted per-goal `heal_attempts`
budget): `mechanical:corrupt_doc` once the contract file parses again (the
tick's contract probe is the recheck — free, every tick; cap 3), and
`mechanical:prep` via a `git ls-remote` recheck on a persisted exponential
backoff (`next_heal_at`, 30 min → 6 h; cap 5 — between windows a blocked goal
stays a zero-subprocess tick). Past its cap a goal parks for a human with one
plain ping. `needs_answer`, `bug`, `mechanical:lost_ref`, and
`mechanical:dispatch_cap` blocks stay human-gated on purpose; recovery verbs
are `resume_goal` (blocker cleared, same contract) and `steer_goal` (direction
change) — both restore the heal budget.

## Testability (one stub at every seam)

| Seam | Stub | Where |
|---|---|---|
| LLM call (cognition) | `StubCognition` | `devclaw/cognition.py` |
| Engine | `StubEngine` | `devclaw/engine/stub.py` |
| Notifier | `NullNotifier` | `devclaw/goal/notify.py` |
| Phase handler registry | reset + register fakes | `devclaw/goal/phases/registry.py` |
| MCP transport | in-process FastMCP client | `tests/conftest.py` |
| Sandbox docker | (stub engine covers the seam above it) | — |
| Worker harness | (no stub yet — runner.py exercised by module import) | gap |

Anything that needs a real `claude` call or real `docker run` is an integration
test, not a unit test. The full `pytest` run is unit-only — see
[`runbooks/live-shakedown.md`](./runbooks/live-shakedown.md) for the real
pipeline.

## Replaceability proofs

| Component | Implementations today | Proof |
|---|---|---|
| Engine (layer 4) | 4 (sandcastle, claude_sdk, host, stub) | ✅ strong |
| Notifier | 2 (`HttpNotifier`, `NullNotifier`) | ✅ ok |
| Cognition | 2 (Claude subprocess, Stub) | ⚠ weak — only stub-vs-real |
| Worker harness (layer 5) | 1 (claude-agent-acp + claude-code) | ❌ no proof — invariants exist but unenforced |
| Phase handler | 1 (FirmingHandler) | n/a — registry exists, one handler |

Closing the worker-harness replaceability gap is the highest-value next muscle.

## How to add new functionality

Before adding new code, ask in order:

1. **Does this fit an existing layer?** → Put it there. Most things do.
2. **Is it a new SURFACE on an existing layer?** → Extend the layer's protocol,
   write the conformance test, implement.
3. **Is it cross-layer machinery (skills, hooks)?** → Worker harness (layer 5),
   and it must be model-agnostic.
4. **Is it a NEW LAYER?** → **Stop.** Probably not. Re-read the contracts.
   Talk to Denys before proposing.

## The code map

```
devclaw/
├── server/          layer 1 — MCP tools, HTTP/SSE routes, auth+serve
├── goal/            layer 2/3 — the heartbeat + cognition callers
│   ├── tick.py + tick_{context,guards,dispatch,donegate,settle}.py   the loop
│   ├── store/       GoalStore package (base · status[CAS] · content)
│   ├── planner.py · evaluator.py · decomposer.py · research.py · transitions.py   cognition + the LEGAL table
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

## Where to look next

- **What runs when** → "The heartbeat is the whole machine" above, then
  `goal/tick.py`'s `_tick_goal_impl`.
- **How one task flows end to end** → [`flows/task-execution.md`](./flows/task-execution.md).
- **How a dispatch becomes a PR** → [`flows/delivery.md`](./flows/delivery.md).
- **Every env var** → [`reference/env-vars.md`](./reference/env-vars.md).
- **Why OpenHands / why this shape** → [`decisions/0001-openhands-engine.md`](./decisions/0001-openhands-engine.md).
- **Every doc, with a currency tag** → [`INDEX.md`](./INDEX.md) — read it before
  trusting any other doc.
