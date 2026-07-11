# Test-driven development

Use when building a feature or fixing a bug test-first. TDD is the red → green
loop; this skill makes the loop produce tests worth keeping.

If the repo has a `CONTEXT.md`, read it first so test names and interface
vocabulary match the project's domain language, and respect any ADRs
(`docs/adr/`) in the area you're touching.

## What a good test is

Tests verify behavior through public interfaces, not implementation details.
Code can change entirely; tests shouldn't. A good test reads like a
specification — "user can checkout with valid cart" tells you exactly what
capability exists — and survives refactors because it doesn't care about
internal structure. Expected values come from an independent source of truth
(a known-good literal, a worked example, the spec) — never recomputed the way
the code computes them.

## Seams — where tests go

A **seam** is the public boundary you test at: the interface where you observe
behavior without reaching inside. Tests live at seams, never against internals.

**Write the seams down before writing any test.** Derive them from the task
contract — the spec and its completion criteria name the behaviors that
matter; the seams are the narrowest public interfaces that exercise those
behaviors. If the contract leaves a seam genuinely ambiguous, pick the
narrowest public interface that covers the required behavior and record the
choice in your summary. You can't test everything — pinning seams up front is
how testing effort lands on the critical paths instead of every edge case.

## Anti-patterns

- **Implementation-coupled** — mocks internal collaborators, tests private
  methods, or verifies through a side channel (querying the database instead
  of using the interface). The tell: the test breaks when you refactor but
  behavior hasn't changed.
- **Tautological** — the assertion recomputes the expected value the way the
  code does (`expect(add(a, b)).toBe(a + b)`, a snapshot derived by hand the
  same way), so it passes by construction and can never disagree with the
  code.
- **Horizontal slicing** — writing all tests first, then all implementation.
  Bulk tests verify *imagined* behavior: you test the shape of things rather
  than user-facing behavior, and you commit to test structure before
  understanding the implementation. Work in **vertical slices** instead — one
  test → one implementation → repeat, each test a tracer bullet that responds
  to what the last cycle taught you.

## Rules of the loop

- **Red before green.** Write the failing test first and watch it fail, then
  write only enough code to pass it. A test you never saw fail proves nothing.
- **One slice at a time.** One seam, one test, one minimal implementation per
  cycle. Don't anticipate future tests or add speculative features.
- **Refactor after green, not during.** Once the slice passes, re-read the
  code with the quality bar in mind and clean it up while the tests hold you
  steady — but never weaken or delete a test to get there.

---
*Adapted from [mattpocock/skills](https://github.com/mattpocock/skills) (MIT © 2026 Matt Pocock).*
