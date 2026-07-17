# End-to-end browser testing with Playwright

Read this when the change touches a web UI — pages, forms, navigation, anything a user would click. The sandbox image ships Chromium pre-installed and the `@playwright/mcp` server available.

## What you have

- Chromium binary at `/home/agent/.cache/ms-playwright/chromium-*/` (or `/home/node/.cache/...` in the devclaw-mcp runtime).
- `@playwright/mcp@latest` installed globally; `/workspace/.mcp.json` is auto-configured so claude can call the Playwright MCP tools directly.
- All required system libs (libnss3, libxkbcommon0, etc.) are present — `chromium.launch()` works without extra apt installs.

## Two ways to use it

1. **Via the MCP tool** (interactive, during your task): navigate, click, screenshot, dump console errors. Use this to *exercise* the UI yourself before claiming a flow works.
2. **Via committed `@playwright/test` specs** (durable, gates regressions): write `.spec.ts` files in `e2e/` (or wherever the project's convention puts them) so the verify gate can re-run them.

## When the goal asks for E2E coverage

Default to **TypeScript `@playwright/test`** unless the project already uses `pytest-playwright`. A typical setup:

```
playwright.config.ts          # at repo root; webServer block boots the app
e2e/
├── auth.spec.ts
├── navigation.spec.ts
├── <feature>.spec.ts
└── regression/<bug>.spec.ts
```

Always attach `page.on('pageerror')` and `page.on('console')` listeners that fail the test on uncaught JS errors or `console.error`. A test that passes with red console messages is a test that catches nothing. (This is exactly the class of bug the gate exists for: an Angular component that unit-tests green but throws `NG05105`/a provider error the instant it renders in the real app.)

After adding browser tests, the verify gate must run `npx playwright test`, not just pytest — see the always-on verify-gate-coverage doctrine for the rule.

## The browser gate needs machine-readable proof it RAN

The host browser-gate fails a web-UI change **closed** unless it sees a Playwright run that actually executed (expected + unexpected > 0) and passed. It does not read your prose or grep `verify_cmd` for the word "playwright" — it parses a JSON report. So the run must be a **JSON-reporter** run:

- Run the suite as `npx playwright test --reporter=json` (or set `reporter: 'json'` in `playwright.config.ts`). The runner exports `PLAYWRIGHT_JSON_OUTPUT_NAME` pointing at the devclaw-owned artifact path — a `--reporter=json` run writes its `stats` there automatically, so you do **not** hardcode a path.
- The gate reads `stats.expected/unexpected/flaky/skipped`. A run where everything is `skipped` (0 executed) counts as **never ran** and fails closed — a suite that skips itself proves nothing.
- The `webServer` block in `playwright.config.ts` must actually boot the app (`ng serve`, or `npm run build` then a static server). If the backend can't run in-sandbox (no .NET SDK is baked), stub it with `page.route(...)` — a frontend-only boot still catches the render/console-error class.
