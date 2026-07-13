# DevClaw — Layered Architecture (the locked model)

**Status:** Locked. The mental model below is the system. Proposed changes that violate a layer boundary or invariant should be reviewed as architectural changes, not feature changes.

This doc complements [`architecture-v2.md`](./architecture-v2.md) (the "why OpenHands" decision) by spelling out the layer contracts and the testability/replaceability story for each.

---

## The 5 layers

| # | Layer | Code | Owns |
|---|---|---|---|
| 1 | **MCP surface** | `devclaw/server/` | HTTP/stdio transport, MCP tool decorators, auth middleware, dashboard. Pure protocol. |
| 2 | **Orchestrator** (GoalService + heartbeat) | `devclaw/goal/` | State machine, lifecycle (`investigating → firming → executing`), scheduler, persistence. Owns goal state (SQLite-backed since Tranche 1). |
| 3 | **Cognition callers** | `devclaw/goal/planner.py`, `decomposer.py`, `evaluator.py`, `phases/firming.py`, `summary.py`, `world_research.py`; `devclaw/elicitation.py` (scope_grill) | One-shot `claude --print` invocations with baked prompts + goal state; return parsed YAML. |
| 4 | **TaskQueue + Engine** | `devclaw/task_queue.py`, `devclaw/engine/` | Receives task dispatches, runs them (in sandbox / on host / in stub), streams events back. |
| 5 | **Worker harness** | `openhands-runner/runner.py` (inside sandbox image) | The agent turn-loop: `claude-agent-acp` → `claude-code` CLI + tools + MCP. Only true agent harness in the stack. |

Above layer 1: humans + OpenClaw waiter. Below layer 5: Claude (the LLM, via Pro OAuth).

---

## Layer contracts

### Layer 1 — MCP surface

- **Public surface:** every `@mcp.tool` decorator in `devclaw/server/tools.py`. HTTP endpoints in `devclaw/server/http.py`.
- **Allowed to call:** layer 2 (`goals.create_goal(...)`, `goals.get_goal(...)`, etc.) and the project registry.
- **Forbidden:** reaching into layer 4 directly (e.g. dispatching tasks bypassing `GoalService`), touching the file-system mind directly (must go through `GoalStore`).
- **Tested by:** `tests/test_dashboard.py`, `tests/test_console_prs_endpoint.py` — full HTTP/tool requests against the FastMCP app (via the in-process client in `conftest.py`) with the layers below stubbed.

### Layer 2 — Orchestrator

