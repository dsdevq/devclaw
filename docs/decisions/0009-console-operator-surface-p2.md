# ADR 0009 — Console operator surface, P2: health enrichment + problem-lifecycle tracker

- **Status:** accepted 2026-07-23 (Denys — "continue until finished"). P2 tranche
  scheduled the same day, graduated from
  [`../proposals/console-operator-surface.md`](../proposals/console-operator-surface.md)
  after P1 ([ADR 0008](./0008-console-operator-surface-p1.md)) shipped. Freezes the
  P2 decision + sizing; P3 (co-pilot chat + §6 structured blocks + mutation reach)
  stays LOCKED-direction in the proposal, unsized.
- **Scope:** the P2 slice only. **Layer 1 (dashboard), read-only** — no writes, no
  tick-path cognition; the zero-token idle guard and single-writer invariants are
  untouched.

## Context

The console P1 (ADR 0008) shipped the read hierarchy + layer trace. The proposal's
P2 is the **health / self-evaluation dashboard** + the **problem-lifecycle
tracker**. Grounding against what already exists on `main`:

- The **Evals tab** (`console/src/pages/Evals.tsx`, from the continuous-eval
  tranche) already delivers a real chunk of "health": pass-rate, clean-cycle rate,
  the settled-outcomes table (with `failure_class`), and per-cycle reports over
  `/evals/outcomes.json` + `/evals/cycles.json`. P2 **enriches** this surface; it
  does not rebuild it.
- The **problem-lifecycle data exists**: the `problems` table carries
  `issue_number` + `issue_state` (`open`/`closed`) from self-issue-filing Stage 1
  (ADR 0006 / `proposals/self-issue-filing.md`). So a problem's lifecycle —
  *identified → filed → open → resolved* — is fully derivable. What's missing is a
  console read surface: there is **no `/problems.json` HTTP route** (only the
  `list_problems` MCP tool) and no problem-lifecycle view.

## Decision

Two additions, both read-only over projections that already exist:

1. **Problem-lifecycle tracker (headline, net-new).** A `GET /problems.json` route
   over the deduplicated `problems` catalog (surfacing `issue_number` /
   `issue_state` alongside the existing count / category / recovered / terminal /
   sample fields), plus a **Problems** console screen that renders each problem's
   lifecycle stage:
   - **identified** — in the catalog (count, first/last seen, category, sample).
   - **filed** — `issue_number` set → link to the GitHub issue.
   - **open / being worked** — `issue_state == "open"`.
   - **resolved** — `issue_state == "closed"` (or aged out of recurrence).
2. **Health enrichment (on the existing Evals surface).** An **error-class
   breakdown** (counts by `failure_class` — which failures dominate) and an
   **estimated token-spend** figure (from `trace_totals`' real+estimated token /
   cost aggregation).

### `[OPEN]` resolutions carried from the proposal's clarify step

- **§5.5 self-resolution honesty — RESOLVED (honest default).** The
  problem-lifecycle UI marks the **fix stage as gated / propose-only**. Filing is
  live (Stage 1); *fixing* is propose-only, human-merges (Stage 2) — the UI shows
  identified → filed → open → resolved and **never implies full auto-fix**. A
  "fixing" state is labelled as gated, not autonomous.
- **§5.6 token honesty — RESOLVED (honest default).** Token spend is labelled
  **estimated** (the traces cost estimate); where real usage was recorded it is
  marked real. Every token figure carries the estimated-vs-real label; time and
  verdict stay real.

### P2 sizing (end-of-week cap, ≈2 PRs)

1. **P2-A — problem-lifecycle tracker.** `GET /problems.json` (backend, surfacing
   issue fields) + a **Problems** screen/nav rendering each problem's lifecycle
   with the §5.5 honesty labelling + issue links. Named regression test on the
   route's wire shape.
2. **P2-B — health enrichment.** Error-class (`failure_class`) breakdown +
   estimated token-spend on the Evals surface, with the §5.6 estimated-vs-real
   labelling.

Sizing is a cap, not a contract; the *boundary* (these two read surfaces,
read-only, over existing projections) is what's locked.

## Consequences

- The self-improving cycle becomes **visible**: an operator can watch a recurring
  failure go identified → filed (issue) → resolved, which is the strongest
  legibility artifact for the self-issue-filing work (ADR 0006).
- **No invariant moved.** Read-only, no tick-path cognition, no new writer. A
  `/problems.json` route is a pure SELECT over `problems` (the same class as
  `/node.json` and `/evals/*.json`).
- **Honesty carried forward:** the auto-fix stage is shown gated (§5.5), token
  spend labelled estimated (§5.6) — the UI never claims autonomy or precision it
  doesn't have.

## Deferred (named, not this tranche)

- **P3** — the co-pilot chat (over the existing waiter/OAuth, §4), the intent
  API's mutation reach + console auth story, and **§6 structured decision blocks**.
- Per-project / per-milestone health *grouping* and true time-series *charts*
  (the P2 health enrichment is breakdown + totals; richer charting can follow if
  the numbers warrant it).
- **Node-wide estimated token spend — DEFERRED (amended 2026-07-23, P2-B).** The
  ADR named token spend as part of the health enrichment, but on build it proved
  ungrounded to ship cheaply/honestly: `eval_outcomes` carries no token/cost
  columns, and the only token data lives per-goal in `traces` (`trace_totals`) —
  a node-wide rollup means an expensive `json_extract` scan over the 200k-row
  traces table on every poll. Per-goal token usage is **already surfaced** (the
  GoalDetail "Usage" badge), so P2-B shipped the **error-class breakdown** (the
  high-value reliability-legibility half) and deferred the node-wide token figure
  rather than fabricate one or add fragile plumbing blind. Revisit with a bounded
  aggregation if a node-wide total is actually wanted.
