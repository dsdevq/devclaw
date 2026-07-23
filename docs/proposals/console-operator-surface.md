# Console as a live operator surface — the node you watch AND command

**Status: LOCKED (P1 direction)** — 2026-07-23. The P1 slice (read hierarchy +
layer trace) is firm and clear to build; **P2 and P3 stay named-but-unsized**
until P1 lands. All eight §5 `[OPEN]`s are resolved or explicitly deferred-to-slice
with an owner (below). Locking is direction, not schedule — sequencing stays
Denys's call. Reference artifact: a clickable mockup built 2026-07-22 (link in the
vault note `console-live-operator-vision-2026-07-22`); per `[OPEN] #8` the mockup
is a demo-of-everything, **not** the target IA.

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

Its **primary** value is packaging / legibility (the CV/portfolio scoreboard's
stated priority), not reliability hardening. A browser surface where you *watch an
autonomous loop grade itself and command it in natural language* is the single
strongest demo/portfolio artifact this system can produce.

**Direction note (`[OPEN] #7`, resolved 2026-07-23):** Denys chose to **lean this
toward a commandable daily driver**, not treat it as packaging-only. The read
hierarchy (P1) is unaffected either way, but the control surfaces (P3 chat, the
intent API's mutation reach) are now explicitly a step toward the direct-use
"helper" devclaw's v1 was — reconciling the helper→PoC drift rather than holding
the fire-and-forget thesis as the sole frame. Named follow-on: **re-surface the v1
task-runner** (`one_shot` / `dispatch_task`); the known structural snag is
branch-off-main (see the vault note `helper-to-poc-drift-2026-07-22`). This lean is
a direction signal for P2/P3, not a committed architecture — it graduates through
its own firm/lock when those slices are scheduled.

## The shape (P1 → P3 slices; only P1 is firmed)

The IA is one drill-down spine, with the same disclosure rule at **every tier**:
**show active + planned in full; fold the settled into a summary you can open.**

```
NODE (the running instance)
  → PROJECT (→ GitHub)
    → GOAL (lifecycle + direction)
      → MILESTONE  (= plan slice / plan_key grouping — NOT a new entity; §5.1 RESOLVED)
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

## §5 — clarify items (all resolved / deferred-to-slice 2026-07-23 → LOCK)

Every item below is either **RESOLVED** (answered, folded into the P1 spec) or
**DEFERRED** to the slice it belongs to, with a named owner. Per the spec
lifecycle, a deferral-with-owner is a valid resolution of a clarify `[OPEN]` — none
remain open, so the P1 direction is clear to lock.

1. **RESOLVED — milestone = `plan_key` grouping.** A plan slice already groups its
   tasks; the console renders that grouping. No new table, no persisted
   description/stats, nothing to drift. Milestone is a *view*, not an entity.
2. **RESOLVED — P1 = read hierarchy + layer trace; charts are P2.** P1 is the node
   view + project→goal→milestone→task drill-down (active/settled disclosure) + the
   layer trace. The health/self-eval **charts and the problem-lifecycle tracker are
   strictly P2** — a distinct build the read hierarchy ships value without. This is
   the locked P1 boundary.
3. **DEFERRED → P3** (owner: Denys at P3 firm). Chat scope (global create/steer vs a
   per-goal steer box, and which lands first) is decided when P3 is scheduled. Not a
   P1 concern.
4. **RESOLVED — P1 is read/navigate-only; mutation reach is a P3 decision.** The P1
   intent API is `open` / `filter` / `select` only. Browser mutation
   (create/steer/cancel/answer) — and the console auth story it requires (tailnet
   today) — is pulled into P3 with the chat, not P1. Keeps P1's surface clean and
   testable.
5. **DEFERRED → P2** (owner: Denys at P2 firm). The self-*resolution* honesty
   labeling contract (mark the auto-*fix* stage **gated**, never imply autonomy that
   isn't there — file is Stage-1-locked, fix is propose-only/human-merges) is pinned
   when the problem-lifecycle UI is actually built in P2.
6. **DEFERRED → P2** (owner: Denys at P2 firm). Token-stat honesty (label
   estimated-vs-real on every chart) is a charts concern — decided when the charts
   land in P2.
7. **RESOLVED — lean toward a commandable daily driver** (Denys, 2026-07-23), *not*
   packaging-only. Does not move the P1 read hierarchy; it reframes the P2/P3 control
   surfaces as a step toward direct daily use and lights up the **re-surface-v1-helper**
   follow-on (snag: branch-off-main). Captured as the "Direction note" under **Why**
   and in the non-goals below. It graduates through its own firm/lock at P2/P3.
8. **RESOLVED — not one mega-page; split into proper screens/routes/tabs** (Denys,
   2026-07-22). The reference mockup concentrates every surface on one scroll to
   *show* the whole vision at once; the shipped console must decompose into real
   screens (overview vs goal-detail vs health/problems). The mockup's single-page
   layout is **not** the target IA — the layout is rethought at build time.

## Out of scope / non-goals

- **P1 is read-only.** No mutation from the browser in P1 — that reach is a P3
  decision (§5.4). P1 adds no write surface.
- Not a reliability change — read surfaces + (later) a control panel over existing
  state.
- Not a new cognition path on any tick (charts/problem-lifecycle are arithmetic
  over projections; the chat reuses the waiter). The zero-token idle guard is
  untouched.
- Not computer-use browser automation (§4).
- **Not (yet) a thesis rewrite.** The `[OPEN] #7` daily-driver lean is a recorded
  *direction* for P2/P3, not a committed pivot; the fire-and-forget durable-goal
  thesis stays intact until a P2/P3 firm says otherwise. P1 does not depend on it.
