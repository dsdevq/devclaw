# quality — a fail-closed adversarial gate for agent-written code

A pre-PR gate that **refuses to approve when it can't produce a verdict**.

Green tests are not trust. An autonomous coding agent can pass its own verify
gate while shipping a regression, weakening the very tests that judge it, or
leaving a "working" UI change that no browser ever rendered. This package is
the layer past green: an independent, adversarial review of the actual diff,
with one non-negotiable stance — **when in doubt, or in error, the answer is
NO**. A crash in the gate is a failed task, never an approval (#186).

That stance is what separates this gate from the common alternatives in
agent-loop tooling: trusting the agent's own "done" self-report, or deferring
every merge to a human click. The gate produces a grounded verdict, and its
failure modes are all closed.

## What's in the box

| Module | Job |
|---|---|
| `__init__.py` | The adversarial diff-review gate: `review_diff` (one reviewer), `review_panel` (N reviewers under **diverse lenses** — correctness, regression risk, acceptance criteria — with unioned blocking issues and fail-closed sub-quorum), `format_feedback` (verdict → retry brief). Includes the cognition-timeout **degradation ladder**: an oversized diff is split per file and the verdicts unioned, still fail-closed end to end (#281). |
| `browser_gate.py` | Browser-E2E verification: a change touching web-UI paths must carry a passing **real-browser** Playwright run in the verify output, or it fails closed. Pure parsing — no LLM. |
| `reachability.py` | The gate's grounded escape valve: an independent judge may clear a browser-gate block **only** on an affirmatively proven "this UI isn't rendered in the running app". Uncertain / crash / reachable → the block stands. |
| `eval_judge.py` | Failure-analysis judge for eval runs — turns "what went wrong?" into a tagged verdict. |
| `evals.py` | Pure scoring functions for grading a build-from-scratch run. No LLM. |
| `prompts/` | The gate's own prompt templates + loader — inside the boundary, so the package renders its verdicts without devclaw's prompt dir. |

Related, one directory over: `devclaw/loom/` — the pure-stdlib substrate the
gate leans on (`test_integrity.scan_diff` detects test-weakening;
`limits.classify_failure` keeps a quota hit from being mistaken for a defect).

## Design rules (the contract)

1. **Fail closed, always.** No verdict ⇒ no approval. An exception, an
   unparseable response, a sub-quorum panel — each settles the task failed.
2. **Fail fast when retry is futile.** An unreviewable diff (too large,
   crashes the reviewer identically every time) fails **without** burning the
   agent-retry budget, carrying an actionable reason instead of looping.
3. **Grounded, never remembered.** Every judging prompt carries a
   `REPOSITORY CONTEXT` block snapshotting the *actual* workspace and forbids
   inference from any repo the model has seen before (#227). Fixing a bad
   verdict means grounding the reasoning — never weakening the gate.
4. **Evidence wins.** Panel verdicts union blocking issues; one panelist's
   proven defect blocks regardless of the other votes.
5. **The gate reads the change; it never trusts the changer.** Inputs are the
   diff, the verify output, and the workspace snapshot — not the agent's
   summary of them.

## Seams (how it stays portable)

The gate's only internal dependencies are deliberate leaf modules:

- **LLM caller** — `devclaw.llm_call.claude_with_model(model, role=…)`
  produces the one-argument async caller each judge binds at import. The
  consumer can inject any `Callable[[str], Awaitable[str]]` in its place
  (`TaskQueue` injects `reviewer=` / `reachability_judge=` in tests).
- **Model tiers** — `devclaw.model_tiers.model_for(role)` maps a role to a
  model alias; a two-line table, trivially replaced.
- **Substrate** — `devclaw.loom` (pure stdlib).

Nothing here imports the planner, the task queue, the goal layer, or the
state store — pinned by `tests/test_quality_package.py`. When this package
moves to its own repo, those three seams are the entire integration surface.

## How devclaw consumes it

One call site: `task_queue.py` runs the gates in the settle path, in order —
verify gate (green tests) → `test_integrity` (nobody weakened the tests) →
`review_panel` (adversarial review) → `browser_gate` (+ `reachability`
escape valve). Any failure feeds back into the retry brief; the terminal
failure escalates to a human. Tests: `tests/test_review_gate*.py`,
`tests/test_review_panel.py`, `tests/test_browser_gate*.py`,
`tests/test_eval_judge.py`, `tests/test_quality_package.py`.
