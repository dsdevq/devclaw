# Guardrail inventory — the ADR 0004 step-1 audit (2026-07-20)

The input to the eval-driven guardrail-shedding program
([ADR 0004](../docs/decisions/0004-eval-workbench.md)). Point-in-time audit of
every mechanism that intervenes between the model and the outcome, classified
per the ADR's cognitive-vs-structural split, with its off-switch (A/B seam),
cost, and measuring instrument. Lives in `evals/` because it is eval-tranche
working data (the runner is a sibling of the thing measured), not a maintained
reference doc — re-audit before trusting file:line refs after major changes.

Legend: **C** = COGNITIVE (shed candidate) · **S** = STRUCTURAL (never-shed,
out of scope) · **H** = HYBRID (structural contract, cognitive implementation —
the cognitive half is the movable part).

---

## A. quality/ — worker / gate-layer (instrument: `measure_passrate`)

### A1. Adversarial review gate + panel + degradation ladder — **H (cognitive core)** — *locked shed-candidate #1*
- `devclaw/quality/__init__.py` — `review_diff`, `_review_panel_core`, `review_panel` + degradation ladder; wired in `task_queue._review_failure`, enable check `_review_gate_enabled`.
- What: after verify+integrity pass, a Claude pass reads the diff vs the ticket and returns approve/request_changes; `request_changes` re-enters the retry loop.
- Cost: **1 Claude call per successful code task** (N calls at `DEVCLAW_REVIEW_PANEL_N>=2`; per-file fan-out on the degrade ladder can burst up to `DEVCLAW_REVIEW_DEGRADE_MAX_FILES`=40 calls). ~756 LOC. Heavy misfire history: #210 (timeout→fail-closed→burned retries), #224 (generated-diff crash), #227 (reviewed wrong repo), #245 (quota sub-quorum misread as defect), #254 (panel), #281 (ladder).
- Structural part (never-shed): the fail-closed contract — unparseable/crash/sub-quorum RAISES, never approves. Cognitive part (shed): the review judgment itself, panel fan-out, lens prompts, the `filter_reviewable_diff` generated-file heuristic.
- A/B: mostly already — `DEVCLAW_REVIEW_PANEL_N=1` is byte-identical single reviewer; per-project `review_gate` registry override turns it fully off; `DEVCLAW_REVIEW_DEGRADE*`. Gap: no single env kills the base gate — a `DEVCLAW_REVIEW_GATE` env would complete the seam.

### A2. Browser-E2E gate — **H**
- `devclaw/quality/browser_gate.py` (~277 LOC); wired `task_queue._browser_gate_failure`, `_browser_gate_mode`.
- What: a diff touching web-UI path globs must carry a passing Playwright `browser_report` (executed>0, 0 failed) or it fails closed and retries.
- Cost: zero LLM (pure verdict fold). History: #264 birth, #278 library-only false-positive scoping, the cmn-tab-group 14h wedge.
- Structural part: "UI must be exercised before it ships" fail-closed contract. Cognitive/movable: the hardcoded `DEFAULT_FRONTEND_GLOBS`/`DEFAULT_LIBRARY_GLOBS` path heuristics + flexible/strict decision — brittle glob taste a model could judge.
- A/B: already — `DEVCLAW_GOAL_BROWSER_GATE=0`, `DEVCLAW_GOAL_BROWSER_GATE_MODE`.

### A3. Browser-gate reachability judge — **C**
- `devclaw/quality/reachability.py` (~116 LOC); wired `task_queue._browser_reachability_clears`.
- What: a cognition call that can reason away A2's false positive (changed UI not rendered in the running app) — only runs on a proven `reachable=="no"` would-block path.
- Cost: 1 Claude call, rare (zero-token on all other paths). A guardrail compensating for another guardrail's bluntness. Strictly safe to drop (can only relax a block).
- A/B: already — `DEVCLAW_GOAL_BROWSER_REACHABILITY=0` (moot when A2 off).

