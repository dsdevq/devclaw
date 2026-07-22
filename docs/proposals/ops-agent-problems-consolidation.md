# Proposal — Ops-agent ⇄ continuous-eval: one problem record, demarcated roles

- **Status:** **DRAFT** — written 2026-07-22, right after the ADR 0006
  cycle-report shipped and Denys asked whether the ops-agent overlaps it.
  Direction NOT locked; the `[OPEN]` clarify step (§4) is mandatory before LOCKED.
- **Date opened:** 2026-07-22 · **Authors:** Denys + Claude
- **Relates to:** [ADR 0006](../decisions/0006-continuous-eval-projection.md)
  (the cycle report + `eval_outcomes`/`problems` read path) and
  [ADR 0004](../decisions/0004-eval-workbench.md) (the workbench). Does NOT
  reopen either; it consolidates a record that now spans both plus an external
  agent.

## 1. Context — two things that look like one

The **ops-agent** is a *separate external container* (`ops-agent:local`, not one
of devclaw's five layers) that polls devclaw every 60s and runs four watchdog
detectors:

- **O1** no-progress — a goal's no-progress watchdog escalated to owner
- **O2** no-steering — a blocked goal is sitting unanswered / unsteered
- **O3** verifying-stall — stuck in `verifying` beyond `OPS_AGENT_VERIFYING_STALL_HOURS` (4h)
- **O4** trend-signal-repeat — the same trend signal repeats ≥ `OPS_AGENT_TREND_REPEAT_THRESHOLD` (3) consecutive days

Per incident it runs an **LLM (sonnet) decision** — `evaluate_goal` (force a fresh
direction eval to try to *unstick* the goal) or `noop` (record only) — can **act
on devclaw via MCP**, can **restart the `devclaw-mcp` container** (docker.sock +
a one-name allowlist), and **writes a structured incident** (`trigger.json`,
`prompt.md`, `decision.json`, `outcome.md`, running `log.md`) into the vault at
`/srv/memory/projects/ops-agent`. It does **not** notify the owner.

The **cycle report** (ADR 0006) is the opposite: mechanical, **zero-LLM**,
read-only, fires once per run-cycle close, assembles the clean/wedge/pause slice
from `eval_outcomes` + the `problems` catalog, and **pushes it to the owner** via
the notifier.

## 2. Finding — complementary, with one real redundancy

They are **different legs of one pipeline**, not duplicates:

- **Ops-agent = the hands.** Real-time, LLM-driven, *acts* (unstick / restart),
  records to the vault. The on-call SRE.
- **Cycle report = the voice.** Batch, mechanical, *reports* to the owner. Never acts.
- **No double-ping** — the ops-agent doesn't message the owner, so the cycle
  report is the only push. This is consistent with ADR 0006 by design: that ADR
  rejected per-failure LLM autopsy *inside* devclaw precisely because the
  ops-agent already is that external LLM-triage layer.

The genuine redundancy is **not reporting** — it is **detection substrate and
record fragmentation**:

1. **Two notions of "wedged/stuck", from different sources.** Ops-agent detects
   via goal-state watchdogs (O1–O4); the cycle report classifies wedges from
   `eval_outcomes` + `problems`. They can disagree about whether a cycle was clean.
2. **Problem records live in three stores** — devclaw's internal `problems`
   catalog, the ops-agent's vault incident dirs, and `eval_outcomes`.
3. **Ops-agent interventions are invisible to the report.** A cycle propped up by
   several `evaluate_goal` unsticks can still read "clean" because those actions
   live in a store the mechanical report never reads.

## 3. Proposed direction — one record, demarcated roles

Keep both agents; **consolidate the record and draw the line**:

- **Ops-agent stays the detect → decide → act layer.** Real-time watchdogs,
  LLM decision, remediation (`evaluate_goal` / mcp restart). Unchanged in purpose.
- **Cycle report stays the report layer.** Mechanical, zero-LLM, once per cycle.
- **The `problems` catalog becomes the single source of truth.** Every ops-agent
  incident (and its decision/outcome) is recorded into devclaw's `problems`
  catalog **through an MCP tool** — so devclaw remains the single writer to its
  own DB (the ops-agent is external and must never write `devclaw.db` directly).
  The vault incident dirs become a secondary human-readable mirror, or are retired.
- **Consequence:** the cycle report's wedge / needs-operator section then reflects
  ops-agent interventions automatically, from one store. Clean layering:
  *ops-agent detects + acts + records-to-`problems` → cycle report reads `problems`
  and reports.* Three stores collapse toward one.

This is the [ADR 0004](../decisions/0004-eval-workbench.md)/0006 observability
plane finishing its own thesis (eval = a projection over one event stream) by
pulling the last out-of-band record — the ops-agent's — into it.

## 4. `[OPEN]` — clarify step (mandatory before LOCKED)

- **[OPEN] O1 — MCP write path.** Is there (or should there be) an MCP tool for
  the ops-agent to record a problem into the `problems` catalog, preserving the
  single-writer invariant? `record_problem` exists internally
  (`StateStore.record_problem`, fingerprint UPSERT) but is not exposed as a tool.
  Owner: design. Default lean: add a narrow `record_incident`/`record_problem`
  MCP tool rather than a DB side-channel.
- **[OPEN] O2 — Does a clean cycle require zero ops-agent *actions*, or only zero
  wedges?** If the ops-agent had to `evaluate_goal` a goal 4× to keep it alive, is
  that cycle clean? Options: (a) actions are surfaced-but-clean like self-healed
  pauses; (b) an ops-agent remediation counts as a soft-wedge. Ties directly to
  ADR 0006's clean-cycle boundary (§5-O1) — this proposal must NOT silently
  redefine it.
- **[OPEN] O3 — Detection dedup.** With incidents in `problems`, do O1–O4 keep
  their own 24h dedup, or defer to the catalog's fingerprint UPSERT? Avoid two
  dedup windows fighting.
- **[OPEN] O4 — Vault incident dirs: mirror or retire?** Keep them as a
  human-browsable audit trail, or drop them once `problems` is canonical? (Denys
  reads the vault; the console `list_problems` surface may already cover it.)
- **[OPEN] O5 — Is the ops-agent's *action* leg earning its keep?** Recent logs are
  mostly `noop`. Worth measuring O1–O4 act-vs-noop rates before investing; a
  rarely-acting ops-agent might collapse to a detector that just writes `problems`,
  with remediation folded into devclaw's own auto-heal. Owner: measurement first.
- **[OPEN] O6 — Portability.** The ops-agent is honestly devclaw-specific in its
  detectors but generic in shape ([[ops-agent-should-generalize]]). Does routing
  through devclaw's MCP tighten the coupling in a way that matters, or is MCP
  exactly the right seam? Default lean: MCP is the right seam.

## 5. Invariants — referenced, not restated

- **Single writer to state.** The load-bearing constraint here: the external
  ops-agent must not write `devclaw.db`. All record consolidation goes through an
  MCP tool so the TaskQueue/StateStore stays the only writer (see `CLAUDE.md`).
- **Zero-token idle guard.** Untouched — the cycle report stays mechanical; nothing
  in this proposal adds an LLM call to a devclaw tick path. The ops-agent's LLM
  use is external and already exists.
- **Clean-cycle boundary (ADR 0006 §5-O1).** `[OPEN] O2` may adjust what counts as
  clean; if it does, that is a headline change to ADR 0006, not a footnote.
