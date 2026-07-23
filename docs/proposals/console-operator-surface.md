# Console as a live operator surface — the node you watch AND command

**Status: P1 → [ADR 0008](../decisions/0008-console-operator-surface-p1.md), P2 → [ADR 0009](../decisions/0009-console-operator-surface-p2.md), P3.1 (§6) → [ADR 0010](../decisions/0010-console-p3-structured-blocks.md) — all GRADUATED**
— 2026-07-23. P1 (read hierarchy + layer trace) + P2 (health + problem-lifecycle)
shipped. P3 is **sliced**: **P3.1 = §6 structured decision blocks** (planner emits
options at block time → console renders click-to-steer; frozen in ADR 0010, which
resolves §6a–§6d) is scheduled; **P3.2 = the co-pilot chat + OpenClaw-waiter-over-OAuth
integration (§4) + §5.3 chat-scope remains LOCKED-direction, unsized** (a distinct,
larger undertaking). All §5 `[OPEN]`s resolved. Reference artifact:
a clickable mockup built 2026-07-22 (link in the vault note
`console-live-operator-vision-2026-07-22`); per `[OPEN] #8` the mockup is a
demo-of-everything, **not** the target IA.

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
- **P3 — the co-pilot chat + structured decision blocks**. The conversational
  control surface, plus the **structured "answer-a-blocked-goal" surface** (§6):
  when a goal is `needs_answer`-blocked, present the operator selectable options +
  a recommendation + a free-text box, click → `steer_goal`. Named, unsized; carries
  the §4 constraint and the largest `[OPEN]` set. The cognition/data-model half of
  §6 is separable and can ship ahead of any console screen (renders in Telegram
  first).

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

## §6 — P3 payload: structured decision blocks (Denys, 2026-07-23)

**The problem.** When a goal is genuinely blocked on the owner — a `needs_answer`
block that *cannot* be resolved without a human decision — today it emits a
**free-text `blocked_on` string**. The owner reads the prose, mentally extracts the
choices, and hand-writes a `steer_goal` message. But the cognition *already reasons
in branches*: the live ng-zorro block literally wrote "(a) migrate+rename … or (b)
drop the clause …" in prose. The options exist; they're just buried in a string and
the owner has to reconstruct + retype them.

**The move.** Promote a human-gated block from a `string` to a small **structured
decision object** the owner answers by *clicking* (or typing a custom answer):

```
{ question, options: [ {label, what_it_means, steer_message} ], recommended, allow_custom }
```

The planner emits this **at block time** (it already did the branch reasoning);
each option carries the **pre-baked `steer_message`** it would apply. Clicking
option N fires `steer_goal(goal_id, options[N].steer_message)`; a typed custom
answer fires `steer_goal` with that free text. No new state machine — it reuses the
existing steering/unblock plumbing entirely; the change is *structuring the block +
a nicer input*.

**This is the concrete form of P3's "answer from the browser"** (§5.4's deferred
mutation reach) and the visible payoff of the whole operator surface: the console
(and the Telegram notify ping) renders a blocked goal as *"here's the fork, here's
my recommendation, one click."* It is essentially devclaw doing to its own human
blocks what `AskUserQuestion` does in a Claude session.

**Precedent — this is a generalization, not a new concept.** Firming-phase blocks
already work this way: a goal blocked in FIRMING carries structured `unknowns` and
is cleared via `answer_unknowns`, not free text. §6 extends that same
structured-Q&A discipline from firming to **execution-phase `needs_answer`
blocks** — systemic-over-specific (`CLAUDE.md` design doctrine), one consistent
"structured decision" pattern across phases rather than two block idioms.

**Scope discipline.** Only `blocked_kind == needs_answer` blocks get options — the
genuine "only the owner can decide" class. `mechanical:*` (self-heal),
`lost_ref`, `dispatch_cap`, and `bug` blocks are **not** menu choices and stay as
they are; options on them would be noise.

**Honest framing.** §6 is **legibility/UX, not reliability.** It makes the
*legitimate* human blocks fast and pleasant to clear; it does **not** reduce how
*often* devclaw blocks, nor touch the amnesiac-retry / planner-local-optimum walls
(those are separate reliability work). It scores high on the CV/packaging
scoreboard and is what makes the operator surface feel alive — prioritize it as
"packaging that delights," not "fixes the failing."

### §6 `[OPEN]` — resolve at P3 firm (NOT P1 clarify items; P1 lock is unaffected)

- **`[OPEN]` 6a — Answer authority / auth.** Clicking an option mutates a goal from
  the browser; this rides §5.4's unresolved console-auth story (tailnet today).
  Pinned when P3 firms.
- **`[OPEN]` 6b — Split the ship.** The cognition/data-model half (planner emits the
  structured object; `steer_goal` mapping) is separable from the console render and
  useful in Telegram first. Ship it ahead of any console screen, or hold it for the
  console? Recommendation: **ship the cognition/data half early** (value without a
  console), render in console at P3.
- **`[OPEN]` 6c — Persistence shape.** Where the options object lives (a new
  `goal_docs`-style projection vs a field on the block record) and whether it
  survives replan/rollback — the single-writer + append-only-log invariants apply.
- **`[OPEN]` 6d — Recommendation honesty.** The planner marks one option
  `recommended`; the UI must show it as *the loop's* recommendation, not a
  pre-made decision, and must never hide the custom-answer path. Confirm the
  labeling contract (sibling to §5.5's honesty rule).

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
