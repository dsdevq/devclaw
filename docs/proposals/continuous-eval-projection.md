# Proposal — Continuous evaluation as a projection of the live event stream

- **Status:** **DRAFT**
- **Date opened:** 2026-07-21
- **Authors:** Denys + Claude (conversation of 2026-07-21)
- **Supersedes / relates:** amends the *shape* of [ADR 0004](../decisions/0004-eval-workbench.md)
  step 2 (the `eval_runs` table / console "Evals" tab) — see §4; absorbs the
  "nightly self-report" idea from the same conversation; touches the parked
  event-driven-loop seed only at a seam (§7). ADR 0004's phases and its 2×2
  shedding rule are NOT reopened.

> How to read this: **[CONFIRMED]** = agreed in conversation on 2026-07-21.
> **[OPEN]** = must be answered (or explicitly deferred with an owner) before
> this can flip to LOCKED. The clarify step is mandatory — see
> `.claude/rules/spec-lifecycle.md`.

---

## 1. The problem [CONFIRMED]

Denys's daily loop is still manual: every morning he pulls state (SSH, digest,
a Claude session) and pushes the system toward working — "every day, I push
you towards making devclaw work. I'm tired." Meanwhile evaluation runs as a
**separate process** (`evals/measure_passrate.py` fired by hand), which has two
costs:

1. **Nobody tells him anything.** devclaw already classifies failures
   (`blocked_kind`), keeps a problems catalog, records gate verdicts with
   reasons, pauses-and-resumes on quota/auth — and then waits to be asked.
   The observability layer did the evaluating on 2026-07-20/21 (the .NET-9
   root cause was diagnosed from *stored verdict texts*, not from the basket
   mechanics) — but the *reporting* is a human ritual.
