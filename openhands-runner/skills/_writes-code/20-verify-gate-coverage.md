# Verify-gate coverage (the lesson)

The `verify_cmd` you'll see in this repo's AGENTS.md (or in the goal) is the single command the done-gate runs to decide if your change is acceptable. **Tests the gate doesn't run might as well not exist** — so if you introduce a new test layer, the verify gate must run it.

Concrete failure mode: you add `e2e/*.spec.ts` Playwright tests that pass by hand, but `verify_cmd` runs only `pytest -q`. The gate goes green having run nothing new; the grounded evaluator sees no Playwright run and rejects with `direction=off_track`.

Before you finish:

1. If you added tests at a layer the existing `verify_cmd` doesn't run, **update `verify_cmd`** (in AGENTS.md, the CI workflow, wherever this repo declares it) to include the new layer.
2. If a build step is needed before the new tests run (e.g. `npm run build` before serving a SPA), include that step in the gate too.
3. Record in your final summary which test layers the gate now covers, so the evaluator can verify against it directly.

For the browser-E2E how-to (Playwright specs, config, console-error listeners), see `craft/playwright.md`.
