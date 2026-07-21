# ADR 0004 — Eval-driven guardrail proportioning + the living eval workbench

- **Status:** accepted 2026-07-20 (Denys). Tranche scheduled same day —
  graduated from [`../proposals/eval-workbench.md`](../proposals/eval-workbench.md)
  under the spec lifecycle. This record freezes the *decision and rationale*;
  system snapshots inside reflect their writing date.
- **Amended 2026-07-21 by [ADR 0006](./0006-continuous-eval-projection.md):**
  step 2's artifact is reshaped — a two-source (`live|basket`) outcome
  projection written at task settle replaces the basket-only `eval_runs`
  table, and the Evals tab gains the clean-night headline. Steps 1/3/4/5 and
  the 2×2 shedding rule stand unchanged.
- **Supersedes:** the "harden blind, then harden more" default — the implicit
  policy that every live failure earns another Python guardrail plus tests,
  with no instrument saying whether the guardrail pays for itself.
- **Relates:** ADR 0003 stage 3 (deferred, still deferred); the mission-control
  packaging borrow (sequenced BEFORE this tranche, completed 2026-07-20);
  issue #178 (gate-pass ≠ quality).

## Context

The daily loop had become a treadmill: overnight run → some new open-world
failure → debug → more Python + more tests → "now it works" → new edge case
tomorrow. ~1,600 tests and the effort still felt like it didn't pay off.

The diagnosis (locked 2026-07-20, Denys + Claude):

1. **We graded devclaw against a goal we never set.** The stated goal is
   learning + CV/portfolio; we kept measuring against "a reliable autonomous
   product that ships unattended overnight" — a frontier-grade phantom.
2. **All effort went to the invisible axis.** Reliability hardening can't be
   tested away — the failure surface is the open world (real repos, npm, model
   variance), not our code — and nobody in the target audience ever sees it.

## Decision

**Stop hardening blind. Measure with the eval instrument, then rebalance the
system away from brittle Python guardrails toward context/prompt engineering +
agentic flow — proving every move with the eval.** The codebase should get
smaller and smarter, not bigger and more brittle. The measurement history
becomes a visible, living workbench — itself the portfolio artifact.

### 1. Two kinds of guardrail — shed one, keep the other

- **Cognitive guardrails** — Python doing thinking the model should do
  (heuristics, hardcoded decisions, babysitting scaffolding). Compensation for
  a weak model → *shed candidates* as models improve.
- **Structural invariants** — fail-closed verification, test-integrity scan,
  OAuth-only stripping, single-writer/CAS state, done-is-a-proposal. System
  contracts, not model-compensation; a more autonomous agent needs them MORE.
  **Never shed; never even A/B-tested for shedding.**

### 2. Calibrate to the WEAKEST target — the 2×2 (load-bearing)

devclaw's long-run production target is a small local model on Denys's PC
(the endgame, planned LAST; hardware-gated; not Qwen-specific), not Claude.
Every shed decision requires the 2×2 measured on BOTH models:

| | small model needs it | small model doesn't |
|---|---|---|
| **Claude needs it** | structural — keep | keep (cheap safety) |
| **Claude doesn't** | **KEEP — portability-critical** | dead weight — **delete** |

The trap: run the eval on Claude, watch it sail through, delete the guardrails
Claude has outgrown — which are exactly the bottom-left cell the small model
depends on. **Never delete on the Claude column alone.** Deletions wait for
the small-model column, whenever it arrives (the §5 timing fork in the
proposal: default = with the endgame; optionally pulled earlier via a small
eval-only local model behind an explicit endpoint seam extending
`DEVCLAW_ACP_COMMAND` — production autonomous runs stay OAuth-Claude, so
OAuth-only is not weakened).

### 3. Verification stays; its implementation climbs the stack

The rebalance is NOT "less verification" — it's moving checks from brittle
Python heuristics to grounded, model-executed, eval-measured checks. The
contract survives; its implementation moves up. Otherwise this quietly becomes
"just trust the model" (the closeloop inert-dead-code failure).

### 4. The instrument exists — operate it, don't rebuild it

`evals/measure_passrate.py` (scored real-pipeline runner, config-driven
baskets, SHA-pinning) + `evals/run_all.py`/`sandbox_e2e.py` (stubbed
goal-layer e2e) + `quality/evals/eval_judge.py` (LLM judge). Layer→instrument
mapping: worker/gate-layer guardrails → `measure_passrate`; goal-layer
cognition → `sandbox_e2e`/`run_all` + the `dry_*` tools. Gate-pass ≠ quality
(#178): `judge_rate` (correct) must sit beside `pass_rate` (green) before
shed decisions rest on it. SWE-bench-Verified via OpenHands' own harness is a
packaging-phase flex, never the shedding loop's instrument.

### 5. The living workbench

Deliberate heavy runs → durable history (`eval_runs` table in `devclaw.db`) →
a console "Evals" tab live over that history. "Alive" ≠ continuous — a real
run burns quota and opens PRs; runs are events, the workbench is the record.
The runner stays a sibling tool under `evals/` (the thing measured doesn't own
its scorekeeper); results surface in the existing console. Run raw FIRST,
build the surface SECOND. MVP = one table + one screen; charts/trends/alerts
are v2, only if the basic screen earns it.

## The phased plan (each step independently valuable & stoppable)

1. **Baseline on Claude** — revive `measure_passrate`, run the existing basket
   on current code + a current Claude model. A live-shakedown-class op (docker,
   logged-in claude, opens PRs, burns quota) — run by Denys. Sub-task: the
   guardrail inventory (cognitive vs structural, ordered shed-candidate list).
2. **Workbench MVP** — `eval_runs` table + the console "Evals" tab; backfill
   June + step-1 runs.
3. **`eval_judge`-as-scorer** — `judge_rate` beside `pass_rate`.
4. **Shed-candidate identification (Claude column)** — with/without A/B per
   cognitive candidate on Claude. NO deletions yet.
5. **Local-model calibration + authorized shedding (LAST)** — the small-model
   column completes the 2×2; only then delete dead weight, keep
   portability-critical.

Locked supporting decisions: first A/B candidate = the adversarial review gate
(`quality/` panel — highest-cost cognitive guardrail, cleanest on/off);
two-tier basket (existing `v01-proof`/lifekit for baseline; small targeted
mini-baskets per guardrail under test); workbench lives in the existing
console, not a separate app.

## Consequences

- Guardrail PRs change shape: a new cognitive guardrail should say what eval
  evidence would justify it (or explicitly claim structural status); a shed PR
  must cite its 2×2 rows.
- The eval history is append-only evidence — runs are never edited or cherry-
  deleted to flatter a trend.
- Risks actively guarded: the workbench becoming the next treadmill (MVP is
  one screen + one table); calibrating to Claude (no shed without the
  small-model number); trusting green (judge_rate); shedding a structural
  invariant (never on the table); rebuilding what exists (wire, don't build).

Full narrative, resolved-decision log, and the honest small-model tension:
[`../proposals/eval-workbench.md`](../proposals/eval-workbench.md) (GRADUATED).
