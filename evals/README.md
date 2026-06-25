# DevClaw evals

Three measurement harnesses against the **real** pipeline (claude + docker + OpenHands). Each drives the engine end-to-end, captures structured results into `evals/runs/`, and exits with a number you can compare across git SHAs.

| Harness | What it measures | Suite |
|---|---|---|
| `measure_passrate.py` | Single-task pass rate on a real backend repo | A basket of `implement_feature` / `fix_bug` tasks on `lifekit-dashboard`, gated by `cd backend && dotnet test`, delivered as PRs. The June-15 must-have. |
| `measure_quality_todo.py` | Quality vs gate-greenness on harder tasks | Deliberately ambiguous / multi-file / pure-UI tasks on `todo-fullstack-demo`. Each PR is reviewed adversarially after the gate passes. Step 1 of "make green mean trustworthy." |
| `validate_review_gate.py` | Discrimination power of the pre-PR review gate | The pre-PR review gate fed the three real "green" diffs from the passrate run (false-positive rate) plus two synthetic bad diffs (dead code; happy-path-only tests). Measures both arms — does it pass good diffs, does it catch bad ones. |

Each driver wires the engine exactly like the server does (`StateStore` + `TaskQueue(runner=run_sandcastle)`), so what it measures is what production behaves like. No mocks past the test boundary.

## Run

```bash
# inside the devclaw-mcp container or a host with claude + docker + the dotnet image:
.venv/bin/python evals/measure_passrate.py
.venv/bin/python evals/measure_quality_todo.py
DEVCLAW_REVIEW_MODEL=sonnet .venv/bin/python evals/validate_review_gate.py
```

Output lands in `evals/runs/<harness>-<timestamp>.json` next to a one-line stderr summary (`passrate=5/5`, etc).

## What's missing on purpose

There's no harness for the build-from-scratch interview flow — the spec-kit elicitation (`build_project` / `answer_question` / `approve_spec`) and its `evals/run.py` golden-project harness were removed as drift (vault explicitly rejected the multi-pass spec-kit flow). Scope alignment now lives on the OpenClaw waiter via the `scope_grill` MCP tool; build-from-scratch is expressed as a normal goal with `done_when` (and an optional pre-grilled `spec`). Measure it through the durable goal layer instead.
