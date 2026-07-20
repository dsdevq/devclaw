# Proposal — Eval-driven guardrail proportioning + the living eval workbench

- **Status:** **GRADUATED → [ADR 0004](../decisions/0004-eval-workbench.md)**
  — tranche scheduled 2026-07-20 (the mission-control packaging borrow, which
  this was sequenced after, completed the same day). The ADR is canonical from
  here on; this doc is history — the full narrative, the clarify-step Q&A
  trail, and the honest tensions the ADR distills. Locked with Denys
  2026-07-20; every previously-**[OPEN]** item was answered and closed the
  same day (§5 resolved, §10 empty).
- **Date opened:** 2026-07-20 · **Locked:** 2026-07-20 · **Graduated:** 2026-07-20
- **Authors:** Denys + Claude (conversation of 2026-07-20)
- **Supersedes / relates:** the deferred stage 3 of [ADR 0003](decisions/0003-goal-program-unification.md);
  the deferred portability spike; the "scored benchmark overdue" thread.

> How to read this: sections marked **[CONFIRMED]** are things we agreed on in
> conversation. Sections once marked **[OPEN]** have all been resolved in place.
> The phased plan is a *proposal*, not a commitment.

---

## 1. The problem this addresses (the honest diagnosis) [CONFIRMED]

The daily loop had become a treadmill: wake → check status → an overnight run
did ~zero real work because of *some* new error → debug it → add more Python +
more tests → agree "now it works" → schedule the next night → hit a new edge
case tomorrow. ~1,600 tests, and it still feels like it doesn't pay off.

The root cause is **not bugs**. Two things:

1. **We were grading devclaw against a goal we didn't set.** The stated goal is
   *learning + CV/portfolio*. We kept measuring it against *"a reliable
   autonomous product that ships working code unattended overnight."* Against
   that phantom goal it will always feel like failure — because unattended,
   open-ended autonomous coding is at/near the frontier, and reliability there
   is a bottomless pit.
2. **All the effort went to the invisible axis.** Reliability hardening (tests,
   guardrails) is real work that *nobody in the target audience ever sees*, and
   that — crucially — **can't be tested away**, because the failure surface is
   the open world (real repos, npm, model variance), not our code. More tests
   never make the nights clean.

## 2. The thesis (the confirmed direction) [CONFIRMED]

Stop hardening blind. **Measure with an eval framework, then rebalance the
system away from brittle Python guardrails toward context/prompt engineering +
agentic flow — proving every move with the eval instead of adding more cages.**
The codebase should get *smaller and smarter*, not bigger and more brittle.

The win is three-way aligned:
- it's the correct engineering (measure-don't-guess);
- it makes progress **visible** for the first time (directly counters the
  despair — you can *see* point A → point B);
- a visible eval workbench **is** the portfolio artifact. "I built an
  eval workbench that let me strip scaffolding as models improved" is a strong,
  current CV line; "I wrote 1,600 tests for a harness" is not.

## 3. Core principles [CONFIRMED]

### 3.1 Two kinds of guardrail — shed one, keep the other
- **Cognitive guardrails** — Python that does the *thinking* the model should
  do (heuristics, hardcoded decisions, prompt scaffolding that babysits). These
  are compensation for a weak model → *candidates to shed* as models improve.
- **Structural invariants** — fail-closed verification, OAuth-only,
  single-writer state, CAS transitions, "done is a proposal gated on grounded
  evaluation." These are **system contracts**, not model-compensation. A *more*
  capable, more autonomous agent needs them **more**, not less. **Never shed.**

### 3.2 The proportion is a function of the target model
There is no single "right amount of Python." A guardrail is compensation for a
model weakness, so the correct proportion of Python-vs-agentic-flow is a
**function of the target model's capability**:
- bigger model → less Python, more can live in agentic flow;
- smaller model → more deterministic scaffolding, structured outputs, tighter
  gates.

### 3.3 Calibrate to the WEAKEST target, not Claude — the 2×2 [CONFIRMED, load-bearing]
**devclaw's long-run production target is small local models (Qwen-class), not
Claude.** So the eval must run on the *target* model. The trap to avoid: run the
eval on Claude, watch it sail through, and delete guardrails Claude has
outgrown — because those are exactly the scaffolding the small model still
depends on.