### A4. eval_judge — out of scope (this IS the instrument)
- `devclaw/quality/eval_judge.py` (~155 LOC). Offline scoring, not a runtime guardrail. ADR step 3 upgrades it. List, don't shed.

### A5. Test-integrity scan — **S (locked never-shed)**
- `devclaw/loom/test_integrity.py` (~208 LOC); wired `task_queue._integrity_failure`. Pure diff scan for deleted tests / added skips. Zero LLM.

## B. goal/ — goal-layer cognition (instrument: `sandbox_e2e`/`run_all` + `dry_*`)

### B1. Done-gate ("done is a proposal") — **H / S-locked**
- `devclaw/goal/tick_donegate.py`: `_done_gate_review_brief` (~95-line hardcoded review-prompt rubric), `_resolve_done_gate`, `_open_done_gate`.
- Structural (locked never-shed): done is a proposal gated on grounded evaluation. Cognitive/movable: the review-brief prose — per-clause evidence + structural-health rubric is prompt scaffolding that babysits; could shrink as the model internalizes it.
- Cost: 1 review dispatch (full sandbox agent) + 1 evaluator call per done-proposal. History: finance-sentry-mcp vague-report-stamped-achieved; closeloop App.tsx 1827 LOC.
- A/B: `DEVCLAW_GOAL_VERIFY_DONE` toggles the review dispatch (artifact-only fallthrough).

### B2. Direction evaluator + periodic cadence — **H**
- `devclaw/goal/evaluator.py` (~584 LOC). Structural: grounded-eval contract. Cognitive/movable: the periodic mid-flight cadence.
- Cost: 1 call every `DEVCLAW_GOAL_EVAL_EVERY` deliveries + at done-gate. A/B: already — `DEVCLAW_GOAL_EVAL_EVERY=0` (done-gate only).

### B3. Decomposer (up-front checklist) — **C**
- `devclaw/goal/decomposer.py`, `checklist.py` (pure schema), `planner.py` (program adapter).
- Cost: 1 deep-tier call per goal. Structured-output scaffolding compensating for planner drift.
- A/B: already — `DEVCLAW_GOAL_DECOMPOSE=0`. Instrument: `dry_decompose` + `run_all`.

### B4. Investigate / discovery / world-research stack — **C**
- `devclaw/goal/research.py`, `world_research.py` (`repo_brief.py` is zero-LLM memory — neutral).
- Cost: 1 dispatch + 1–2 cognition calls per goal. Scaffolding to stop the planner inventing shape.
- A/B: already — `DEVCLAW_GOAL_INVESTIGATE=0`. Instrument: `dry_world_research` + `sandbox_e2e`.

### B5. Firming phase — **C**
- `devclaw/goal/firmed.py`, `goal/phases/firming.py`. Cognition + owner round-trips. A/B: `DEVCLAW_GOAL_FIRMING=0` (default off).

### B6. Admission heuristics — **C**
- `devclaw/goal/admission.py`: `_check_vague_done_when` length heuristic, `_check_scope_anchor_for_from_scratch`, `_check_bare_verify_cmd` regex, `_check_standing_done_when`.
- Cost: zero LLM, ~200 LOC of hardcoded taste a model could judge in one pass. Self-flagged "length-only heuristic, intentionally simple".
- A/B: no env flag — off-switch means guarding the shape checks (presence checks stay). Medium effort.

### B7. Self-triage interceptor — **C**
- `devclaw/goal/triage.py` (~210 LOC). 1 Claude call only when a real owner ping fires (zero-token idle preserved). A/B: `DEVCLAW_SELF_TRIAGE=0`. Telemetry-measured only.