2. **A second devclaw process is a seam factory.** The 2026-07-21 exhibit: a
   service redeploy mid-eval SIGKILLed the eval's in-flight sandboxes via the
   startup orphan sweep (exit 137, two attempts burned, a morning of
   diagnosis). Fixed for that instance (owner-scoped sweep, PR #312), but the
   class remains: every capability the eval harness duplicates outside the
   main loop is a mechanism×mechanism collision waiting to happen.

Denys's direction (his words, distilled): error handling, observability,
monitoring, and evaluation should be **one integrated flow** — "upgrade and
improve the flow, not run something separate."

## 2. The direction [CONFIRMED]

**Evaluation is a projection over the event stream devclaw already writes.**
This is the same architectural move the state layer already made ("append-only
event log; views are projections") applied to metrics:

- Every **live** task settle is an evaluation sample for free: gate verdict +
  reasons, retry count, wedge class, PR URL, wall time, zero extra tokens.
- The **basket** (`measure_passrate`) demotes from "the evaluation" to "the
  experiment tool" — reserved for questions live traffic cannot answer
  (controlled A/B on fixed tickets: guardrail on/off, model column vs model
  column, per ADR 0004's 2×2). Field telemetry primary; lab runs occasional.
- Both write into **one outcome projection** distinguished by a `source`
  column (`live | basket`), replacing ADR 0004 step 2's basket-only
  `eval_runs` shape. One schema, one console tab, two sources.

Three deliverables ride this:

1. **The outcome projection** — per-settled-task rows + derived rates
   (pass rate, first-attempt rate, wedge rate by class) computed from events
   already in `devclaw.db`. Backfill: June runs + the July baseline JSONs.
2. **Clean-night rate** — the second headline metric beside `pass_rate`,
   operationalizing Denys's stated done-criterion ("kick off a goal for the
   night and it runs without me"): a night window is *clean* iff zero
   mechanism-wedges (definition in §5-O1).
3. **The window-close push report** — at the end of each night window devclaw
   assembles the night's projection slice (clean? wedge list + classes? what
   needs Denys?) and *pushes* it through the existing notifier. Mechanical
   counts only — **zero LLM calls** (§3).

## 3. Invariants touched [CONFIRMED]

References, not restatements (CLAUDE.md / `docs/architecture.md`):

- **Zero-token idle guard — unchanged, by construction.** The projection and
  the report are arithmetic over existing rows; no cognition call is added to
  any tick path. Explicitly rejected in conversation: heartbeat-embedded LLM
  evaluation, and an automatic LLM "autopsy" per failure (mechanical
  classification carried the last two root-cause diagnoses; revisit only when
  it demonstrably falls short).
- **Single writer to state — respected; exact write path is §5-O2.** The
  projection is a *view* in the architectural sense; whoever materializes it
  must not create a second writer to task/goal rows.
- **Quota (one shared OAuth pool) — improved.** Live telemetry costs zero
  tokens; basket runs become rarer, deliberate, and scheduled (§5-O4).

## 4. What this changes in ADR 0004 [CONFIRMED]

Step 2's artifact ("`eval_runs` table + console Evals tab; backfill June +
baseline") is *reshaped*, not dropped: the table becomes the two-source
outcome projection above, and the tab grows the clean-night headline. Steps
1/3/4/5 and the shedding rule (never delete a guardrail on the Claude column
alone) are untouched. If this proposal locks, the amendment is recorded in
ADR 0004's header, pointing here.

## 5. Open items — the clarify step [OPEN]

- **[OPEN] O1 — Clean-night definition.** Which `blocked_kind` classes count
  as mechanism-wedges? Working proposal: any `mechanical:*` block, any
  cognition-timeout-terminal, any engine/gate crash class = wedge; a
  `needs_answer` with a genuine question = clean (human-gated is the design,
  not a failure); a quota/auth pause that self-heals inside the window =
  clean but reported. Denys to confirm the boundary, especially the pause
  case.
- **[OPEN] O2 — Projection write path.** Settle-hook in the TaskQueue (rows
  materialize at settle time, same transaction boundary) vs. a cheap
  periodic projector reading the event log (no queue change, eventually
  consistent)? Both keep the single-writer contract; pick one and name the
  owner of the new table.
- **[OPEN] O3 — Report trigger + channel.** How is "window close" detected
  (heartbeat sees the edge? a scheduled check?), which notifier channel gets
  it, and what happens when no notifier is configured (log-only?).
- **[OPEN] O4 — Basket cadence.** Post-deploy smoke only, weekly, or
  manual-only-for-now? Each basket run spends real quota from the shared
  pool; the VPS cron exists as mechanism but the budget is a policy call.
- **[OPEN] O5 — judge_rate on live PRs.** ADR 0004 step 3 scores basket PRs
  with the judge. Can the same judge ride *live* PRs (sampled? all?), or is
  judge_rate basket-only until quota says otherwise?
- **[OPEN] O6 — Retention/rollup.** Per-task rows forever, or roll up beyond
  N days into nightly aggregates?
- **[OPEN] O7 — Event-driven-loop seam.** The parked idea (vault:
  `event-driven-loop-idea-2026-07-20.md`) would demote the heartbeat to
  reconciliation. The window-close report must not bake in "the heartbeat is
  the trigger" in a way that fights that future — O3's answer should name
  the trigger abstractly (a scheduled edge, whoever owns edges).

## 6. Explicitly out of scope [CONFIRMED]

- No LLM calls added to tick paths; no per-failure LLM autopsy (yet).
- No new guardrails and no shedding — this measures; ADR 0004 governs acting
  on measurements.
- `measure_passrate` is not deleted, rewritten, or moved in-process; it keeps
  its own DB/workroot (the owner-scoped sweep makes cohabitation safe).
- The operator console beyond the Evals tab reshape.

## 7. Sequencing [CONFIRMED as intent, not schedule]

Locking this reshapes the *next* scheduled tranche (ADR 0004 step 2) rather
than adding a new one: projection table + backfill + tab + clean-night rate +
window-close report is one tranche of comparable size to the original step-2
plan. Sequencing stays Denys's call per the spec lifecycle.
