# ADR 0006 — Continuous evaluation as a projection of the live event stream

- **Status:** accepted 2026-07-21 (Denys). Tranche scheduled the same evening —
  graduated from [`../proposals/continuous-eval-projection.md`](../proposals/continuous-eval-projection.md)
  under the spec lifecycle. This record freezes the *decision and rationale*;
  system snapshots inside reflect their writing date.
- **Amends:** [ADR 0004](./0004-eval-workbench.md) step 2 only — the workbench
  table/tab is reshaped (two-source outcome projection instead of a
  basket-only `eval_runs`). ADR 0004's phases, its 2×2 shedding rule, and
  everything else stand unchanged.
- **Relates:** the parked event-driven-loop idea (vault seed 2026-07-20) —
  deliberately not blocked on and not preempted (see "Trigger abstraction").

## Context

Two forces converged on 2026-07-21:

1. **The daily manual ritual.** Every morning the operator pulled state (SSH,
   digest, a Claude session) and pushed the system toward working. devclaw
   already classified failures (`blocked_kind`), kept a problems catalog,
   recorded gate verdicts with reasons, and paused-and-resumed on quota/auth
   — then waited to be asked. The observability layer did the evaluating
   (the 2026-07-20 baseline's .NET-9 root cause was diagnosed from *stored
   verdict texts*); the reporting was a human.
2. **The separate-process seam factory.** Evaluation ran as a second devclaw
   process (`evals/measure_passrate.py`), and the same morning delivered the
   proof-by-incident: a service redeploy mid-eval SIGKILLed the eval's
   in-flight sandboxes via the startup orphan sweep (exit 137, a morning of
   diagnosis; instance-fixed by owner-scoping the sweep, #312). Every
   capability the eval harness duplicates outside the main loop is a
   mechanism×mechanism collision waiting to happen.

Denys's direction, distilled: error handling, observability, monitoring, and
evaluation are **one integrated flow** — upgrade the flow, don't run something
separate.

## Decision

**Evaluation is a projection over the event stream devclaw already writes** —
the same architectural move the state layer made ("append-only event log,
views are projections") applied to metrics.

1. **One outcome projection, two sources.** Every live task settle writes an
   `eval_outcomes` row (gate verdict, retries, wedge class, PR, wall time —
   zero extra tokens) at the settle commit, by the same single writer that
   owns task rows. Basket runs land in the same table as `source=basket` via
   an ingest verb. This replaces ADR 0004 step 2's basket-only `eval_runs`.
2. **Clean-night rate is the second headline metric** beside `pass_rate`,
   operationalizing the operator's real done-criterion ("kick off a goal for
   the night and it runs without me"). A night is clean iff zero
   mechanism-wedges. Boundary (locked): wedge = `mechanical:*` blocks,
   cognition-timeout-treated-as-terminal, engine/gate crash classes; clean =
   a genuine `needs_answer` (human-gated is the design) and a **self-healed
   quota/auth pause** (the pause machinery working unattended IS the
   mechanism working — listed in the report, never failing the night).
3. **A mechanical window-close push report.** When the nightly run window
   closes, the *scheduled-edge owner* (today: the heartbeat) assembles the
   night's slice — clean?, wedge list with classes, self-healed pauses, what
   needs the operator — and pushes it through the existing notifier. Zero
   LLM. No notifier configured → log-only, never an error.
4. **`measure_passrate` demotes to the experiment tool.** Field telemetry is
   primary; the basket answers only what live traffic cannot (controlled A/B
   on fixed tickets, per ADR 0004's 2×2). Cadence: an automatic 1-ticket
   smoke after each devclaw-mcp deploy (deploys are empirically when
   regressions land); full baskets stay deliberate and operator-fired.
5. **judge_rate stays basket-only for now** (comparable scores need fixed
   tickets; live-PR judging is standing quota spend with unknown headroom —
   revisit after ADR 0004 step 3 lands).
6. **Per-task rows are kept indefinitely** (SQLite-trivial volume; the long
   tail feeds trend charts and the portfolio narrative; StateStore's size
   checks/VACUUM are the escape hatch).

### Trigger abstraction

The report trigger is specified as "the scheduled-edge owner", not "the
heartbeat": today the heartbeat holds the job; if the parked event-driven
rework ever demotes the heartbeat to reconciliation, it inherits the edge
unchanged. Nothing in this decision blocks on, or preempts, that idea.

### Explicitly rejected

- Heartbeat-embedded LLM evaluation and per-failure LLM autopsies — the
  zero-token idle guard is load-bearing, and mechanical classification has
  carried every root-cause diagnosis so far. Revisit only on demonstrated
  insufficiency.
- Merging `measure_passrate` into the service process. The exam does not
  live inside the student; cohabitation is instead made safe (owner-scoped
  sweep) and rare (smoke + deliberate baskets).

## Invariants

Zero-token idle guard: untouched — every addition is arithmetic over
existing rows, placed after the cheap idle gates. Single writer: the
projection insert shares the settle commit inside the store; `night_reports`
is written from layer-2 heartbeat code through the store. Quota: improved —
live telemetry is free; basket spend becomes rarer and deliberate.

## Consequences

- The workbench tranche (reshaped ADR 0004 step 2) becomes: `eval_outcomes`
  + settle-hook + ingest/backfill; `night_reports` + window-close report;
  console Evals tab; deploy-smoke automation. Each lands independently.
- The morning ritual inverts: the system reports to the operator; the
  operator answers questions and reads trends.
- The full clarify-step Q&A (seven resolutions, same-evening) lives in the
  graduated proposal; this ADR is the distillation.
