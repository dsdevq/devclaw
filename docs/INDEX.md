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
| [`architecture.md`](./architecture.md) | **Start here.** Part I: the one-sitting mental model (five layers, two chains, the heartbeat, one task's journey, where state lives). Part II: the **locked contract** — per-layer contracts, the invariants (incl. grounded cognition, auto-heal), testability/replaceability, the question tree, the code map. | **CURRENT** — *written 2026-07-13* by merging `how-it-really-works.md` + `architecture-layers.md` (both deleted; invariants now stated once). Every load-bearing claim re-verified against main @ `8f9a59e` during the 2026-07-13 docs audit (5-agent sweep after the #228–#238 harden-loop tranche). *Updated 2026-07-15*: layer-1 tested-by row now names the telemetry read surface (`GET /traces.json` + `devclaw trace list/report`, pinned by `tests/test_trace_read_surface.py`); layer-3 gained an **operator-inspection** note for the `devclaw cognition plan\|decompose` dry-run (one cognition call, no docker/queue/state; `tests/test_cli_cognition.py`); layer-5 behavior row now describes the doctrine (always-on) vs `craft/` (self-selected, not concatenated) skill split. *Updated 2026-07-15*: "Where state lives" gained the **`problems` catalog** — the deduplicated self-observability capture layer (`StateStore.record_problem`, single writer, fingerprint UPSERT, wired at the block/task-fail/limit/trace choke points; `devclaw/state_store/problems.py`, `tests/test_problems_catalog.py`). *Updated 2026-07-16*: the `problems` catalog gained its **read surface** — the `list_problems` MCP tool (most-frequent first, optional `category` filter; `devclaw/server/tools.py`, `tests/test_list_problems_tool.py`). *Updated 2026-07-17*: mental-model gate step 5 now names the **browser-E2E gate** (`quality/browser_gate.py`) — a web-UI change must carry a passing real-browser Playwright run (parsed `browser_report` counts) or fail closed; flexible/strict per-project. *Updated 2026-07-18*: "Where state lives" gained the **self-triage** propose-only interceptor (`goal/triage.py`, layer-3 caller row + prompt `self-triage.md`) — the first consumer of the `problems` catalog, routing an eligible owner ping (allowlist: DB-size alarm) through a bounded dedupe+propose step; `tests/test_self_triage.py`. *Updated 2026-07-19*: layer-5 deps + replaceability-proofs row reflect the new `DEVCLAW_ACP_COMMAND` worker-agent config seam (seam tested; no second implementation exercised yet). *Updated 2026-07-19*: code map notes the quality gate is self-contained (own `quality/prompts/` + README; the 3 gate prompts moved out of `devclaw/prompts/`). *Updated 2026-07-18*: browser-E2E gate trigger scoped to **app surface** — a library-only diff (`*/src/lib/*`) is `not_triggered` (no app route to visit; proof = story+spec via the library build/test gate; evidence from a run that actually executed is still processed in full); `tests/test_browser_gate.py::test_browser_gate_library_only_diff_not_triggered`. |

## Flows

| Doc | Purpose | Currency |
|---|---|---|
| [`flows/task-execution.md`](./flows/task-execution.md) | Temporal trace of ONE task, every hop (node 1 waiter → node 2 devclaw-mcp → node 3 ephemeral sandbox), with a "fails if" rail. | **CURRENT** — *audited 2026-07-13*: all dispatch/settle symbols verified; Step I now lists the REPOSITORY CONTEXT input the review gate gained in #227. Sandbox mechanics re-checked 2026-07-11, unchanged. *Updated 2026-07-15*: Step D now names the per-task acceptance-criteria/constraints (carried inside the goal string) + the structured return contract `_wrap_goal` appends for code tasks. Step I now names `review_panel` + the opt-in `DEVCLAW_REVIEW_PANEL_N` diverse-lens panel (fail-closed on sub-quorum). *Updated 2026-07-19*: sandbox diagram shows `acp_command` from `DEVCLAW_ACP_COMMAND` (default `claude-agent-acp`). |
| [`flows/delivery.md`](./flows/delivery.md) | How dispatches become PRs: the 3 delivery shapes (backlog / checklist / program), the dispatch cap, the Shape-3 reconcile step. | **CURRENT** — *audited 2026-07-13*: cap-recovery row now names both human verbs (`steer_goal`/`resume_goal`, #228/#238) and notes the cap block never auto-heals; the Shape-1 `pr_state` quote fixed to match `tick_settle.py` verbatim. Cap formula re-verified (algebraically identical to `tick_dispatch.py`). |

## Reference

| Doc | Purpose | Currency |
|---|---|---|
| [`reference/env-vars.md`](./reference/env-vars.md) | Single source of truth for every env var the runtime reads, grouped by purpose. | **CURRENT** — *updated 2026-07-15*: added `DEVCLAW_COGNITION=agent_sdk` (opt-in agent-sdk cognition backend, feat/cognition-sdk-backend); `DEVCLAW_COGNITION_TIMEOUT_S` (cognition-timeout headroom, default 180s replacing the hardcoded 90s cap — now also the agent_sdk inactivity budget); `DEVCLAW_TRACE_RETENTION_DAYS` (trace-retention prune); `DEVCLAW_REVIEW_PANEL_N` (opt-in diverse-lens review panel, default 1 = single reviewer). Prior audit 2026-07-13 (#228–#238 tranche; `DEVCLAW_SKILL_LIBRARY` removed). Doc↔code parity enforced by `tests/test_env_vars_doc_sync.py` (bidirectional). *Updated 2026-07-17*: added `DEVCLAW_GOAL_BROWSER_GATE` + `DEVCLAW_GOAL_BROWSER_GATE_MODE` (the browser-E2E gate on/off + flexible/strict). *Updated 2026-07-18*: added `DEVCLAW_GOAL_BROWSER_REACHABILITY` (reasoned, grounded, fail-closed escape valve — clears the browser gate only when an independent judge proves the changed UI isn't rendered in the running app); added `DEVCLAW_EVENTS_RETENTION_DAYS` (events-table retention prune, volume hygiene — the `events` log now bounded like `traces`); added `DEVCLAW_DB_SIZE_ALERT_MB` (loud-not-silent DB-size owner alarm, default 2000MB — pairs with the new weekly VACUUM). *Updated 2026-07-18*: added `DEVCLAW_SELF_TRIAGE` (propose-only self-triage interceptor, default on — an eligible owner ping is routed through a bounded triage cognition step that dedupes against the `problems` catalog + drafts a proposed fix; slice-1 allowlist = the DB-size alarm; `tests/test_self_triage.py`). *Updated 2026-07-18*: added `DEVCLAW_ITEM_MAX_ATTEMPTS` (structural per-checklist-item circuit breaker, default 3 — after N straight failed settles the item flips to `blocked` and the goal is parked `needs_human`, replacing the planner-authored "CIRCUIT BREAKER" prose; `tests/test_goal_tick_checklist.py`). *Updated 2026-07-18*: added `DEVCLAW_REVIEW_DEGRADE` + `DEVCLAW_REVIEW_DEGRADE_MAX_FILES` (the review gate's cognition-timeout degradation ladder — on a whole-diff timeout, split per file, review each, union the verdicts; fail-closed preserved, exhaustion re-raises to the same #186 crash path; `tests/test_review_panel.py`). *Updated 2026-07-19*: added `DEVCLAW_ACP_COMMAND` (the worker-agent replaceability seam — swap the in-sandbox ACP agent command without a code change; payload-threaded like `model`, shlex-split by the runner, default `claude-agent-acp`; `tests/test_acp_command_config.py`). |

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
| [`decisions/0003-goal-program-unification.md`](./decisions/0003-goal-program-unification.md) | Goal ↔ program unification: ONE `create_goal`, one dial (re-evaluation cadence — one-shot vs long-lived), one planning spine (grill→firm→decompose), shippable-release iterations, two-level sizing rule, fog-of-war scope map, checkpoint grill + auto-continue, hard cost backstops. Staged migration plan (PR-B → surface collapse → iterative loop). | **DECISION RECORD** — accepted 2026-07-19. Design locked; implementation staged — the "today" descriptions inside describe the pre-migration system and will drift as stages land. |

## Where the docs are NOT

- **The agent harness contract** — [`../CLAUDE.md`](../CLAUDE.md): the distilled
  working contract an agent reads before touching the repo. It deliberately
  duplicates the invariants in condensed form; `architecture.md` is the canonical
  statement.
- **The product narrative** — [`../README.md`](../README.md).
- **Worker-layer skills** — `.agent/skills/` (product, not harness docs).
- **Generated views** (`STATUS.md`/`log.md`/`deliveries.md` under goal dirs) —
  projections, never hand-edited, never audited here.
