# Proposal — Continuous evaluation as a projection of the live event stream

- **Status:** **GRADUATED → [ADR 0006](../decisions/0006-continuous-eval-projection.md)**
  — 2026-07-21, the same evening the draft landed: clarify step (all seven
  `[OPEN]` items answered by Denys, resolutions in §5), lock, tranche
  scheduled, graduation. The ADR is canonical from here on; this doc keeps
  the full narrative + the clarify-step trail.
- **Date opened:** 2026-07-21 · **Locked:** 2026-07-21 · **Graduated:** 2026-07-21
- **Authors:** Denys + Claude (conversation of 2026-07-21)
- **Supersedes / relates:** amends the *shape* of [ADR 0004](../decisions/0004-eval-workbench.md)
  step 2 (the `eval_runs` table / console "Evals" tab) — see §4; absorbs the
  "nightly self-report" idea from the same conversation; touches the parked
  event-driven-loop seed only at a seam (§7). ADR 0004's phases and its 2×2
  shedding rule are NOT reopened.

> How to read this: **[CONFIRMED]** = agreed in conversation on 2026-07-21.
> Sections once marked **[OPEN]** have all been resolved in place (§5) —
> the mandatory clarify step (`.claude/rules/spec-lifecycle.md`) ran the
> same evening the draft landed.

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

## 5. Clarify-step resolutions [all RESOLVED 2026-07-21, by Denys]

- **[RESOLVED] O1 — Clean-night boundary.** Wedge = any `mechanical:*`
  block, cognition-timeout-treated-as-terminal, and engine/gate crash
  classes. Clean = a `needs_answer` with a genuine question (human-gated is
  the design), and **a quota/auth pause that self-heals inside the window**
  — the pause-and-resume machinery working unattended IS the mechanism
  working. The report still lists self-healed pauses ("paused 32min,
  self-resumed") so lost throughput stays visible without failing the night.
- **[RESOLVED] O2 — Projection write path: settle-hook in the TaskQueue.**
  Rows materialize the moment a task settles — the same writer that already
  owns task rows, exactly-once, real-time, single-writer invariant intact.
  The TaskQueue owns the projection table. Basket runs keep writing their
  report JSONs; a small ingest verb (CLI/MCP) loads them as `source=basket`
  rows, since basket runs use their own measure DB, not the live one.
- **[RESOLVED] O3 — Report trigger + channel: the scheduled-edge owner →
  the existing owner-notify channel.** Today the scheduled-edge owner is the
  heartbeat: a mechanical "window just closed" check fires the report
  through the same notifier that carries owner pings. Zero LLM. No notifier
  configured → log-only, never an error.
- **[RESOLVED] O4 — Basket cadence: automatic 1-ticket smoke after each
  devclaw-mcp deploy; full baskets stay manual.** Deploys are empirically
  when things break (2026-07-21: a deploy seam killed a gate run; the
  deploy-script rot would have killed all future deploys). ~1 ticket of
  quota per deploy buys same-day detection; 9-ticket baskets remain
  deliberate, Denys-fired, around tranches and A/Bs.
- **[RESOLVED] O5 — judge_rate is basket-only for now.** Fixed tickets give
  comparable scores; live PRs already pass the review gate, and a second
  LLM pass per live PR is standing quota spend with unknown headroom.
  Revisit after ADR 0004 step 3 lands and a night's quota profile is known.
- **[RESOLVED] O6 — Retention: keep per-task rows indefinitely.** Dozens of
  rows a night is nothing for SQLite; the long tail is what trend charts
  (and the portfolio narrative) need. StateStore's existing size
  checks/VACUUM are the escape hatch if volume ever surprises.
- **[RESOLVED] O7 — Event-driven seam: abstract trigger, heartbeat today.**
  The spec names "the scheduled-edge owner" as the report trigger; the
  heartbeat holds that job now, and the event-driven rework (parked, vault:
  `event-driven-loop-idea-2026-07-20.md`) inherits it unchanged if/when it
  lands. Nothing in this proposal blocks on, or preempts, that idea.

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
