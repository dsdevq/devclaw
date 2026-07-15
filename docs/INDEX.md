# docs/ — index & currency map

Every doc under `docs/`, its one-line purpose, and a **currency tag** so a new
reader knows what to trust. Tags:

- **CURRENT** — verified against code; load-bearing claims hold.
- **DECISION RECORD** — a frozen ADR in `decisions/`: the decision stands, the
  point-in-time system descriptions are not maintained for drift.
- **STALE — see note** — contains at least one claim the code contradicts; note says what.

Currency is verified by grepping each doc's load-bearing claims against the code, not
by trusting the doc's own "Status:" line. When you change behavior that a doc
describes, fix the doc **and** update its tag here in the same PR.

## Layout

```
docs/
├── architecture.md    the system doc — mental model + the locked contract
├── flows/             temporal traces (one task; dispatch → PR)
├── reference/         look-up tables (env vars)
├── runbooks/          operational procedures (shakedown, VPS deploy)
└── decisions/         frozen ADRs — rationale, not current state
```

## System

| Doc | Purpose | Currency |
|---|---|---|
| [`architecture.md`](./architecture.md) | **Start here.** Part I: the one-sitting mental model (five layers, two chains, the heartbeat, one task's journey, where state lives). Part II: the **locked contract** — per-layer contracts, the invariants (incl. grounded cognition, auto-heal), testability/replaceability, the question tree, the code map. | **CURRENT** — *written 2026-07-13* by merging `how-it-really-works.md` + `architecture-layers.md` (both deleted; invariants now stated once). Every load-bearing claim re-verified against main @ `8f9a59e` during the 2026-07-13 docs audit (5-agent sweep after the #228–#238 harden-loop tranche). *Updated 2026-07-15*: layer-1 tested-by row now names the telemetry read surface (`GET /traces.json` + `devclaw trace list/report`, pinned by `tests/test_trace_read_surface.py`). |

## Flows

| Doc | Purpose | Currency |
|---|---|---|
| [`flows/task-execution.md`](./flows/task-execution.md) | Temporal trace of ONE task, every hop (node 1 waiter → node 2 devclaw-mcp → node 3 ephemeral sandbox), with a "fails if" rail. | **CURRENT** — *audited 2026-07-13*: all dispatch/settle symbols verified; Step I now lists the REPOSITORY CONTEXT input the review gate gained in #227. Sandbox mechanics re-checked 2026-07-11, unchanged. *Updated 2026-07-15*: Step D now names the per-task acceptance-criteria/constraints (carried inside the goal string) + the structured return contract `_wrap_goal` appends for code tasks. |
| [`flows/delivery.md`](./flows/delivery.md) | How dispatches become PRs: the 3 delivery shapes (backlog / checklist / program), the dispatch cap, the Shape-3 reconcile step. | **CURRENT** — *audited 2026-07-13*: cap-recovery row now names both human verbs (`steer_goal`/`resume_goal`, #228/#238) and notes the cap block never auto-heals; the Shape-1 `pr_state` quote fixed to match `tick_settle.py` verbatim. Cap formula re-verified (algebraically identical to `tick_dispatch.py`). |

## Reference

| Doc | Purpose | Currency |
|---|---|---|
| [`reference/env-vars.md`](./reference/env-vars.md) | Single source of truth for every env var the runtime reads, grouped by purpose. | **CURRENT** — *updated 2026-07-15*: added `DEVCLAW_COGNITION=agent_sdk` (opt-in agent-sdk cognition backend, feat/cognition-sdk-backend); `DEVCLAW_COGNITION_TIMEOUT_S` (cognition-timeout headroom, default 180s replacing the hardcoded 90s cap — now also the agent_sdk inactivity budget); `DEVCLAW_TRACE_RETENTION_DAYS` (trace-retention prune). Prior audit 2026-07-13 (#228–#238 tranche; `DEVCLAW_SKILL_LIBRARY` removed). Doc↔code parity enforced by `tests/test_env_vars_doc_sync.py` (bidirectional). |

## Runbooks

| Doc | Purpose | Currency |
|---|---|---|
| [`runbooks/live-shakedown.md`](./runbooks/live-shakedown.md) | Exercising the real pipeline (logged-in `claude` + docker) layer by layer, L1 single task → L5 abort. | **CURRENT** — *audited 2026-07-13*: every tool named in the steps exists on the MCP surface; L1–L5 descriptions match. |
| [`runbooks/vps-waiter-deploy.md`](./runbooks/vps-waiter-deploy.md) | Deploying the OpenClaw waiter + devclaw to the VPS; the waiter's tool menu. | **CURRENT** — *audited 2026-07-13*: all menu tools exist; `resume_goal` added to the Goals line (#228). |

## Decision records (frozen — rationale, not current state)

| Doc | Purpose | Currency |
|---|---|---|
| [`decisions/0001-openhands-engine.md`](./decisions/0001-openhands-engine.md) | The founding decision: adopt OpenHands as the execution engine; devclaw is thin orchestration. Includes the OpenHands-vs-isolation orthogonality rationale and why devclaw calls `docker run` itself. | **DECISION RECORD** — accepted 2026-06, frozen 2026-07-13 (was `architecture-v2.md`). The decision stands; system snapshots inside reflect their writing date. Current system: [`architecture.md`](./architecture.md). |
| [`decisions/0002-engine-mode.md`](./decisions/0002-engine-mode.md) | The OpenHands-vs-`claude_sdk` engine choice: current default, the switch procedure, and the data a switch requires. | **DECISION RECORD** — frozen 2026-07-13 (was `engine-decision.md`). Default (`run_sandcastle`) and `DEVCLAW_ENGINE` values re-verified at freeze time. |

## Where the docs are NOT

- **The agent harness contract** — [`../CLAUDE.md`](../CLAUDE.md): the distilled
  working contract an agent reads before touching the repo. It deliberately
  duplicates the invariants in condensed form; `architecture.md` is the canonical
  statement.
- **The product narrative** — [`../README.md`](../README.md).
- **Worker-layer skills** — `.agent/skills/` (product, not harness docs).
- **Generated views** (`STATUS.md`/`log.md`/`deliveries.md` under goal dirs) —
  projections, never hand-edited, never audited here.
