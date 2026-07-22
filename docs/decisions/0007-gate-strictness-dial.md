# ADR 0007 — Gate strictness dial: advisory-by-default gates, per-goal strict opt-in

- **Status:** accepted 2026-07-22 (Denys). Tranche scheduled the same day —
  graduated from [`../proposals/gate-strictness-dial.md`](../proposals/gate-strictness-dial.md)
  under the spec lifecycle. This record freezes the *decision and rationale*;
  the proposal keeps the full narrative + the five clarify-step resolutions.
- **Recalibrates:** the "loud failure over silent degradation" invariant
  (CLAUDE.md, Tranche 0) — it does NOT repeal it (see Consequences).
- **Subsumes:** the per-project `DEVCLAW_GOAL_BROWSER_GATE_MODE` (`flexible|strict`)
  env var into a general per-goal dial.
- **Defers (named, not this tranche):** the adversarial *judge-gate* mechanism
  and the Playwright-in-sandbox runtime fixes (see Deferred).

## Context

devclaw's gates fail **closed** because it runs autonomously and unattended —
there is no human at 3am, so machine gates replaced the reviewer. That instinct
is right for that threat model, but the gates are **rigid rules, not judgment** —
a reviewer's strictness with none of a reviewer's judgment, the worst of both.
The proof (2026-07-19 → 22):

