# Proposal — devclaw files (and later fixes) its own issues from the `problems` catalog

- **Status:** **DRAFT** — 2026-07-22, Denys's idea right after the ops-agent cut.
  Direction NOT locked; the `[OPEN]` clarify step (§5) is mandatory before LOCKED.
- **Date opened:** 2026-07-22 · **Authors:** Denys + Claude
- **Relates to:** [ADR 0006](../decisions/0006-continuous-eval-projection.md)
  (the cycle report + `problems`/`eval_outcomes` this reads from) and the ABANDONED
  `ops-agent-problems-consolidation.md` — this is the **dev-loop** half of that
  same cut (infra-ops vs dev-loop): devclaw watching *its own product quality* is
  devclaw's job, not the ops-agent's. It also rescues the dead **O4 trend-repeat**
  detector by giving recurrence-detection its correct home. Touches the parked
  [event-driven-loop seed](./) only at a seam.

## 1. Idea

Devclaw already *observes* its own failures (the `problems` catalog, the
`eval_outcomes` projection, the run-cycle report). Close the loop: turn a
**recurring** problem into a **GitHub issue on the devclaw repo**, tagged by
failure class — and, later and gated, let devclaw pick that issue up as a goal,
fix it, and run the fix through the gates it already has. The "mission-control"
pattern (watchdog files an issue → agent fixes → evaluation gates the fix),
pointed at devclaw itself.

This is almost entirely **wiring existing parts**, not new invention:
- `problems` catalog — already deduped (fingerprint UPSERT) and categorized
- `eval_outcomes.failure_class` — the mechanical taxonomy (reasoning / gate /
  engine / timeout / review-rejected / …)
- traces — the grounded repro/context per failure
- the goal loop — already `goal → fix → gate → evaluate`
- delivery — already `commit → branch → push → PR`

## 2. The shape, staged

**Stage 1 — file (safe, high-value, do first).** At run-cycle close (the same
mechanical, zero-LLM edge that assembles the cycle report), for each problem that
has **recurred past a threshold**, open/update a GitHub issue on the devclaw repo:
- **body** carries the grounded context — failure class, count, first/last seen,
  the representative trace, the goals/tasks it hit;
- **labels** map from `failure_class` / problem category (`reasoning`, `gate`,
  `engine`, `timeout`, `delivery`, …);
- **idempotent** — one issue per problem fingerprint; recurrence updates the
  existing issue (count/comment), never spawns duplicates; a closed-then-recurring
  problem may reopen.
Denys triages. This is the whole of stage 1: a mechanical `problems → gh issue`
bridge with recurrence gating and a label map. Strong portfolio artifact — *the
system files its own bugs, deduplicated, tagged, reproducible.*

**Stage 2 — fix (gated, later).** Devclaw can pick up one of its own issues as a
durable goal → fix → the review / eval / browser gates run on the change. But
the self-referential case gets a **hard brake** (see §3).

## 3. The load-bearing invariant — self-modification is PROPOSE-ONLY

A dev-loop that edits its own harness is the system modifying the machinery that
judges it: a bad self-fix could break the very gate meant to catch it. So:

> **On its own repo, devclaw may open a *draft PR* and run every gate — but a
> human merges. Never auto-merge to self.**

This is "done is a proposal" (already an invariant) extended to self-modification.
On *other* repos (finance-sentry, closeloop, …) the full auto-loop stays as-is —
only the self-referential case needs the brake. Devclaw must reliably recognize
"this is my own repo" (workspace path / repo identity), and the auto-merge path
(`delivery/merge.py`) must refuse when target == self.

## 4. Why this is the right home for recurrence (rescuing O4)

The ops-agent's O4 "trend-signal-repeat" detector never fired and was about to be
dropped. Its logic — *act only when a problem repeats across N cycles* — is
exactly the right **issue-creation trigger** here. A one-off `timeout` is noise; the
same reasoning failure three cycles running is a real, file-worthy issue. So this
proposal absorbs O4 into the dev-loop where it belongs, instead of discarding it.

## 5. `[OPEN]` — clarify step (mandatory before LOCKED)

- **[OPEN] O1 — Recurrence threshold.** What triggers an issue: seen ≥ N times,
  or across ≥ M distinct cycles, or both? Default lean: ≥ M cycles (a problem that
  survives cycles, not just a burst). Values TBD with a little live data.
- **[OPEN] O2 — Idempotency + lifecycle.** One issue per problem fingerprint.
  On recurrence: comment + bump a count label? On a closed issue that recurs:
  reopen, or file fresh with a back-link? Who closes — human only, or does devclaw
  close when the class goes quiet for K cycles?
- **[OPEN] O3 — Label taxonomy.** Fixed map from `failure_class` / problem category
  → GitHub labels. Who owns the label set (created on first use vs pre-seeded)?
- **[OPEN] O4 — Noise budget.** Cap issues opened per cycle (e.g. ≤3), and
  `log()` what was suppressed so silent truncation never reads as "all clear."
- **[OPEN] O5 — Stage-2 trigger (human-in-the-loop for *starting* a self-fix).**
  Does devclaw auto-open a fix-goal for its own issue, or only once Denys labels it
  `accepted` / assigns it? Default lean: **human opt-in** — devclaw never starts
  modifying itself unprompted; filing is automatic, fixing is invited.
- **[OPEN] O6 — Self-repo recognition + auto-merge refusal.** How does devclaw
  know a target repo is *itself*? Where does the "no auto-merge to self" check live
  (`delivery/merge.py`? a project-registry flag?)? This is the safety seam — it
  must be explicit and tested.
- **[OPEN] O7 — Where issues live + who reads them.** devclaw repo Issues, a
  project board, or also surfaced in the console? Should the cycle-report push
  link the issues it filed ("filed #123, #124")?
- **[OPEN] O8 — Egress / auth.** Issue creation is a `gh`/GitHub-API call from the
  service (not the sandbox). Confirm the token scope + that this stays OAuth-clean
  (no `ANTHROPIC_*` involved; this is a GitHub credential, separate concern).

## 6. Invariants — referenced, not restated

- **Zero-token idle guard.** Stage-1 filing is mechanical (recurrence math + a
  GitHub API call) on the cycle-close edge — no LLM on any tick path, same shape
  as the cycle report. Untouched.
- **Single writer to state.** Issue-filing reads the `problems` catalog; it does
  not add a second writer. If a "filed issue #" is recorded back, it goes through
  the store's single writer.
- **"Done" is a proposal — extended to self-modification (§3).** The new headline:
  devclaw never auto-merges a change to its own harness.
- **Fail loud.** A GitHub API failure logs and is visible (surfaced in the cycle
  report / console), never a silent drop; filing failure never wedges the tick.
