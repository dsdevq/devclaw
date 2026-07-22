# Proposal — devclaw files (and later fixes) its own issues from the `problems` catalog

- **Status:** **LOCKED (direction)** — locked 2026-07-22 after the §5 clarify step
  (all eight `[OPEN]` items resolved with Denys). Direction is fixed; **schedule is
  not** — a tranche is Denys's call. Reopenable: edit this doc and say so, don't
  silently diverge. First build = **Stage 1 only** (file); Stage 2 (self-fix) stays
  deferred.
- **Date opened:** 2026-07-22 · **Locked:** 2026-07-22 · **Authors:** Denys + Claude
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
- **body** carries the grounded context — `problems.category`/`kind`, `count`,
  `terminal_count`, first/last seen, the `sample_message`, and the `last_goal_id`/
  `last_task_id` pointers it hit (the `problems` row has no `trace_id` FK — those
  pointers are the grounding it does carry);
- **labels** map from `problems.category` (the Stage-1 source of record — see O3):
  a fixed marker `devclaw:self-filed` + `class:<category>` (`class:gate`,
  `class:cognition`, …);
- **idempotent** — one issue per problem `fingerprint` (already the row PK);
  recurrence while the issue is open → a **comment** (bump count/last-seen), never a
  duplicate; a closed issue that recurs in a *new* cycle → **reopen** with a comment.
Denys triages. This is the whole of stage 1: a mechanical `problems → gh issue`
bridge with recurrence gating and a label map. Strong portfolio artifact — *the
system files its own bugs, deduplicated, tagged, reproducible.*

**One honest caveat on "wiring."** Recurrence-by-cycle (O1) is *not* pure wiring:
the `problems` row tracks raw `count` + first/last-seen only — occurrences collapse
into one row, so "seen across ≥ M distinct cycles" has **no backing column today**.
Stage 1 therefore adds one small piece of persistence: a `problem_cycles(fingerprint,
cycle_key)` table (`INSERT OR IGNORE` at cycle close for each problem last-seen inside
the window) plus a nullable `issue_number`/`issue_state` column on `problems` (ALTER
migration, same shape as `sandbox_image`). Everything else is wiring.

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
only the self-referential case needs the brake. Self-recognition is by **repo slug**
(`dsdevq/devclaw`, constant `DEVCLAW_SELF_REPO`, env-overridable) compared via the
existing `goal/remote_checks.py:parse_owner_repo(project.repo_url)` — the slug
survives worktrees/clones/mount differences better than a workspace path. The refusal
seam lives in **`goal/merge.py`** (`resolve_automerge`), which returns a hard,
un-overridable `False` when target == self, beating any per-project `automerge=True`.
(The auto-merge path is `goal/merge.py`, *not* `delivery/merge.py` — corrected from
the DRAFT; `delivery/` only creates repos + PRs.)

## 4. Why this is the right home for recurrence (rescuing O4)

The ops-agent's O4 "trend-signal-repeat" detector never fired and was about to be
dropped. Its logic — *act only when a problem repeats across N cycles* — is
exactly the right **issue-creation trigger** here. A one-off `timeout` is noise; the
same reasoning failure three cycles running is a real, file-worthy issue. So this
proposal absorbs O4 into the dev-loop where it belongs, instead of discarding it.

## 5. Clarify step — RESOLVED (locked 2026-07-22)

All eight items answered with Denys. Values below are the locked direction;
threshold numbers are config constants tunable from live data, not code changes.

- **O1 — Recurrence threshold. → RESOLVED.** File when a problem has appeared in
  **≥ 3 distinct cycles** (matching the rescued O4 `TREND_REPEAT_THRESHOLD=3`)
  **and** `terminal_count > 0` (it caused at least one *terminal* failure, not only
  self-healed `mechanical:*` blocks). Distinct-cycle counting is backed by the new
  `problem_cycles` table (§2 caveat); a self-healing block that never sticks
  (`terminal_count == 0`) never qualifies. `3` is a constant, tunable later.
- **O2 — Idempotency + lifecycle. → RESOLVED.** One issue per `fingerprint`;
  persist `issue_number` + `issue_state` as a nullable `problems` column. Recurrence
  while open → **comment** (bump count/last-seen), not a new issue. A closed issue
  that recurs in a *new* cycle → **reopen with a comment**, never a duplicate.
  **Closing is human-only in Stage 1** — devclaw does not auto-close. (Auto-close-
  when-quiet-for-K-cycles is deferred; owner: Denys.)
- **O3 — Label taxonomy. → RESOLVED.** Map from `problems.category` (the Stage-1
  source; the distinct `eval_outcomes.failure_class` taxonomy is not used here).
  Every filed issue gets a fixed marker `devclaw:self-filed` + `class:<category>`
  (8-category vocab: `block`/`task_fail`/`gate`/`delivery`/`limit`/`cognition`/
  `subprocess`/`other`). Labels are **created-on-first-use** (idempotent ensure-
  exists before `gh issue create`), owned by a fixed map in code — no manual
  pre-seed.
- **O4 — Noise budget. → RESOLVED.** **≤ 3 new issues filed per cycle**, ranked by
  (distinct-cycles desc, then `terminal_count` desc). Suppressed over-threshold
  problems are named in the cycle-report line ("N more over threshold, not filed")
  so silent truncation never reads as "all clear." Comments/reopens on *existing*
  issues don't count against the cap (they aren't new noise).
- **O5 — Stage-2 trigger (starting a self-fix). → RESOLVED: human opt-in.** Devclaw
  never auto-opens a fix-goal on its own issue. A self-fix goal is created only once
  Denys labels the issue `accepted` (or assigns it). Filing is automatic; fixing is
  invited. Locked as direction — the mechanism is Stage 2 (deferred).
- **O6 — Self-repo recognition + auto-merge refusal. → RESOLVED.** Recognize self by
  **repo slug** `dsdevq/devclaw` (constant `DEVCLAW_SELF_REPO`, env-overridable) via
  `parse_owner_repo(project.repo_url)`. The refusal seam is in **`goal/merge.py`**
  (corrected from DRAFT's `delivery/merge.py`): `resolve_automerge` returns a hard,
  un-overridable `False` when target == self, beating any per-project
  `automerge=True`. Ships with a named regression test asserting auto-merge refuses
  on self even with `automerge=True`.
- **O7 — Where issues live + who reads them. → RESOLVED.** devclaw repo Issues
  (`dsdevq/devclaw`), labeled as O3. The **cycle-report push links what it filed**
  ("filed/updated: #123, #124"), appended in `cycle_report.render_summary` — rides
  the existing digest, zero new push. Console surface **deferred** (the `problems`
  catalog isn't in the console today — no `/problems.json` — so it's net-new and out
  of Stage-1 scope; owner: Denys, later).
- **O8 — Egress / auth. → RESOLVED.** Service-side `gh` call on the cycle-close edge
  using the existing **`GITHUB_TOKEN`/`GH_TOKEN`** pillar (same credential as
  `gh pr create`/`gh repo create`; scope `repo` → issues:write). **No `ANTHROPIC_*`
  touched** — the OAuth-only invariant is untouched; this is an orthogonal GitHub
  credential. Fail-loud: a `gh` failure logs + surfaces in the cycle report, never a
  silent drop, never wedges the tick (best-effort, like the cycle-report push).

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