- `finance-sentry-ui-library`: a **one-line `angular.json`** build-config change
  failed **5×** and wedged the goal for a **week**. The browser gate's trigger is
  path-based ("a file under `frontend/` changed and a `playwright.config.*`
  exists") — it cannot tell a config-only edit from a UI behavior change, so it
  demanded a real-browser run for a line that renders nothing.
- Night of 2026-07-21→22: gate misfires dominated the problems catalog
  (browser gate ×5, review-gate crash ×5) — the mechanism meant to *prevent*
  broken output was the top *cause* of wedged, no-output nights.

The recurring comparison ("other harnesses aren't this strict and work fine") is
real but rigged: Cursor/Aider/Claude Code keep a human reviewing every diff
seconds later — the human *is* their gate. Deleting devclaw's gates regresses to
the confident-but-wrong scars (`cmn-select` passing every unit gate while
throwing `NG05105` on dropdown open; the `verify_cmd` existence-vs-execution
scar; PR #265 merged red). The fix is neither "keep strict" nor "delete" — it is
to **recalibrate strictness to the stakes of the goal**, which only the owner
knows, and make "loud" mean *surfaced*, not *wedged*.

devclaw's scoreboard is legibility / clean-nights / CV, not product reliability.
Against that scoreboard a wedged goal that pollutes the demo costs more than a
slightly-imperfect diff that ships visibly flagged.

## Decision

**Strictness is a per-goal dial; dial-able gates default to advisory (log
loudly, surface in the PR, do not wedge); `strict` is an owner opt-in for goals
whose output is depended on.**

1. **Two levels, `trust | strict`, default `trust`.** `trust` = a failed
   dial-able gate records its verdict + reason and the work **ships**; `strict` =
   it **blocks** (today's fail-closed behavior). `balanced` is intentionally not
   built — it only becomes meaningful once the judge-gate exists.
2. **The dial is a per-goal field with a per-project default** the goal inherits
   when unset. It is changeable **end-to-end through a dedicated narrow verb**
   `set_goal_strictness(goal_id, strictness)` — NOT a generic `update_goal`/patch
   tool (goals stay durable; this is the one field O1 blessed as steerable,
   because it changes the *consequence-of-a-verdict*, not objective/done_when/
   backlog). The verb is exposed at every layer: `GoalStore.set_strictness`
   (atomic goal.yaml rewrite) → `GoalService.set_strictness` → the **MCP tool**
   `set_goal_strictness` → the **HTTP route** `POST /goals/{id}/strictness` → a
   one-tap **console toggle** on Goal Detail. `create_goal` also accepts an
   initial `strictness`. A change applies to FUTURE dispatches (the value is
   snapshotted on the task/program row at dispatch); in-flight work keeps the
   value it was dispatched with.
3. **Only two gates are dial-able; three are always-hard.** Dial-able (obey the
   dial): the **browser-E2E gate** and the **pre-PR adversarial review gate** —
   both backstopped by the human merge. Always-hard (ignore the dial, fail
   closed in every mode): **test-integrity**, **delivery-trust** (CI-green-
   before-review), and the **done-gate** grounded `achieved` evaluation — these
   guard against the model gaming its own evidence or closing a goal on its own
   say-so, which the human merge does NOT reliably catch. The dial only ever
   loosens the two review-shaped gates.
4. **Advisory verdicts still count and still surface.** A non-blocking verdict is
   still written to the log + `problems` catalog + `eval_outcomes`. It is **not**
   a clean-night/cycle wedge (it shipped) but **is** listed in the cycle report
   the way a self-healed pause is (ADR 0006), so lost quality stays visible. The
   verdict + reason **rides into the PR body** (mechanical text, zero LLM) — the
   human merge is the enforcement point for advisory gates.
5. **The per-goal dial replaces `DEVCLAW_GOAL_BROWSER_GATE_MODE`**; a set env
   var survives only as the global default an unset per-goal/per-project dial
   falls back to.

### Implementation shape — data + one pure policy function, NOT mode-objects

The dial is *data*: a `Strictness` enum (`TRUST | STRICT`) on the goal. The
consequence is decided by ONE pure function at the settle/gate choke point —
`(gate_id, verdict, strictness) → block | advise_and_ship` — with the always-
hard gate ids a data set inside it. This matches the existing idiom (gates are
already *pure verdict functions* — `quality/browser_gate.py`: "Pure module — no
subprocess, no I/O") and keeps the whole policy legible in one screen for the
mandatory `invariant-guard` pass. **Deliberately not the Strategy pattern** —
strict vs. trust is a one-branch consequence difference and there is no third
variant to justify polymorphism. The Strategy-shaped seam belongs to the *other*
knob (the deferred judge-gate: rule- vs. model-formed verdict). **Consequence
knob = value; mechanism knob = strategy.**

## Deferred (named, explicitly not this tranche)

- **The adversarial judge-gate.** Replace a gate's rigid rule with a second,
  independent model instance prompted adversarially to find the break — trust
  the model as a *fresh skeptic*, not as its own self-report. Composes with this
  dial (judge = how a verdict forms; dial = whether its "no" blocks). Deferred
  because it is real, adversarially-shaped engineering, invisible to the CV
  audience, and — once advisory-default lands — no longer *urgent* (a dumb rule
  that only logs cannot wedge). Seed: `quality/reachability.py` +
  `prompts/browser-reachability.md`.
- **Playwright-in-sandbox timeout fixes.** The authorized ui-library spec timed
  out booting Chromium inside the flat 3600s task budget. With advisory-default
  a timeout becomes a loud log, not a torn-down goal, so urgency drops. When it
  returns (for `strict` app-surface goals): warm `npm ci` + a project-version-
  matched Chromium in the *prep* step (out of the timed turn, like `mise install`
  already is); pin `PLAYWRIGHT_BROWSERS_PATH` to the baked cache to kill the
  task-time re-download; swap the flat wall-clock for a no-output watchdog on
  e2e tasks.

## Consequences

- **The "loud failure over silent degradation" invariant is recalibrated, not
  repealed.** It was written against *silent* degradation (a gate crash silently
  approving, #186; a delivery silently done-without-a-PR, #183). Advisory-default
  keeps every failure **maximally loud** — logged, in the problems catalog,
  counted in `eval_outcomes`, listed in the cycle report, surfaced in the PR body
  — and only changes the *consequence* from "wedge" to "ship-with-the-flag-
  visible." Because delivery is **open-PR-only and a human merges**, the human at
  merge is the strict backstop the loop otherwise lacks. This argument holds ONLY
  for gates whose escape is a human merge — which is exactly why the three
  evidence-integrity gates stay always-hard (decision 3).
- **Scope discipline.** This changes only the *consequence* of a verdict and
  *who chooses* strictness. It does NOT reduce the *number* of gates (a separate
  lever) and does NOT touch auto-merge/autodeploy policy — open-PR-only + human
  merge is the backstop this decision *relies on*.
- **Opportunity cost, acknowledged.** Gate internals are invisible to the
  portfolio audience; packaging remains higher-value. This tranche earns its slot
  only because wedged goals pollute the demo and "clean nights" is itself a
  headline metric (ADR 0006). Capped at the dial + advisory-default + PR
  surfacing — the judge-gate and runtime fixes wait.