Classify each guardrail by a **2×2 measured on both models**:

| | small model (Qwen) needs it | small model doesn't |
|---|---|---|
| **Claude needs it** | structural — **keep** | keep (cheap safety) |
| **Claude doesn't** | **KEEP — portability-critical** | dead weight — **delete** |

The whole game is the bottom-left cell. Calibrating to Claude deletes exactly
those and silently breaks the small-model target. **Calibrate to the floor.**

### 3.4 Verification stays; its implementation moves up the stack
The rebalance is *not* "less verification." It's moving the check from brittle
Python heuristics to **grounded, model-executed, eval-measured** checks. The
contract survives; its implementation climbs. (Otherwise this quietly becomes
"just trust the model" — the closeloop "shipped inert dead code" failure.)

### 3.5 The honest tension [CONFIRMED]
"Target small models" pulls *against* "lean hard on prompt/context engineering":
small models are worse at sophisticated agentic flow, so committing to Qwen
means keeping **more** scaffolding, not less. The resolution is the word
*proportion* — set by the target's capability floor, proven by eval, not by
feeling.

## 4. The instrument — what we already have (do NOT rebuild) [CONFIRMED]

The eval harness largely **exists**; the gap is *operating it*, not building it.

- `evals/measure_passrate.py` — a scored, real-pipeline runner: basket of real
  tickets → docker sandbox → OpenHands → model → real `verify_cmd` gate → opens
  a PR each → writes a `pass_rate` JSON. Already **config-driven**:
  `--basket file.json`, each ticket `{id, kind, goal, repo_url?, verify_cmd?,
  pin_sha?}`; `--only` subset; SHA-pinning for stable re-measurement.
- `evals/baskets/` — task sets (`v01-proof.json`, a homegrown basket).
- `evals/run_all.py`, `evals/sandbox_e2e.py`, `compare_engines.py` — the stub
  e2e + engine-compare runners.
- `devclaw/quality/evals/eval_judge.py` — the LLM-judge for grading quality.
- `evals/runs/` — historical scored runs. **Last real run ~2026-06-25** → stale.