- **Public surface:** `GoalService` methods (`create_goal`, `get_goal`, `answer_unknowns`, `steer_goal`, `resume_goal`, `evaluate_goal`, `cancel_goal`, …). Plus the heartbeat loop owned by `serve_loop`.
- **Internal state:** `GoalStore`, backed by the goal-state tables inside the SAME `StateStore`/`devclaw.db` the task queue uses (`goal_status`, `goal_steering`, `goal_log`, `goal_deliveries`, `goal_docs`, `goal_phase_history` — Tranche 1). `goal.yaml` (facts), `spec.md`, `discovery.md` stay plain files; `STATUS.md` / `log.md` / `inbox.md` / `deliveries.md` / `checklist.yaml` / `firmed-draft.yaml` are generated **views** rewritten after every write, for human reading and rollback — never read back for decisions.
- **Allowed to call:** layer 3 (cognition callers) and layer 4 (via the in-process engine).
- **Forbidden:** spawning sandbox containers directly (must go through `TaskQueue` + `Engine`); calling `claude` directly (must go through a cognition caller); mutating `goal_status`'s phase/lifecycle/in_flight outside `GoalStore.transition()` (the CAS'd choke point in `devclaw/goal/transitions.py`).
- **Tested by:** `tests/test_goal_*.py` (e.g. `test_goal_tick.py`, `test_goal_engine.py`, `test_goal_reconcile.py`), `tests/test_firming_handler.py`, `tests/test_goal_tick_firming.py` — drive single ticks with stubbed cognition + stubbed engine. The SQLite-backed state substrate itself: `tests/test_goal_state.py` (the `GoalState` tables), `tests/test_goal_store.py` + `tests/test_goal_store_checklist.py` (`GoalStore`'s row-first/view-mirror behavior, migration from legacy `.md`/`.yaml`), `tests/test_goal_transitions.py` (the `LEGAL` table + CAS/legality guards in isolation).

### Layer 3 — Cognition callers

- **Public surface:** each module exposes a `default_caller()` factory and a per-purpose `build_prompt()` + `parse_response()` pair. The protocol they all conform to lives in `devclaw/cognition.py` (`Cognition` protocol).
- **Internal state:** none. Pure functions over (prompt-template + goal-state) → (subprocess) → parsed output.
- **Allowed to call:** `claude --print` via subprocess (today); any LLM via the `Cognition` protocol.
- **Forbidden:** writing to the goal store directly (return parsed output, let layer 2 persist it); reaching into the task queue.
- **Tested by:** `tests/test_cognition.py`, `tests/test_goal_decomposer.py`, `tests/test_goal_evaluator.py` — assert prompt rendering + response parsing in isolation, with the LLM call stubbed.

### Layer 4 — TaskQueue + Engine

- **Public surface:** `Engine` protocol (`devclaw/engine/__init__.py`) — one async callable: `(EngineRequest) → EngineResult`. `TaskQueue` lifecycle methods (`submit`, `cancel`, on-settle callbacks).
- **Engine implementations:** `sandcastle.py` (production, docker per task), `claude_sdk.py` (in-sandbox claude --print spike), `host.py` (host-side, no sandbox — testing only), `stub.py` (deterministic, no LLM).
- **Allowed to call:** docker socket (sandcastle only), the workspace filesystem.
- **Forbidden:** reading the goal store (the orchestrator passes everything the engine needs in `EngineRequest`); writing event lines that aren't valid protocol.
- **Tested by:** queue lifecycle in `tests/test_queue_dag.py`, `tests/test_durability.py`, `tests/test_task_retry.py`, `tests/test_task_timeout.py`, `tests/test_rate_limit_pause.py`; engine/sandbox behavior in `tests/test_workspace_breaker.py`, `tests/test_sandbox_isolation.py`, `tests/test_container_hygiene.py`, `tests/test_stub_engine.py`, `tests/test_claude_sdk_engine.py`. The stub engine also drives all higher-layer tests so they need no docker / no claude.

### Layer 5 — Worker harness

- **Public surface:** the `runner.py` JSON-line stdout protocol (`event:` lines + a single terminating `result:` line). Layer 4 (sandcastle) consumes this.
- **Behavior:** reads `/opt/devclaw/skills/` per kind, drops `/workspace/.mcp.json` for Playwright MCP, fires pre/post hooks (universal + per-repo), runs the agent loop, runs verify_cmd, emits `result:`.
- **Allowed to depend on:** `claude-agent-acp`, `claude-code`, MCP servers (Playwright today), the per-task `/workspace` git checkout.
- **Forbidden:** importing anything from the devclaw Python package (the harness runs in a different container; cross-process boundary). Writing files outside `/workspace`. Using claude-code-specific harness features (skills/hooks `settings.json`) — see model-agnostic invariants.
- **Tested by:** `tests/test_runner_wrappers.py`, `tests/test_runner_skills.py`, `tests/test_runner_io.py` — import the module file directly (it's not a package) and exercise pure functions with the SDK call stubbed out.

---

## Invariants (what NOT to do)

### Layer-separation invariants

1. **No cross-layer reach-through.** Layer 1 must not call layer 3 or 4 directly. Layer 2 must not bypass layer 4 to spawn containers itself. The chain is strict: 1 → 2 → 3 (for cognition) or 1 → 2 → 4 → 5 (for execution).
2. **Single source of truth per state.** Goal state lives in `GoalStore`, SQLite-backed via `GoalState` inside the shared `StateStore`/`devclaw.db` (Tranche 1) — `.md`/`.yaml` files are generated views, not read back for decisions. Task/program state lives in the same `StateStore`. Each is owned by one layer (layer 2). No caching of either in upstream layers.
3. **Engines are pure async callables.** An engine implementation may not assume which orchestrator called it. It receives an `EngineRequest`, returns an `EngineResult`. No back-channel.
4. **Cognition callers are stateless.** Every call gets the full prompt + state it needs as input. No process-level memory between calls.

### Model-agnostic invariants

The worker harness layer is the *only* place model-coupling is allowed. Everything below it must work with any LLM-driven agent that can read files + call tools.

1. **Skills are plain markdown.** No frontmatter with model-specific fields. No `Skill(name=…)` tool invocations in prompts (that's Claude's native skill system, not ours).
2. **Hooks are bash, not harness-native config.** Hooks live as `.sh` files in `/opt/devclaw/hooks/` or `<repo>/.agent/hooks/`. Not in `settings.json` or any other harness-specific config.
3. **Tools cross via MCP, not vendor wiring.** Tools we want every agent to have go through MCP (Cline, Cursor, Zed, Claude Code all support it). Not through Claude-Code plugins or commands.
4. **Per-repo discovery is `ls` + `cat`.** No agent-specific catalog API for finding `.agent/skills/`.

### Persistence invariants

1. **Goals are durable.** `GoalStore`'s phase/lifecycle/in_flight changes go through `GoalStore.transition()` — a CAS'd (compare-and-swap on stored `(state, version)`) write inside a `StateStore` transaction, legality-checked against the `LEGAL` table in `devclaw/goal/transitions.py`. This is NOT heartbeat-exclusive: `steer_goal`, `resume_goal`, and `cancel_goal` write directly from the MCP-tool call path (layer 1→2), concurrently with the heartbeat — the CAS exists precisely to stop one writer's stale snapshot from clobbering the other's write (a mismatch raises `TransitionConflict`, abandoning that write cleanly). Generated `.md`/`.yaml` views are written atomically (tmp-file + `os.replace`) immediately after each transaction commits.
2. **Tasks are append-only events.** `StateStore`'s `events` table is an append-only log; mutations are appends. State views (program/task status) are projections. (Goal-state tables in the SAME `StateStore` are not all append-only — `goal_status`/`goal_docs` are mutable single-row-per-key, CAS'd or upserted; `goal_steering`/`goal_log`/`goal_deliveries`/`goal_phase_history` are append-only, matching their `.md` view's append-only shape.)
3. **Hooks may write best-effort.** Pre-run / post-run hooks may write scratch files (`.devclaw-pre-head`); post-run is responsible for cleaning them up. No hook output is durable beyond `hook_warnings` in the runner result.

---

## Testability story (one stub at every seam)

| Seam | Stub | Where |
|---|---|---|
| LLM call (cognition) | `StubCognition` | `devclaw/cognition.py` |
| Engine | `StubEngine` | `devclaw/engine/stub.py` |
| Notifier | `NullNotifier` | `devclaw/goal/notify.py` |
| Phase handler registry | reset + register fakes | `devclaw/goal/phases/registry.py` |
| MCP transport | direct in-process FastMCP client | `tests/conftest.py` |
| Sandbox docker | (no stub — layer 4 above the engine seam handles this with the stub engine) | — |
| Worker harness | (no stub yet — runner.py is exercised by importing the module) | gap |

Anything that requires a real `claude` call or real `docker run` is an integration test, not a unit test. The full `pytest` run is unit-only.

---

## Replaceability proofs

| Component | Implementations today | Proof of replaceability |
|---|---|---|
| Engine (layer 4) | 4 (sandcastle, claude_sdk, host, stub) | ✅ strong |
| Notifier | 2 (`HttpNotifier`, `NullNotifier`) | ✅ ok |
| Cognition | 2 (Claude subprocess, Stub) | ⚠ weak — only the stub-vs-real axis |
| Worker harness (layer 5) | 1 (claude-agent-acp + claude-code) | ❌ no proof — model-agnostic invariants exist but unenforced |
| Phase handler | 1 (FirmingHandler) | n/a — registry exists, only one handler so far |

Closing the worker-harness replaceability gap is the highest-value next muscle (build a second implementation, even trivial, to prove the seam).

---

## How to add new functionality (the question tree)

Before adding new code, ask in order:

1. **Does this fit an existing layer?** → Put it there. Most things do.
2. **Is it a new SURFACE on an existing layer?** → Extend the layer's protocol, write the test that asserts conformance, implement.
3. **Is it cross-layer machinery (skills, hooks)?** → It belongs in the worker harness layer (layer 5). It must be model-agnostic (see invariants).
4. **Is it a NEW LAYER?** → **Stop.** Probably not. Re-read the layer contracts above. Talk to Denys before proposing.

---

## What's NOT in this doc

- **Why** we chose OpenHands, Pro OAuth, FastMCP, etc. — see [`architecture-v2.md`](./architecture-v2.md).
- **Engine mode decision tree** — see [`engine-decision.md`](./engine-decision.md).
- **Env vars** — see [`env-vars.md`](./env-vars.md).
- **Live shakedown procedure** — see [`live-shakedown.md`](./live-shakedown.md).
- **Task execution flow** (the timeline of one task) — see [`task-execution-flow.md`](./task-execution-flow.md).

This doc is the layer reference. Other docs are decision logs, operational guides, or flow descriptions.
