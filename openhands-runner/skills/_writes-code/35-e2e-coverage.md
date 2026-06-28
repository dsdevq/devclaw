# E2E coverage gate

Before any PR ships, the diff is scanned. If you modified or added UI files
(`*.tsx`, `*.jsx`, `*.vue`, `*.svelte`, `*.html`, `*.css`, `*.scss`) and your
diff did NOT also add or modify a Playwright spec (`*.spec.ts` / `*.e2e.ts` and
their `.tsx/.js/.jsx` variants), the gate **blocks** and your task is retried
with the gap fed back to you.

This is mechanical — no judgment is involved. The gate is regex on the diff,
not an LLM. So you can't argue it down; you have to ship the spec.

## What satisfies the gate

1. A Playwright spec file is added or modified in the same diff as the UI change.
2. New spec files contain at least one `test(`, `it(`, or `describe(` call —
   an empty spec ("TODO write the tests") fails the substance check.
3. The verify_cmd runs Playwright so the spec actually executes (see `20-verify-gate.md`).

That's it. Three checks. All trivially defensible.

## What does NOT satisfy the gate

- **`.test.ts` unit tests.** A Vitest test on a button component is not E2E
  coverage of the page. Use `.spec.ts` for Playwright; the gate intentionally
  excludes `.test.ts`.
- **An MCP-driven manual exercise during your task.** Calling Playwright MCP to
  poke at the UI doesn't commit a spec. Coverage requires a committed file.
- **A spec for a different page.** "I shipped a spec last week" doesn't help —
  the gate scans THIS diff. If your change touches a page, the diff must touch
  that page's spec.
- **Renaming the UI file** to dodge the heuristic. The diff still shows the UI
  extension on the new path. The gate sees it.
- **Deleting an existing spec** to "clean up" or "consolidate". Test-integrity
  catches that separately; doubly blocked.

## When you genuinely don't need a spec

Two cases:

1. **Backend-only change** (`.py`, `.go`, `.ts` in `server/`). The gate already
   skips — it only fires on UI extensions.
2. **The project is committed to pytest-playwright**, not @playwright/test.
   The maintainer must disable the gate at the project level
   (`DEVCLAW_E2E_COVERAGE_GATE=0`); not your call as the agent. If you think
   this applies, surface the question instead of working around it.

## Quality bar within the spec

The gate's substance check is mechanical (one `test(` call satisfies it), but a
green spec that asserts nothing is still a green run shipping broken product.
For every page your change touches, the spec should:

1. **Navigate** with `page.goto('/...')`.
2. **Interact** with the page like a real user (click, fill, submit, create).
3. **Assert the user-visible outcome** with `expect(...)` — text appeared,
   route changed, the new entity is in the list.
4. **Capture JS errors and console.error** and fail the test on either:
   `page.on('pageerror', e => { throw e })` and a console-error listener.

A spec that only does `await page.goto('/dashboard'); await expect(page).toHaveTitle(/.+/)` is technically a spec, but it catches nothing meaningful. The agent that ships that work is the agent that ships broken product. Don't be that agent.