### B8. Item asserts (reality-anchored acceptance) — **H / S-leaning**
- `devclaw/goal/tick_settle.py`: `_check_one_assert_sync`, `_check_addressed_asserts` (#298, ADR 0003 #2/#4).
- Mechanical `file_exists`/`grep` enforcement at settle — fail-closed reality anchor under the LLM gate; zero LLM. The assert *contents* are decomposer-authored (cognitive), the enforcement is structural.
- A/B: `DEVCLAW_ITEM_ASSERTS=0` (operator kill-switch).

### B9. Per-item + per-workspace circuit breakers — **S (mechanical)**
- `tick_settle.py` `_apply_item_failure`/`DEVCLAW_ITEM_MAX_ATTEMPTS`; `task_queue._check_and_trip_breaker`. Anti-storm loop-guards, zero LLM. Never-shed-class.

### B10. Tick guards / auto-heal / no-progress watchdog — **S (mechanical)**
- `devclaw/goal/tick_guards.py` (~387 LOC): block handlers, damped `_autoheal_*` caps + backoff, zero-token `_check_no_progress`. #230/#235/#237. Structural.

### B11. Remote-checks / CI gate — **S**
- `devclaw/goal/remote_checks.py`; wired in `_resolve_done_gate`. Fail-closed grounded CI verification. A/B exists (`DEVCLAW_GOAL_REMOTE_CHECKS`, `DEVCLAW_GOAL_CI_GATE`) but out of scope.

## C. task_queue.py — worker orchestration (instrument: `measure_passrate`)

### C1. Retry-on-fail loop + attempt-history feedback — **H (cognitive-leaning)**
- `task_queue._run_and_settle` retry loop, `TASK_MAX_RETRIES`, attempt-failure history prompt, retry-isolation reset (#277).
- What: re-runs a gate-failing task, feeding the numbered failure history back as prompt so the agent self-corrects.
- Cost: **HIGH** — a full agent re-run + full gate stack per retry; the feedback-history prompt is compensate-for-weak-self-corrector scaffolding.
- A/B: already — `DEVCLAW_MAX_RETRIES=0`. The biggest per-run cost lever.

### C2. Quota/rate failure classifier — **H (cognitive impl, structural intent)**
- `devclaw/loom/limits.py` `classify_failure` + regex banks; vendored copy in `runner.py` `_detect_usage_limit`.
- Structural intent: never burn quota re-probing a limit. Cognitive impl: a large, chronically-patched regex bank (#189/#190 + repeated wording misses) — could move to a cheap model classify.
- A/B: no off-switch (fail-open default REAL). Shedding = replace-not-delete; robustness win, not cost-cut. Rank last.

### C3. Worker honest-block / honest-exit — **S** — `_WORKER_BLOCKED_MARKER` fail-fast-closed path (#280); runner `_parse_blocked_reason`. Out of scope.

### C4. MAX_PAUSE_REQUEUES bound — **S (mechanical)**. Anti-infinite-loop bound. Zero LLM.

## D. openhands-runner/runner.py — worker prompt layer (instrument: `measure_passrate`)

### D1. Skills wiring + kind-wrappers + quality-bar/verify-coda/return-contract — **C**
- `_load_skills`, `_wrap_goal`, `_KIND_WRAPPERS`, `_RETURN_CONTRACT`, `_VERIFY_CODA`, `_QUALITY_BAR`, `_CONTEXT_PREAMBLE`.
- What: prepends universal + per-repo skill bundles and hardcoded quality-bar/verify-coda/return-contract prose to every worker goal.
- Cost: zero extra LLM calls but bloats every worker prompt — exactly the babysitting-scaffolding class the ADR names.
- A/B: de-facto = point `DEVCLAW_SKILLS_DIR` at an empty dir (embedded wrappers remain); no clean "no scaffolding" flag. Medium effort.

### D2. Universal + per-repo hooks — **S (mechanical extension seam)** — `_run_hook`, `DEVCLAW_HOOKS_DIR`. Deterministic scripts, not model-compensation.

### D3. Return-contract parse (`browser_report`, REPO NOTES, `BLOCKED:`) — structural proof-of-execution plumbing. Out of scope.

## E. Infra / seams (not guardrails — listed for completeness)
- `devclaw/dispatch_gate.py` — operator manual-pause + run-window (fail-open). Operator control.
- `devclaw/cognition.py`, `devclaw/model_tiers.py` — the cognition seam + tiering; the §5 eval-endpoint seam extends here (with `DEVCLAW_ACP_COMMAND`).
- `devclaw/elicitation.py` (scope grill) — human-invoked cognition tool, not an autonomous guardrail.

---

# Ordered shed-candidate list

Cognitive guardrails + cognitive halves of hybrids only, ranked by
(expected cost saved × ease of A/B). #1 was locked in the ADR before this
audit; the audit confirms it. **A shed still requires the full 2×2 —
this list only orders the experiments, it authorizes nothing.**

| # | Candidate | Off-switch (A/B seam) | Cost saved | Instrument |
|---|---|---|---|---|
| **1** | **Adversarial review gate / panel** (A1) — *locked* | `DEVCLAW_REVIEW_PANEL_N`, registry `review_gate`, `DEVCLAW_REVIEW_DEGRADE*` (gap: no base-gate env) | 1–N Claude calls per successful code task — biggest steady-state gate-layer burn | `measure_passrate` |
| 2 | Retry loop + attempt-history scaffolding (C1) | `DEVCLAW_MAX_RETRIES=0` | Highest per-run cost (full re-run + gate stack per retry); trivial on/off | `measure_passrate` |
| 3 | Investigate/world-research stack (B4) | `DEVCLAW_GOAL_INVESTIGATE=0` | 1 dispatch + 1–2 calls per goal | `sandbox_e2e` / `dry_world_research` |
| 4 | Decomposer up-front checklist (B3) | `DEVCLAW_GOAL_DECOMPOSE=0` | 1 deep-tier call per goal | `run_all` / `dry_decompose` |
| 5 | Direction-evaluator periodic cadence (B2, cognitive half) | `DEVCLAW_GOAL_EVAL_EVERY=0` (done-gate stays) | 1 call / N deliveries | goal-layer `run_all` |
| 6 | Done-gate review-brief prose (B1, cognitive half only) | `DEVCLAW_GOAL_VERIFY_DONE` (contract stays via evaluator) | 1 sandbox dispatch + eval per done-proposal; shrink the 95-line rubric | `sandbox_e2e` |
| 7 | Worker prompt scaffolding / skills / return-contract (D1) | empty `DEVCLAW_SKILLS_DIR` (no clean flag) | Per-task token bloat | `measure_passrate` |
| 8 | Browser-gate reachability judge (A3) | `DEVCLAW_GOAL_BROWSER_REACHABILITY=0` | 1 call, rare paths only | `measure_passrate` |
| 9 | Firming phase (B5) | `DEVCLAW_GOAL_FIRMING=0` (default off) | Low incremental | `sandbox_e2e` |
| 10 | Admission heuristics (B6) | no flag — needs one added | Zero LLM; code-shrink only | `run_all` |
| 11 | Self-triage interceptor (B7) | `DEVCLAW_SELF_TRIAGE=0` | 1 call, very rare | telemetry only |
| 12 | Quota-classifier regex → model (C2, movable impl) | no off-switch (fail-open) | Replace-not-delete; robustness, not cost | n/a |

**Never-shed (structural, out of scope — confirming the ADR's locked list):**
fail-closed verify gate · test-integrity scan (A5) · OAuth strip / OAuth-only ·
single-writer/CAS transitions · done-is-a-proposal contract (B1 structural
half) · remote-checks CI verification (B11) · item-asserts fail-closed anchor
(B8) · worker honest-exit (C3) · circuit breakers + auto-heal + watchdog
(B9/B10/C4) · dispatch-gate operator controls (E).
