# End-to-end browser testing with Playwright

The sandbox ships Chromium pre-installed and `@playwright/mcp` available, and the verify gate runs whatever your `verify_cmd` is. If your change touches a web UI — pages, forms, navigation, anything a user would click — **you MUST cover it with a Playwright spec**. This is a hard rule enforced by the E2E coverage gate (see `35-e2e-coverage.md`): a UI diff without a spec is automatically blocked and fed back to you for retry.

## What you have

- Chromium binary at `/home/agent/.cache/ms-playwright/chromium-*/` (or `/home/node/.cache/...` in the devclaw-mcp runtime).
- `@playwright/mcp@latest` installed globally; `/workspace/.mcp.json` is auto-configured so claude can call the Playwright MCP tools directly.
- All required system libs (libnss3, libxkbcommon0, etc.) are present — `chromium.launch()` works without extra apt installs.

## Two ways to use Playwright

1. **Via the MCP tool** (interactive, during your task): call the Playwright MCP to navigate, click, screenshot, dump console errors. Use this to *exercise* the UI yourself before claiming a flow works — but this is not coverage. MCP calls don't satisfy the E2E gate.
2. **Via committed `@playwright/test` specs** (durable, gates regressions): write `.spec.ts` files. This is what the E2E coverage gate checks for, and what the verify gate re-runs every task.

## File convention (what the gate expects)

Use `*.spec.ts` (or `.tsx/.js/.jsx`) — Playwright's default. `*.e2e.ts` also counts. **`.test.ts` does NOT count** — that's the unit-test convention, and a Vitest unit on a button component doesn't prove the page works end-to-end.

A typical layout:

```
playwright.config.ts          # at repo root; webServer block boots the app
e2e/
├── auth.spec.ts
├── dashboard.spec.ts         # one spec per page
├── accounts.spec.ts
└── regression/<bug>.spec.ts
```

## What a spec must actually do

The coverage gate is mechanical (it checks the file is there + has `test(`/`it(`/`describe(` calls), but the *quality bar* is real user flows, not direct-nav assertions. A passing-but-useless spec ("page loaded with status 200") is still a green run that ships broken product.

Each spec for a page should:

1. **Navigate** to the route via `await page.goto('/...')`.
2. **Interact** like a user — fill the form, click the button, create the entity, submit. Tests that only visit pages don't catch the failure modes that matter.
3. **Assert the user-visible result** with Playwright's `expect()` matchers — text appeared, route changed, list contains the new item.
4. **Attach listeners that fail on JS errors**: `page.on('pageerror', e => { throw e })` and a `console.error` filter. A spec that passes with red console messages catches nothing.

```ts
test('user can create an account', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });

  await page.goto('/accounts');
  await page.getByRole('button', { name: 'New account' }).click();
  await page.getByLabel('Name').fill('Acme Corp');
  await page.getByRole('button', { name: 'Save' }).click();

  await expect(page.getByRole('row', { name: 'Acme Corp' })).toBeVisible();
  expect(errors).toEqual([]);
});
```

## What gets the gate to pass

- Diff contains `*.spec.ts` (or `.e2e.ts`) **and**
- The spec has at least one `test(` / `it(` / `describe(` call **and**
- `verify_cmd` runs `npx playwright test` (or the project's equivalent) so the spec actually executes.

A spec file you added but the gate doesn't run is worthless. After adding browser tests, **read the verify-gate skill** — the gate must run Playwright, not just pytest.

## When the project uses pytest-playwright (not @playwright/test)

The E2E coverage gate is opinionated on the `*.spec.ts` convention. If this project is committed to `pytest-playwright` instead, the gate must be disabled at the project level (`DEVCLAW_E2E_COVERAGE_GATE=0`). Don't try to dodge it by renaming files.
