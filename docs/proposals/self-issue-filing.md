# Proposal — the self-improving cycle: devclaw files (and later fixes) its own issues

- **Status:** **Stage 1 (FILE + CLOSE) — LOCKED 2026-07-22. Stage 2 (FIX) — LOCKED
  2026-07-23.** §3 self-merge autonomy RESOLVED = **tiered by blast radius** (§3;
  reversed the 2026-07-22 full-auto lean "with fresh eyes" exactly as that clause
  required); P2 boundary firmed in §5A with **shadow-first** tiered auto-merge chosen.
  LOCKED = direction, not schedule — the tranche is Denys's to sequence; no Stage-2
  code lands outside it. Stage 1 carries no self-merge, so it locked independently.
- **Date opened:** 2026-07-22 · **Revived + partially locked:** 2026-07-22 ·
  **Stage 2 locked:** 2026-07-23 · **Authors:** Denys + Claude
- **Relates to:** [ADR 0006](../decisions/0006-continuous-eval-projection.md)
  (the cycle report + `problems`/`eval_outcomes` this reads from), the ABANDONED
  `ops-agent-problems-consolidation.md` (this is the **dev-loop** half of that cut),
  and backlog **#259** (age-out stale problems — absorbed into Stage 1's CLOSE).
  Backlog tracker: **#340**.

## 0. The self-improving cycle (framing)

Denys's target isn't an issue-filer — it's a **closed self-improving loop**, so
problems don't accumulate:

```
   ┌────────────────────────────────────────────────────────┐
   │                                                          │
 GATHER  ──▶  FILE  ──▶  FIX  ──▶  CLOSE  ──▶ (recurs again?) ┘
 (have it)  (Stage 1)  (Stage 2)  (Stage 1: resolve / age-out)
```

- **GATHER** — exists: the `problems` catalog + `eval_outcomes` + cycle report.
- **FILE** — recurring problem -> labeled GitHub issue on the devclaw repo.
- **FIX** — devclaw points its own goal loop at an issue -> PR through the gates.
- **CLOSE / don't accumulate** — an issue leaves the board by **two exits**: it is
  *fixed* (recurrence stops -> auto-close) or it *ages out* (stopped recurring on
  its own -> close as stale). This is the anti-accumulation Denys asked for.

**Sliced** (per `spec-lifecycle.md` "Sizing novel work"):

- **P1 (LOCKED, this doc):** GATHER->FILE->CLOSE — auto-file + auto-close/age-out.
  A complete, demoable loop with **zero self-modifying code** -> zero risk. The
  portfolio artifact: *the system catalogs, files, and retires its own bugs.*
- **P2 (DRAFT):** FIX — dispatch a filed issue to the goal loop -> PR. §3 autonomy
  is the open question here.
- **P3 (DRAFT):** tighten recurrence trigger, dedup, cost caps.

