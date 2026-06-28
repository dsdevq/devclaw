# Verify-gate coverage (the lesson)

The `verify_cmd` you'll see in this repo's AGENTS.md (or in the goal) is the single command that the done-gate runs to decide if your change is acceptable. **If you introduce a new test layer, the verify gate must run it.**

Concrete failure mode this rule prevents:

- You add `e2e/*.spec.ts` Playwright tests. They pass when you run them by hand.
- The verify gate runs only `pytest -q`. It does not call `npx playwright test`.
- The gate is green. The done-gate's grounded evaluator inspects the gate output, sees no Playwright run, and rejects with `direction=off_track`.

Before you finish:

1. If you added tests at a layer the existing `verify_cmd` doesn't run, **update `verify_cmd`** (in AGENTS.md, in the CI workflow, in whatever file declares it for this repo) to include the new layer.
2. If a build step is needed before the new tests run (e.g. `npm run build` before serving a SPA), include that step in the gate too.
3. Record in your final summary which test layers the gate now covers, so the evaluator can verify against it directly.

Tests the gate doesn't run might as well not exist.
