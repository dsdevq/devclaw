# DevClaw evals

Two layers of measurement against the chef:

1. **Sandbox e2e suite** (`sandbox_e2e.py` + `run_all.py`) — isolated, scenario-driven runs that exercise every real path the chef supports (single task, full goal lifecycle, scope grill, blocked planner, steered goal, failing verify, no-progress watchdog, quota pause, off-track done-gate). Default mode uses stub cognition (free, deterministic, CI-runnable) and the stub engine (no docker, no real PRs); opt into real `claude --print` with `--cognition claude`. **This is the regression metric** — run it before/after any change that touches the runtime path.

2. **Real-pipeline harnesses** — drive the actual `claude + docker + OpenHands` pipeline end-to-end against a real repo. Used for pass-rate measurement, gate-discrimination validation, and quality-vs-greenness checks. These cost real OAuth quota and dispatch real PRs; treat them as periodic measurement, not CI.

## Sandbox e2e suite

```bash
# Full suite (free, deterministic, ~30s):
.venv/bin/python evals/run_all.py

# Single scenario (full trace + per-tick goal-state snapshot):
.venv/bin/python evals/sandbox_e2e.py --scenario blocked_planner

# Real claude (burns quota; opt-in, periodic):
.venv/bin/python evals/run_all.py --cognition claude

# Subset:
.venv/bin/python evals/run_all.py --only blocked_planner,scope_grill_happy
```

Each run writes three artifacts under `evals/runs/sandbox-<scenario>-<ts>/`:

| File | Purpose |
|---|---|
| `trace.json` | Machine-diffable event log: every cognition call (role, model, prompt hash, latency), every tick (incoming lifecycle/phase → outcome), every dispatch / delivery / notification. |
| `timeline.md` | Human-readable per-event narrative — read this when something looks off. |
| `summary.json` | The metric: pass/fail, counts by category, expect-block evaluation. |
| `goal-state/` | Snapshot of the goal's on-disk artifacts (`STATUS.md`, `log.md`, `deliveries.md`, `discovery.md`, `spec.md`, `inbox.md`) so the chef's recorded "mind" is inspectable after the fact. |

### Scenarios

Each scenario is a YAML fixture under `evals/sandbox/scenarios/<id>.yaml`. The schema declares the cognition stub responses, the setup (goal definition / MCP call / grill idea), and an `expect` block listing the predicates the run must satisfy.

| id | mode | What it exercises |
|---|---|---|
| `single_task` | mcp | One direct `implement_feature` call — no goal layer. |
| `goal_existing_project` | goal | Full lifecycle against a repo that already exists; multi-tick. |
| `goal_new_project` | goal | Build-from-scratch on an empty workspace. |
| `scope_grill_happy` | grill | Vague idea → grill asks one question → finalizes spec. |
| `scope_grill_decides` | grill | Decide-instead-of-ask path: no questions, spec on turn 1. |
| `blocked_planner` | goal | Planner returns `decision=blocked` → phase blocked + owner ping. |
| `steered_goal` | goal | User calls `steer_goal` mid-run; next plan sees the steering. |
| `failing_verify` | goal | Task with a failing verify_cmd; chef must NOT open a PR. |
| `stuck_no_progress` | goal | Goal sits in executing for > NO_PROGRESS_S; watchdog fires once. |
| `quota_pause` | goal | Cognition raises usage-limit; layer pauses (zero tokens). |
| `done_gate_off_track` | goal | Planner proposes done → evaluator says off_track → goal continues. |

### Expect predicates

The `expect:` block evaluates against the captured trace + the goal's final on-disk state:

```yaml
expect:
  final_phase: blocked
  final_lifecycle: executing
  blocked_on_contains: "Which database"
  outcomes_contain: [blocked]
  cognition_by_role_min:
    goal_planner: 1
  counts_eq:
    deliveries: 0
  notify_owner_contains:
    - "needs you"
    - "Which database"
  log_contains:
    - "blocked: Which database"
```

Predicates supported: `final_phase`, `final_lifecycle`, `blocked_on_contains`, `outcomes_contain`, `cognition_by_role_eq`, `cognition_by_role_min`, `counts_eq` (ticks / cognition_calls / dispatches / deliveries / notifications), `counts_min`, `notify_owner_contains`, `log_contains`, `mcp_result_contains`, `grill_final_action`, `grill_questions_eq`, `grill_questions_min`.

Add a new scenario by dropping a YAML in `evals/sandbox/scenarios/`. No runner edits required.

## Real-pipeline harnesses

| Harness | What it measures |
|---|---|
| `measure_passrate.py` | Single-task pass rate on a real backend repo (`implement_feature` / `fix_bug` tasks on `lifekit-dashboard`, gated by `cd backend && dotnet test`, delivered as PRs). The June-15 must-have. |
| `measure_quality_todo.py` | Quality vs gate-greenness on harder tasks (ambiguous / multi-file / pure-UI tasks on `todo-fullstack-demo`; PRs reviewed adversarially after the gate passes). |
| `validate_review_gate.py` | Discrimination power of the pre-PR review gate. |
| `e2e_trace.py` | Single-goal live trace — points at a real `DEVCLAW_GOALS_DIR` and ticks one goal under the trace recorder. |
| `compare_engines.py` | Run the same task suite through OpenHands and the Claude-SDK engine, side by side, on real claude + docker. |

```bash
# inside the devclaw-mcp container or a host with claude + docker + the dotnet image:
.venv/bin/python evals/measure_passrate.py
.venv/bin/python evals/measure_quality_todo.py
DEVCLAW_REVIEW_MODEL=sonnet .venv/bin/python evals/validate_review_gate.py
.venv/bin/python evals/e2e_trace.py --mode live --goals-dir ~/memory/goals --goal-id <id>
.venv/bin/python evals/compare_engines.py --workspace /tmp/spike-ws --task '<the task>'
```

Each driver wires the engine exactly like the server does (`StateStore` + `TaskQueue(runner=run_sandcastle)`), so what it measures is what production behaves like. No mocks past the test boundary.

## What's missing on purpose

There's no harness for the build-from-scratch interview flow — the spec-kit elicitation (`build_project` / `answer_question` / `approve_spec`) and its `evals/run.py` golden-project harness were removed as drift (vault explicitly rejected the multi-pass spec-kit flow). Scope alignment now lives on the OpenClaw waiter via the `scope_grill` MCP tool; build-from-scratch is expressed as a normal goal with `done_when` (and an optional pre-grilled `spec`). Measure it through the durable goal layer instead.