**Revival note.** This was parked 2026-07-22 (the reliability reframe judged it a
substitute for fixing the real trust bug). That objection is now spent: the trust
bug is fixed (#327), a real backlog exists for it to feed, and Denys wants the loop
for its own sake. Revived by Denys's direction.

## 1. Idea

Devclaw already *observes* its own failures. Close the loop: a **recurring**
problem becomes a **GitHub issue on the devclaw repo**, tagged by failure class —
and, later and gated, devclaw picks that issue up as a goal, fixes it, and runs the
fix through the gates it already has. The "mission-control" pattern (watchdog files
an issue -> agent fixes -> evaluation gates the fix), pointed at devclaw itself.

Almost entirely **wiring existing parts** (+ one tiny table, see O1):
- `problems` catalog — deduped (fingerprint UPSERT) and categorized
- `eval_outcomes.failure_class` — the mechanical taxonomy
- traces — the grounded repro/context per failure
- the goal loop — already `goal -> fix -> gate -> evaluate`; delivery — already
  `commit -> branch -> push -> PR`

## 2. The shape, staged

**Stage 1 — FILE + CLOSE (LOCKED, do first).** At run-cycle close (the same
mechanical, zero-LLM edge that assembles the cycle report, `goal/cycle_report.py`):

- *File:* for each problem that **recurred past threshold** (O1), open/update a
  GitHub issue on the devclaw repo — body carries failure class, count, first/last
  seen, representative trace, goals/tasks hit; labels map from `problems.category`
  (O3); **idempotent** — one issue per fingerprint, recurrence updates the existing
  issue, never duplicates (O2).
- *Close (the anti-accumulation exit):* when a filed problem **stops recurring for
  K cycles**, auto-close its issue as stale (age-out). This pulls the auto-close
  that O2 originally deferred **into P1**, because Denys's requirement is that
  problems must not accumulate. (A human can still close early; devclaw only
  *auto*-closes on the age-out signal.)

Denys triages what's open. A mechanical `problems <-> gh issue` bridge with
recurrence gating, a label map, and stale age-out.

**Stage 2 — FIX (DRAFT, gated, later).** Devclaw picks up one of its own issues as a
durable goal -> fix -> the review / eval / browser gates run. Self-referential
autonomy = §3 (reopened).

## 3. Self-modification autonomy — RESOLVED 2026-07-23 = (b) TIERED BY BLAST RADIUS

The originally-locked invariant:

> **On its own repo, devclaw may open a *draft PR* and run every gate — but a
> human merges. Never auto-merge to self.** — because a bad self-fix could break
> the very gate meant to catch it, and the orchestrator cannot recover its own
> bricked loop.

**History.** Reopened 2026-07-22 when Denys leaned **full-auto-if-green**; that
choice was recorded as explicitly *not settled*, to be re-decided with fresh eyes at
the Stage-2 lock.

**Resolved 2026-07-23 — Denys chose (b) TIERED**, over his own prior full-auto lean,
after two arguments landed: (1) the **self-referential hazard** — on its *own* repo a
self-fix can modify the very gate meant to catch it, so "all gates green" can mean
"good fix" *or* "the fix weakened its own gate"; this failure mode is unique to
self-modification and full-auto cannot guard it; (2) **current reliability** — with
devclaw's live #1 complaint being "hits a stupid error and just fails," a green-but-
wrong auto-merge to the orchestration core can brick the very loop that would recover
it (the healer is what broke). Tiered keeps ~all the autonomy and human-gates only the
paths that can self-brick or self-weaken a gate.

**The blast-radius partition (fail-closed on the classifier):**

- **AUTO-MERGE if green** — peripheral surface: `docs/**`, `tests/**`, `evals/**`,
  `delivery/**`, `server/**` (non-core), and peripheral `goal/` helpers.
- **HUMAN-GATE (draft PR, always), even if green** — the self-brick / self-referential
  set: `goal/tick*`, `goal/transitions*`, `task_queue*`, `engine/**`, **`quality/**`
  and the gate prompts** (added at resolution — the single most dangerous
  self-modification target, per the hazard above), and `loom/` limits/test_integrity.
- **Unclassifiable / mixed / new top-level path ⇒ treat as core ⇒ HUMAN-GATE.** The
  classifier defaults CLOSED: an unknown path never auto-merges. A PR that touches
  *both* tiers is human-gated as a whole (no partial auto-merge of a mixed diff).

Rejected: **(a) propose-only** (safest but throttles even a docs typo behind a human)
and **(c) full-auto-if-green** (maximum autonomy, but ships a gate-weakening fix
unattended and can brick core with no self-recovery).

Stage 1 is unaffected (it never merges). On *other* repos the full auto-loop stays —
this tiering is **self-repo only**, keyed on `DEVCLAW_SELF_REPO` (O6).

## 4. Why this is the right home for recurrence (rescuing O4)

The ops-agent's O4 "trend-signal-repeat" detector never fired and was about to be
dropped. Its logic — *act only when a problem repeats across N cycles* — is exactly
the right **issue-creation trigger** here (O1). This proposal absorbs O4 into the
dev-loop where it belongs.

## 5. Clarify decisions (RESOLVED 2026-07-22 unless marked reopened)

- **O1 — Recurrence threshold. RESOLVED.** File when a problem is in **>=3 distinct
  cycles AND `terminal_count > 0`** (self-healed-only blocks never qualify). Needs a
  **new small `problem_cycles(fingerprint, cycle_key)` table** — raw `count` can't
  express cross-cycle survival. `3` is a tunable constant (rescues O4's
  `TREND_REPEAT_THRESHOLD=3`).
- **O2 — Idempotency + lifecycle. RESOLVED (+ auto-close pulled into P1).** One
  issue per `fingerprint`; nullable `issue_number` / `issue_state` on `problems`;
  comment on open-recurrence, reopen on new-cycle-recurrence. **Auto-close on
  age-out** (problem quiet for K cycles) is now IN P1 (Denys's anti-accumulation);
  humans may also close early.
- **O3 — Labels. RESOLVED.** From `problems.category` (8-vocab), NOT
  `eval_outcomes.failure_class` (distinct taxonomies); marker `devclaw:self-filed`
  + `class:<category>`, created-on-first-use. (Coexists with the P0/P1/P2 + `area:*`
  backlog taxonomy; self-filed issues get triaged into it.)
- **O4 — Noise budget. RESOLVED.** <=3 new issues/cycle; suppressed ones **named**
  in the cycle-report line (never a silent truncation).
- **O5 — Stage-2 start trigger. RESOLVED (Stage 2).** Human opt-in via an
  `accepted` label; devclaw never *starts* modifying itself unprompted. (Distinct
  from §3, which is about the *merge*, not the start.)
- **O6 — Self-repo recognition + merge seam. RESOLVED (mechanism) / REOPENED
  (policy).** Recognize self by **slug** `dsdevq/devclaw` (`DEVCLAW_SELF_REPO`) via
  `parse_owner_repo`; the refusal seam lives in **`goal/merge.py`** (the proposal's
  original `delivery/merge.py` was wrong). *What* the seam does on self is §3.
- **O7 — Where issues live. RESOLVED.** devclaw repo Issues; the cycle-report push
  links the issues it filed ("filed #123, #124"); console surfacing deferred.
- **O8 — Egress / auth. RESOLVED.** Service-side `gh` via `GITHUB_TOKEN` / `GH_TOKEN`
  (a GitHub credential); **no `ANTHROPIC_*`** involved — the OAuth-only invariant is
  untouched. Fail-loud on API error.

## 5A. Stage-2 (P2) boundary — firmed 2026-07-23 (the slice to lock)

Per `spec-lifecycle.md` "Sizing novel work", this firms the **P2 slice boundary**
only; P3 stays named-but-unsized. The end-to-end FIX flow and what's in vs out:

- **Start (O5, resolved).** A human applies the `accepted` label to a
  `devclaw:self-filed` issue. Nothing self-modifying starts without it.
- **Pickup mechanism (firmed).** On the **same once-per-cycle mechanical edge** that
  files/closes (`cycle_report.py` / `_maybe_emit_cycle_report`), scan for
  `accepted` + `devclaw:self-filed` issues with **no active goal**, and open **one
  durable goal per issue** targeting `DEVCLAW_SELF_REPO`. Zero-LLM to *detect* (a
  `gh` list + a state check); the goal loop does the reasoning. No new heartbeat
  path — reuses the existing edge, so the zero-token idle guard is untouched.
- **Goal mode = `one_shot` (firmed).** A single issue is a bounded deliverable →
  plan-once-run-the-checklist (ADR 0003 dial), not long_lived re-planning.
- **Concurrency = 1 self-fix goal at a time (firmed, tunable).** Serialize
  self-modification: parallel self-fixes multiply the self-brick surface and muddy
  failure attribution. One in flight; a queued `accepted` issue waits.
- **Fix → DRAFT PR → gates (existing loop).** On the self-repo, delivery opens a
  **draft** PR so the merge seam decides; the review / eval / browser gates run as
  today under the goal's `trust` dial.
- **Merge seam = the §3 tiered classifier (firmed mechanism).** In `goal/merge.py`
  (O6): classify the PR's changed paths against the §3 blast-radius allowlist.
  Peripheral+green → the auto-merge tier; core / mixed / unknown-path → stays a
  human-gated draft PR + one owner ping. **Fail-closed** — unclassifiable ⇒ core.
- **Close.** Stage-1 CLOSE already retires the issue when recurrence stops; a merged
  auto-tier fix additionally closes it as `fixed` and comments the PR link.

**How the tiered auto-merge turns on — RESOLVED 2026-07-23 = SHADOW-FIRST.**

Build the full tiered classifier in P2, but ship it with auto-merge **defaulted OFF**
behind a flag: on every self-PR the classifier *logs* its verdict ("would auto-merge:
yes/no, tier=…") while a human still merges everything. Flip the flag to live (P2.1)
once a handful of shadow verdicts look right. This builds the whole §3 mechanism yet
fires **zero unattended self-merges until there's evidence** — maximally de-risks the
self-brick concern the §3 decision was about, and P2 still ships a complete, demoable
self-fix loop (issue → PR → gates) as a standalone increment. *Rejected:
live-immediately* (faster to full autonomy, but no evidence buffer before the first
unattended self-merge). The live-flip is its own tiny slice **P2.1**, gated on shadow
evidence.

**Sizing P2 (shadow-first path), in devclaw units — end-of-week cap:**

1. Pickup: `accepted`-scan + one_shot goal spawn at the cycle edge (+ concurrency
   cap). **~1 PR.**
2. Draft-PR-on-self + the tiered classifier in `goal/merge.py`, **shadow mode**
   (log verdict, human merges). **~1–2 PRs.**
3. Named regression tests (accepted→goal spawn, classifier partition incl.
   fail-closed on unknown path, mixed-diff human-gate, shadow logs-but-never-merges,
   zero-token guard on the scan) + `invariant-guard`. **folded into the above.**

→ **P2 ≈ 2–3 PRs**, capped end-of-week. The live-flip flip (P2.1) is a later, tiny
slice once shadow evidence accrues. P3 (recurrence-trigger tuning, dedup, cost caps)
stays unsized until P2 lands.

## 6. Invariants — referenced, not restated

- **Zero-token idle guard.** Stage-1 file+close is mechanical (recurrence/age-out
  math + a GitHub API call) on the cycle-close edge — no LLM on any tick path, same
  shape as the cycle report. The `FakeClaude.calls == 0` guard tests must stay green.
- **Single writer to state.** Filing *reads* the `problems` catalog; the
  `issue_number`/`issue_state`/`problem_cycles` writes go through the store's single
  writer, not a second one.
- **"Done" is a proposal — extended to self-modification (§3, reopened).**
- **Fail loud.** A GitHub API failure logs + surfaces (cycle report / console),
  never a silent drop; a filing failure never wedges the tick.

## 7. Build order (Stage 1)

1. Schema: `problem_cycles(fingerprint, cycle_key)` table + `issue_number` /
   `issue_state` columns on `problems` (single-writer migration).
2. Recurrence + age-out pure helpers (unit-testable over primitives, mirrors
   `dispatch_gate` / `cycle_report` style — no DB, no clock).
3. `gh` issue upsert/close adapter (service-side, `GITHUB_TOKEN`, fail-loud).
4. Wire at cycle-close in `cycle_report.py`; cycle-report line links filed/closed #s.
5. Named regression tests (recurrence threshold, idempotent upsert, age-out close,
   noise cap, zero-token guard) + `invariant-guard` before PR.
