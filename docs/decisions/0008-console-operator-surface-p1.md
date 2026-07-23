# ADR 0008 — Console operator surface, P1: the read hierarchy + layer trace

- **Status:** accepted 2026-07-23 (Denys). P1 tranche scheduled the same day —
  graduated from [`../proposals/console-operator-surface.md`](../proposals/console-operator-surface.md)
  under the spec lifecycle. This record freezes the *P1 decision and rationale*;
  the proposal keeps the full vision, the eight clarify-step resolutions, and the
  still-named-but-unsized P2/P3 slices (incl. §6 structured decision blocks).
- **Scope of this ADR:** the **P1 slice only.** P2 (health + problem-lifecycle
  dashboard) and P3 (co-pilot chat + mutation reach + §6) remain LOCKED-direction
  in the proposal, unsized, and are **not** decided here.
- **Layer:** this is a **layer-1 (MCP surface / dashboard)** change — read routes +
  a React frontend. It touches no goal-layer, queue, or engine invariant.

## Context

The console today is a **status board** — flat lists and tabs. devclaw's
architecture has a *shape* (one append-only event log, five layers, a heartbeat,
goals that own programs that own tasks), and the operator mental model is that the
console should have **that** shape: the whole running node visible from the top,
drilled down through a logical hierarchy. A prior console idea already died by
drift (the operator-UX gap); this ADR exists so P1 is a durable build target, not
a conversation that evaporates.

P1 is a **packaging / legibility** move (the CV/portfolio scoreboard's stated
priority), explicitly **not** a reliability change and **not** a new cognition path
on any tick — it is read surfaces over projections that already exist. The
zero-token idle guard and the single-writer invariant are untouched (P1 adds no
writes).

## Decision

Build a read-only operator surface with one drill-down spine and one cross-cutting
trace, over existing projections:

```
NODE (the running instance)
  → PROJECT (→ GitHub)
    → GOAL (lifecycle + direction)
      → MILESTONE  (= plan_key grouping — a view, NOT a new entity)
        → TASK
```

The **same disclosure rule at every tier: show active + planned in full; fold the
settled into a summary you can open.** Plus **the layer trace** — one tick/task
rendered hop-by-hop through the 5 layers, a failure highlighted in place.

Locked resolutions carried from the proposal's clarify step (§5):

- **Milestone = `plan_key` grouping** (§5.1). A view over tasks grouped by their
  existing `plan_key`; no new table, no persisted milestone entity.
- **P1 boundary = read hierarchy + layer trace; health charts are P2** (§5.2).
- **P1 is read/navigate-only** (§5.4). The intent API is `open`/`filter`/`select`
  only; browser mutation (create/steer/cancel/answer) and its auth story are P3.
- **Not one mega-page** (§5.8). Proper screens/routes; the reference mockup is a
  demo of the whole vision, not the target IA.

### What already exists (reuse) vs. net-new (build)

Grounded in the code as of 2026-07-23 (`devclaw/server/console/` + `server/http.py`):

- **Reuse as-is:** the GOAL tier — `console/src/pages/GoalDetail.tsx` (tabbed) over
  `GET /goals/{id}.json`. Rich already; becomes the spine's GOAL node.
- **Half-there:** PROJECT (`Projects.tsx` / `ProjectDetail.tsx` over
  `/projects.json` + `/projects/{id}.json`) and TASK (`TasksSection.tsx`, flat
  table) — need the spine + the shared disclosure primitive, not a rewrite.
- **Net-new backend (small):** a `GET /node.json` vitals summary (assembles
  heartbeat/dispatch from `/control.json`, goal-population counts, clean/wedge from
  `cycle_reports`); and surfacing `plan_key`/`milestone` in `_task_row`
  (`http.py`) — the fields are on the rows (`state_store/rows.py`), just not
  emitted.
- **Net-new frontend:** the NODE view; the milestone(`plan_key`) grouping under a
  goal; and the **layer-trace view** — its backend read is already exposed
  (`GET /traces.json` + the `get_trace` grouping by `trace_id`); this is a pure
  frontend build. Layer is inferred from trace `kind` (no payload re-tagging in
  P1).

### P1 slicing (the firmed sizing — end-of-week cap, ≈6 PRs)

Each PR is independently shippable and independently testable. Only P1 is sized;
P2/P3 stay unsized until P1 lands.

1. **PR-A — wire shape + reusable disclosure primitive.** Surface
   `plan_key`/`milestone` in `_task_row`; add a shared `<TieredDisclosure>` (active
   shown, settled folded-openable) and adopt it in the existing Project/Goal
   screens. Foundation for the spine.
2. **PR-B — `GET /node.json` + NODE view.** The vitals summary endpoint + the top
   NODE screen (heartbeat/dispatch, 5-organ health placeholder from what's
   available, goal-population, clean/wedge).
3. **PR-C — the drill-down spine.** Wire NODE → PROJECT → GOAL as one navigable
   hierarchy with the disclosure primitive at each tier (reusing GoalDetail as the
   GOAL node).
4. **PR-D — MILESTONE (plan_key) grouping + TASK.** Group a goal's tasks by
   `plan_key` under the GOAL node; task drill-in.
5. **PR-E — the layer-trace view.** Frontend over `/traces.json`; render one
   `trace_id` hop-by-hop across the 5 layers, error highlighted in place.
6. **PR-F — screen decomposition + polish.** Ensure proper routes/screens (not one
   page, §5.8); overview vs goal-detail vs trace; responsive/theme pass.

Sizing is a cap, not a contract — PRs may split or merge as the build reveals the
work; the *boundary* (read hierarchy + trace, read-only) is what's locked.

## Consequences

- **The operator surface becomes real and legible** — the strongest single
  demo/portfolio artifact the system can produce, and the substrate P2/P3 render
  onto.
- **No invariant moved.** Read-only, no tick-path cognition, no new writer. If any
  P1 PR is tempted to add a write or a per-tick LLM call, it is out of P1 scope by
  this ADR.
- **Honesty carried forward:** where a stat is estimated (tokens) it is labelled;
  the NODE "5-organ health" starts from what's actually derivable and does not
  fabricate a signal that doesn't exist yet (a real per-layer health rollup is a
  later, separate build).

## Deferred (named, not this tranche)

- **P2** — health / self-evaluation charts + the problem-lifecycle tracker.
- **P3** — the co-pilot chat (over the existing waiter/OAuth, §4), the intent API's
  mutation reach + console auth story, and **§6 structured decision blocks**
  (needs_answer block → options + recommendation + custom-answer, click →
  `steer_goal`).
- A real **per-layer health rollup** endpoint (the NODE view uses derivable
  signals in P1).
