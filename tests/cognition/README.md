# Cognition evals — verify the *judgment*, not just the mechanism

**Why this exists.** DevClaw has extensive mechanism tests (does the planner return valid JSON, does the evaluator handle edge cases). It had **zero cognition tests** — nothing answered "is the evaluator's verdict *right*?" or "is the planner's next action *good*?". The 2026-06-29 done-gate truncation incident is the most expensive case: a defect in cognitive quality only surfaced after 10+ hours of goal-blocking, because mechanism tests passed throughout — the defect was inside the cognition output.

Per-module evals collapse the iteration loop for cognitive changes from *hours-per-real-goal* to *minutes-per-fixture*. See `~/memory/projects/devclaw/plan.md` §Per-module evals for the durable design.

## What lives here

```
tests/cognition/
├── README.md                       (you are here)
├── __init__.py
├── harness.py                      loader + runner primitives
├── fixtures/
│   └── evaluator/
│       ├── achieved_clean.json           happy-path done-gate: real evidence, expects `achieved`
│       ├── off_track_missing_clause.json real evidence for 3/4 clauses; expects downgrade to `off_track`
│       └── off_track_stub_disguise.json  clause satisfied by an unauthorized stub; expects `off_track`
└── test_evaluator_evals.py         mechanism guards (always) + live cognition run (opt-in)
```

## Two run modes

**1. Mechanism guards — always on (normal `pytest`).** For every fixture, verify:

- the JSON parses into a fully-realized `Goal` + `GoalStatus`,
- `build_prompt()` produces a non-empty prompt that mentions the goal's objective and every done_when clause,
- `validate()` on the fixture's canned "expected model output" reproduces the fixture's `expected.verdict`.

This proves the fixture is *coherent* and that the evaluator's parsing/validation layer round-trips it correctly. Costs zero quota; runs in ~ms; caught by CI.

**2. Live cognition run — opt-in, quota-burning.** Set `DEVCLAW_RUN_COGNITION_EVALS=1` and pytest will call the real `evaluate()` against each fixture using the production evaluator (`goal/evaluator.py`) bound to Anthropic. It prints the verdict + rationale + corrections + clauses for each fixture next to the fixture's expected verdict. **The human reads the diffs and judges.**

There is no automated pass/fail gate on the live run yet — premature automation is worse than none. When we've eyeballed enough live outputs to know the shape of "correct" for this module, we can add a judge-LLM. Not before.

```bash
# CI-safe (mechanism-only, ~ms):
pytest tests/cognition/

# Live cognition (burns Anthropic quota, opt-in):
DEVCLAW_RUN_COGNITION_EVALS=1 pytest tests/cognition/ -s

# One fixture, one live call, output pretty-printed:
DEVCLAW_RUN_COGNITION_EVALS=1 pytest tests/cognition/test_evaluator_evals.py::test_live_cognition_run -s -k achieved_clean
```

## Fixture schema

Every fixture is a JSON file matching this shape:

```json
{
  "name": "achieved_clean",
  "module": "evaluator",
  "source": "reconstructed | hand-crafted | production-trace",
  "notes": "One-line context on why this fixture exists.",
  "inputs": {
    "goal": {
      "id": "...",
      "objective": "...",
      "done_when": "...",
      "backlog": ["..."],
      "stub_acceptable": ["..."],
      "cadence": "2h",
      "workspace_dir": "/repos/example"
    },
    "status": {
      "phase": "verifying",
      "lifecycle": "executing",
      "next": "..."
    },
    "recent_log": "...",
    "deliveries": "...",
    "review_report": "## Per-clause evidence\n...",
    "at_done_gate": true,
    "spec": ""
  },
  "expected": {
    "verdict": "achieved",
    "must_contain_rationale_hints": [],
    "must_contain_correction_hints": [],
    "notes": "Why this is the right verdict."
  },
  "canned_model_output": {
    "verdict": "achieved",
    "rationale": "...",
    "clauses": [{"clause": "...", "satisfied": true, "evidence": "..."}]
  }
}
```

- **`inputs`** — everything `evaluate()` needs, plus enough context to reconstruct the state.
- **`canned_model_output`** — what a well-behaved model *would* return; the mechanism guard feeds this through `validate()` to verify the parsing/normalization layer keeps the fixture's expected verdict.
- **`expected`** — the human-graded target verdict + any substrings the rationale/corrections should contain when the live model does its own reasoning. Kept qualitative; no strict equality.

## Boundary rules

- Fixtures live in-repo, git-tracked. Every change is a PR-reviewable diff.
- Live output diffs go to stdout for the human; **no automated pass/fail on the live run** until a manual baseline exists.
- When fixture inputs change shape (new `Goal` field, new prompt section), fixtures are regenerated as a deliberate PR — same discipline as a schema migration.
- Cap at ~5 fixtures per module for v1. Live-mode CI would burn quota; the opt-in env flag keeps it manual.

## Adding a new fixture

1. Copy an existing fixture in `fixtures/evaluator/`.
2. Fill in `inputs` from a real production trace when possible (mark `source: "production-trace"`). Reconstruction from journal notes is OK (mark `source: "reconstructed"`).
3. Fill in `expected.verdict` — what a competent human would judge given the same inputs.
4. Fill in `canned_model_output` — a plausible well-behaved model response that yields `expected.verdict` after `validate()`.
5. Run `pytest tests/cognition/ -k <your_fixture_name>` — the mechanism guards should pass.
6. Run `DEVCLAW_RUN_COGNITION_EVALS=1 pytest tests/cognition/ -s -k <your_fixture_name>` — read the live output.

## Extending to other modules

The pattern is the same for planner, decomposer, done-gate review. Each gets:

- `fixtures/<module>/<scenario>.json`
- a harness function in `harness.py` that turns fixture inputs into the module's live callable arguments
- one guard test + one live-cognition test in `test_<module>_evals.py`

Do the evaluator first (highest leverage, smallest input surface), then use its shape as the template.