Key facts that shape the plan:
- **Gate-pass ≠ quality.** `measure_passrate` itself flags this (issue #178):
  what it measures is a green `verify_cmd`, explicitly "NOT the v0.1 metric";
  real quality is a human verdict at the PR. To rebalance guardrails on
  *evidence of correctness* (not just "green"), we must make `eval_judge` strong
  enough to trust as the scoring function — itself a context/prompt-engineering
  job.
- **Layer → instrument mapping** (match them or the number lies):
  - *worker / gate-layer* guardrails (integrity guard, review gate, browser
    gate, #2/#4 asserts, verify degradation) → `measure_passrate`;
  - *goal-layer* cognitive guardrails (planner heuristics, decomposer,
    done-gate) → `sandbox_e2e` / `run_all` + the `dry_*` tools.
- **OSS reuse is the DATASET, not a framework.** For a CV-credible external
  number, reuse **SWE-bench-Verified** via **OpenHands' own eval harness** (we
  run on OpenHands; it already handles the hidden-test-patch / FAIL_TO_PASS
  protocol). Do NOT reimplement that protocol in our basket runner. But for the
  guardrail-shedding loop itself, the **existing homegrown basket is enough** —
  SWE-bench is a later flex, not a prerequisite.

## 5. Target-model eval [RESOLVED — locked 2026-07-20]

**Denys's call: a local model on his own PC — no rented GPU, no hosted API.**
Running a local model is devclaw's *production endgame* and is planned as the
**last** milestone (hardware-gated; it's "the last-ever thing," not Qwen-
specific — any local model).

**Key separation to hold onto:** "local model *for production*" (the endgame,
last) is NOT the same as "local model *for eval*" (a few slow batch runs to get
the small-model column of the 2×2). Eval doesn't need throughput. So there's a
timing fork, decided at execution time:
- **Defer (default, matches "local is last"):** the small-model column — and
  therefore the actual guardrail *deletions* — waits for the endgame. Steps 1–4
  still deliver the workbench + Claude baseline + shed-*candidate* list (real,
  visible, CV-valuable), but the codebase-shrinking payoff lands at the end.
- **Pull earlier:** run a *small* local model on the PC **for eval only**,
  decoupled from the production endgame, to complete the 2×2 and shed sooner.
  Viability depends on how strong a model the PC runs — a ~7B may be too weak to
  give signal (fails everything → every guardrail looks "needed").

**Load-bearing invariant either way:** never delete a guardrail on the Claude
column alone (that deletes the portability-critical ones — §3.3). Deletions
require the small-model column, whenever it arrives.

**Prerequisite (unchanged):** a sanctioned **eval-only model-endpoint seam**
(extend `DEVCLAW_ACP_COMMAND` / #283) so the harness can point at the local
model. Production autonomous runs stay OAuth-Claude — the eval path is an
explicit, non-silent experiment, so this does not weaken OAuth-only.

## 6. The living eval workbench [CONFIRMED vision · sketch is DRAFT]

Denys's requirement: the eval must be **visible and alive** — not buried in
code + loose JSON. It should show *how the system evolves, point A → point B*,
with runs stored durably and viewable. It's the artifact that makes the whole
project legible.

Confirmed refinements (the "how"):
- **Run raw FIRST, build the surface SECOND.** Do not build the workbench before
  running `measure_passrate` by hand at least once. The instrument earns its UI
  by being used; building the dashboard first is just the next treadmill.
- **"Alive" ≠ continuous.** A real run spins docker, calls the model, opens PRs,
  burns quota, takes minutes/task — it can't auto-run per commit. "Alive" means:
  *deliberate heavy runs → durable history → a dashboard that is live over that
  history.* Runs are events you trigger; the workbench is the living record.
- **Reuse the substrate, don't build a metrics system.** devclaw already has a
  live console, a telemetry layer (traces, problems catalog,
  `get_scorecard_metrics`, `review_trends`), and `devclaw.db`.
- **Architecture:** the *runner* stays a sibling tool (`evals/` — the thing
  measured shouldn't own its scorekeeper); the *results* surface in the console.

### 6.1 MVP sketch [DRAFT]

**(a) Persist runs to a table** (instead of only `evals/runs/*.json` — the
runner already produces this exact summary):

```
eval_runs
  id            text  pk        -- run stamp
  created_at    text
  model         text            -- e.g. claude-opus-4-8 | qwen2.5-coder-32b
  basket        text            -- basket name/path
  guardrail_cfg text            -- JSON: which guardrails ON/OFF this run
  n             int
  pass_rate     real            -- gate pass-rate
  judge_rate    real  null      -- eval_judge "actually correct" rate (v1.1)
  prs           text            -- JSON list of PR urls
  records       text            -- JSON per-ticket detail
```

**(b) One console screen — "Evals"** — a list over time:

```
┌ Evals ─────────────────────────────────────────────────────────────┐
│ date        model            basket     guardrails      gate   judge │
│ 07-20 14:02 qwen-coder-32b   v01-proof  review:on       55%    40%   │
│ 07-20 11:30 qwen-coder-32b   v01-proof  review:OFF      35%    30%   │  ← the experiment
│ 07-19 …     claude-opus-4-8  v01-proof  review:on       90%    85%   │
│ 06-25 …     claude-sonnet    lifekit    (baseline)      100%   —     │
└──────────────────────────────────────────────────────────────────────┘
  click a row → per-ticket records + PR links
```

That row-pair (`review:on` 55% vs `review:OFF` 35% on Qwen) *is* the guardrail
decision, made visible: on the small model, the review gate is earning +20pts →
**keep it** (bottom-left cell). On Claude it might be 90% vs 90% → Claude doesn't
need it, but Qwen does → still keep. The workbench turns the 2×2 into something
you *see*.

Everything else — charts, trend lines, alerts, auto-diffing configs — is **v2**,
added only if the basic screen proves useful.

## 7. Proposed phased plan [DRAFT — not locked]

1. **Baseline on Claude (cheap, today-ish).** Revive `measure_passrate`, run the
   existing basket on current code + a current Claude model. One fresh number +
   a set of PRs. This is the reference point. *(Real pipeline: docker + logged-in
   claude + opens PRs + burns quota — a live-shakedown-class op, run by Denys.)*
2. **The workbench MVP.** Persist runs to `eval_runs`; add the one console
   screen. Backfill the June + step-1 runs so there's history to look at.
3. **`eval_judge`-as-scorer.** Upgrade the judge so `judge_rate` (correctness)
   sits beside `pass_rate` (green) — so guardrail decisions rest on *correct*,
   not just *green*.
4. **Shed-candidate identification (Claude column).** Run the with/without pair
   on Claude for each cognitive candidate — build the ordered candidate list and
   the "does Claude need it" column. **No deletions yet** — a shed needs the 2×2
   complete. Structural invariants are out of scope entirely.
5. **Local-model calibration + authorized shedding (LAST — matches the
   production endgame, §5).** Run the candidates on the local model → the
   small-model column. Complete the 2×2 and only THEN delete the dead-weight
   guardrails; keep the portability-critical ones. *(Can be pulled earlier via a
   small eval-only local model — the §5 timing fork.)*

Each step is independently valuable and independently stoppable. Steps 1–2 alone
already break the "invisible axis" spell; the codebase-shrinking payoff lands at
step 5.

## 8. Risks & anti-patterns to actively avoid [CONFIRMED]

- **The workbench becomes the next treadmill.** Biggest risk. Guard: run raw
  before building UI; MVP is *one screen + one table*, not a metrics platform.
- **Calibrating to Claude.** Deletes the portability-critical guardrails. Guard:
  no shed decision without a Qwen number.
- **Trusting "green."** Gate-pass isn't correctness. Guard: judge_rate.
- **Shedding a structural invariant.** Never. The 2×2 applies only to cognitive
  guardrails; fail-closed / OAuth / single-writer / done-is-a-proposal are not
  on the table.
- **Rebuilding what exists.** The harness, DB, console, telemetry all exist.
  Wire, don't build.

## 9. Resolved decisions [LOCKED 2026-07-20 · reopenable]

- **First guardrail to test → the adversarial review gate** (`quality/` review
  panel). Highest-cost cognitive guardrail, cleanest on/off A/B via
  `measure_passrate`, textbook 2×2 demo (likely "Claude doesn't need / Qwen
  does → keep-for-portability"). Full guardrail inventory = a step-1 sub-task.
  **Never-test-to-shed (structural):** fail-closed verify, test-integrity scan,
  OAuth strip, single-writer/CAS, done-is-a-proposal.
- **Basket → two-tier.** (1) Baseline pass-rate: reuse the existing
  lifekit / `v01-proof` basket (SHA-pinnable). (2) Per-guardrail shed tests: a
  *small targeted mini-basket that stresses the guardrail under test* (e.g. for
  the review gate, tasks where a plausible-but-wrong solution passes tests, so
  the gate's value shows as a pass-rate delta). No giant basket up front.
- **Qwen setup → not blocked for eval.** Rented GPU / hosted Qwen +
  eval-only endpoint seam; target Qwen2.5-Coder-32B. See §5.
- **Workbench home → a new "Evals" tab in the EXISTING console** (already
  tabbed, live). Not a separate app. Runner stays a sibling; results surface in
  the console.
- **SWE-bench → a packaging-phase deliverable, decoupled from the shedding
  loop.** It's the headline CV number, gotten during mission-control #1
  packaging. Caveat for then: SWE-bench scores far lower on Qwen than Claude →
  the *headline number is the Claude one*, portability is the *story* around it.
  The shedding loop uses the homegrown basket, never SWE-bench.

## 10. Remaining open items — NONE blocking [closed 2026-07-20]

- ~~Rented-GPU budget~~ → **resolved:** local model on the PC, no rented GPU
  (§5). Production-local is the endgame/last; eval-local timing is the §5 fork,
  an execution-time judgment, not a planning blocker.
- ~~Step-1 guardrail inventory~~ → **not a decision** — it's deferred *work*
  (audit the codebase, produce the ordered shed list), done when execution
  starts, not during planning.

**The plan is fully locked. Nothing requires an answer before execution.**

---

*Living doc. Add, cut, argue in the margins. Lock → graduate to an ADR.*
