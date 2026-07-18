# Verify-gate coverage (the lesson)

The `verify_cmd` you'll see in this repo's AGENTS.md (or in the goal) is the single command the done-gate runs to decide if your change is acceptable. **Tests the gate doesn't run might as well not exist** — so if you introduce a new test layer, the verify gate must run it.

Concrete failure mode: you add `e2e/*.spec.ts` Playwright tests that pass by hand, but `verify_cmd` runs only `pytest -q`. The gate goes green having run nothing new; the grounded evaluator sees no Playwright run and rejects with `direction=off_track`.

Before you finish:

1. If you added tests at a layer the existing `verify_cmd` doesn't run, **update `verify_cmd`** (in AGENTS.md, the CI workflow, wherever this repo declares it) to include the new layer.
2. If a build step is needed before the new tests run (e.g. `npm run build` before serving a SPA), include that step in the gate too.
3. Record in your final summary which test layers the gate now covers, so the evaluator can verify against it directly.

## Web-UI changes are gated in a real browser

If your change touches an **app-surface** web-UI path (`*.component.ts`, `*.component.html`, `src/app/**`, `angular.json`), the host **browser gate** fails it **closed** unless the verify gate ran a passing Playwright suite over it — unit tests + a build are not enough, because they never render the integrated app. The gate keys off a machine-readable **Playwright JSON report** (executed count > 0, zero failures), not a string-match of `verify_cmd`.

**Library-only exemption:** a change whose UI paths are ALL library surface (`src/lib/**` — a library component not wired into any running app route) is exempt from the browser gate. Do **not** write a full-app Playwright spec for a library-only slice — its proof is its unit test + story, which the library build/test gate runs. The browser proof for that component lands later, on the app-side diff that wires it into a route (that diff touches app surface, so the gate fires then). If your diff mixes library and app paths, the gate is required as usual.

So for an app-surface UI change: add a `@playwright/test` spec that exercises it in the running app, and make `verify_cmd` run `npx playwright test --reporter=json`. See `craft/playwright.md` for the config, the `webServer` boot, the console-error listeners, and the reporter-artifact contract.
