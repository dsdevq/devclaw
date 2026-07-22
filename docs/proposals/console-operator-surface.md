# Console as a live operator surface — the node you watch AND command

**Status: DRAFT** — 2026-07-22. Direction not locked. No code before lock (spec
lifecycle). Reference artifact: a clickable mockup built this session (link in the
vault note `console-live-operator-vision-2026-07-22`).

> This proposal references, does not restate, the invariants in
> [`../../CLAUDE.md`](../../CLAUDE.md) and [`../architecture.md`](../architecture.md).
> One invariant is load-bearing here and called out explicitly in §4 (OAuth-only).

## Why

The console today is a **status board** — flat lists and tabs (goals, problems, an
Evals tab). But devclaw's *architecture* has a shape: one append-only event log,
five layers, a heartbeat, goals that own programs that own tasks. Denys's operator
mental model is that the console should have **that** shape — the whole running
node visible *from the top*, drilled *down* through a logical hierarchy, and
**commanded conversationally** — not a pile of tables. This proposal names that
surface so it becomes a real build target instead of evaporating (the exact drift
the spec lifecycle exists to prevent — a prior console idea already died this way;
see the operator-UX gap).

It is squarely a **packaging / legibility** move (the CV/portfolio scoreboard's
stated priority), not reliability hardening. A browser surface where you *watch an
autonomous loop grade itself and command it in natural language* is the single
strongest demo/portfolio artifact this system can produce.

## The shape (P1 → P3 slices; only P1 is firmed)

The IA is one drill-down spine, with the same disclosure rule at **every tier**:
**show active + planned in full; fold the settled into a summary you can open.**

```
NODE (the running instance)
  → PROJECT (→ GitHub)
    → GOAL (lifecycle + direction)
      → MILESTONE  (= plan slice / plan_key — NOT a new entity; see §5 [OPEN])
        → TASK (token/time/verdict stats)
```

Plus two cross-cutting surfaces and one control surface:

- **The layer trace** — one tick/task rendered hop-by-hop through the 5 layers,
  with a failure (e.g. a `TransitionConflict`) highlighted *in place* and shown
  recovering. Backed by `traces` + `events`.
- **Health / self-evaluation** — charts over `eval_outcomes`, `cycle_reports`,
  `failure_class`: pass rate, **clean-cycle rate** (the done-criterion), error-class
  breakdown, token spend (estimated). Overall + per-project + per-milestone.
- **Problem lifecycle / self-resolution** — trace each `problems` entry
  *identified → filed → fixing → resolved*, with when and how it was identified.
  This is the visible form of the self-improving cycle (ADR 0006 cycle-report +
  self-issue-filing, `proposals/self-issue-filing.md`).
- **Co-pilot chat** — a conversational control panel that operates the console and
  devclaw over the MCP (§4).

### Slices

- **P1 — the read hierarchy + trace** *(firm this first)*. Node view (heartbeat +
  5 organs + vitals + goal population + clean/wedge), project-grouped fleet with
  GitHub links, goal → milestone(slice) → task drill-down with active/archived
  disclosure, and the layer trace. All read-only, all over projections that
  already exist. **Ships value alone** — it's the operator "what's going on"
  surface, independently testable. Sizing: firm at P1 lock (a handful of console
  PRs; end-of-week cap), not estimated here.
- **P2 — health + problem-lifecycle dashboard**. The charts + the problem tracker.
  Named, unsized until P1 lands.
- **P3 — the co-pilot chat**. The conversational control surface. Named, unsized;
  carries the §4 constraint and the largest `[OPEN]` set.

## §4 — The load-bearing constraint: the chat is the *waiter*, over OAuth

The co-pilot chat MUST NOT spin up a fresh agent on the **Anthropic API**. That
would break the **OAuth-only / no-metered-billing invariant** (actively stripped in
`llm_call.py`, `engine/host.py`, `engine/sandcastle.py`, `openhands-runner/runner.py`)
and add a per-message billing surface. The chat's cognition is the **existing
OpenClaw waiter** — the component that already translates chat → devclaw MCP calls
over the OAuth/Pro auth. The console embeds a *panel* onto that agent; it does not
mint a new one.

Console *navigation* is driven by a **clean intent API** (`open(goal)`,
`filter(...)`, `select(project)`, `create_goal(...)`), NOT literal browser
computer-use. We own the console; the agent emits *what*, the console does *how* —
faster, reliable, testable, and it doesn't break when a button is restyled.
Computer-use is for apps we don't control; this isn't one.

## §5 — `[OPEN]` items (the clarify step — resolve before LOCK)

1. **`[OPEN]` Is "milestone" a real new entity, or a rendering of `plan_key`
   grouping?** Recommendation: **`plan_key` grouping** (a plan slice already groups
   tasks; no new table, nothing to drift). Confirm — or decide milestones warrant a
   first-class concept with its own description/stats persisted.
2. **`[OPEN]` P1 boundary.** Does P1 include the health dashboard, or is that
   strictly P2? Recommendation: **trace in P1, charts in P2** (charts are a distinct
   build; the read hierarchy ships value without them).
3. **`[OPEN]` Chat scope — global vs per-goal.** Global (create/steer anything) and
   a per-goal steer box are both desirable. Which lands first in P3?
4. **`[OPEN]` How far does the intent API reach?** Read/navigate only, or also
   mutate (create/steer/cancel/answer) from the browser? Mutation needs the
   console's auth story pinned (tailnet today).
5. **`[OPEN]` Self-*resolution* honesty.** The problem-lifecycle UI shows a loop
   that is *partly* built: file is Stage-1-locked (`self-issue-filing.md` #347), but
   *fix* is propose-only-on-self, human-merges — NOT full auto. The UI must mark the
   auto-fix stage as **gated**, never imply autonomy that isn't there. Confirm the
   labeling contract.
6. **`[OPEN]` Token stat honesty.** Tokens are *estimated* (traces cost estimate);
   time and verdict are real. Every chart labels estimated-vs-real. Confirm.
7. **`[OPEN]` Does this reopen the helper-vs-PoC question?** A commandable console
   is close to the "daily helper" devclaw deliberately isn't. Is this packaging
   (watch + light control) or a pivot toward a daily driver? Recommendation:
   **packaging** — augment direct manipulation, don't replace the fire-and-forget
   thesis.

## Out of scope / non-goals

- Not a reliability change — read surfaces + a control panel over existing state.
- Not a new cognition path on any tick (charts/problem-lifecycle are arithmetic
  over projections; the chat reuses the waiter). The zero-token idle guard is
  untouched.
- Not computer-use browser automation (§4).
