# Proposal — Gate strictness dial: advisory-by-default gates with a per-goal opt-in to fail closed

- **Status:** **LOCKED (direction)** — 2026-07-22. Direction agreed in
  conversation; the mandatory clarify step (`.claude/rules/spec-lifecycle.md`)
  ran the same day — all five `[OPEN]` items resolved by Denys, resolutions in
  place in §5. **LOCKED is direction, not schedule** — sequencing stays Denys's
  call; a locked line is reopenable (edit the doc, don't silently diverge).
  **No code before a tranche is scheduled + this graduates to an ADR.**
- **Date opened:** 2026-07-22 · **Locked:** 2026-07-22
- **Authors:** Denys + Claude (conversation of 2026-07-22)
- **Supersedes / relates:** subsumes the per-project `DEVCLAW_GOAL_BROWSER_GATE_MODE`
  (`flexible|strict`) into a general per-goal dial (§4); names a **deferred
  graduation** — the adversarial *judge-gate* (§6) — that is explicitly NOT in
  this tranche. Touches the "loud failure over silent degradation" invariant
  head-on (§3) — read that section first.

> How to read this: **[CONFIRMED]** = decided in the 2026-07-22 conversation.
> Sections once marked **[OPEN]** are all resolved in place (§5) — the
> mandatory clarify step ran the same day the draft landed.

---

## 1. The problem [CONFIRMED]

devclaw's gates were built for a threat model — an autonomous loop shipping
broken work at 3am with no human watching — and they fail **closed** to honor
"loud failure over silent degradation" (Tranche 0). That instinct is correct
*for that threat model*. But the gates are **rigid rules, not judgment**, and
that combination — a careful reviewer's strictness with none of a reviewer's
judgment — is the worst of both. The evidence is the `finance-sentry-ui-library`
week (2026-07-19 → 22):

- A **one-line `angular.json`** build-config change failed **5 times** and
  wedged the goal for a week. The browser gate's trigger is path-based ("did a
  file under `frontend/` change, and does a `playwright.config.*` exist?") — it
  cannot tell a config-only edit from a UI behavior change, so it demanded a
  real-browser run for a line that renders nothing. A human would have waved it
  through in two seconds.
- The unblock required an owner round-trip, and the authorized fix then
  **timed out** booting Playwright in the sandbox (separate issue, §6).
- The night of 2026-07-21→22, gate misfires dominated the problems catalog
  (browser gate ×5, review-gate crash ×5) — the mechanism meant to *prevent*
  broken output was the top *cause* of wedged, no-output nights.

The comparison Denys keeps making — "other agent harnesses (Cursor, Aider, the
reference repos) aren't this strict and work fine" — is real but **rigged**:
those tools keep a human reviewing every diff seconds after it's produced. The
human *is* their gate. devclaw removed the human and replaced them with rigid
rules. The fix is not "delete the gates" (that regresses to the confident-but-
wrong scars — `cmn-select` passing every unit gate while throwing `NG05105` the
instant its dropdown opened; the `verify_cmd` existence-vs-execution scar; PR
#265 merged red). The fix is to **recalibrate strictness to the stakes of the
goal**, and to make "loud" mean *surfaced*, not *wedged*.

## 2. The direction [CONFIRMED]

**Strictness becomes a dial the owner sets per goal; gates default to advisory
(log loudly, do not block); `strict` is an opt-in for the handful of goals whose
output the owner actually depends on.**

- A gate in **advisory** mode still *runs* and still records its verdict + reason
  (into the log, the `problems` catalog, and the `eval_outcomes` projection) —
  the evidence and the demo story ("it verifies before it ships") are fully
  preserved — but a negative verdict **does not wedge the goal**. It surfaces.
- A gate in **strict** mode keeps today's fail-closed behavior.
- The **default is `trust`** (advisory), because devclaw's scoreboard is
  legibility / clean-nights / CV, not product reliability
  ([[scoreboard-cv-learning-2026-07-18]]). A wedged goal that pollutes the demo
  costs more, against that scoreboard, than a slightly-imperfect diff that ships
  and is visibly flagged.
- Reserve **`strict`** for goals where wrong output has a real-world cost — the
  live finance-sentry monitoring work is the canonical case.

This is the same doctrine already in `CLAUDE.md`: *software development is the
first domain, not the definition; keep domain/threat specifics at the edges.*
Strictness is a threat-model specific, so it belongs on a per-goal dial, not
baked into every gate as an unconditional law.

**Number of gates ≠ strictness of gates** — two independent levers, deliberately
NOT conflated here. This proposal changes only the *consequence* of a verdict
(block vs. surface) and *who chooses* it (the owner, per goal). Consolidating or
removing gates is a separate question, out of scope (§7).

## 3. The invariant this touches — head-on, not a footnote [CONFIRMED intent, mechanism OPEN]

This proposal is in direct tension with **"loud failure over silent
degradation"** (CLAUDE.md, Tranche 0), so per the spec-lifecycle hygiene rule it
states that in its own words rather than burying it:

The invariant was written against **silent** degradation — a gate crash silently
counting as an approval (#186), a delivery silently "done without a PR" (#183).
Advisory-default does **not** reintroduce silence. It keeps every failure
**maximally loud** — logged, counted in the problems catalog, counted against the
clean-night rate (ADR 0006), and (proposed §5-O5) surfaced in the PR body — and
only changes the *consequence* from "wedge the goal" to "ship-with-the-flag-
visible, and let the human merge be the enforcement point." Because delivery is
**open-PR-only and a human merges** (never auto-merge on these goals), the human
at merge *is* the strict backstop for advisory gates — the reviewer devclaw
removed from the loop is still there at the merge boundary. That is the crux of
why this is a recalibration, not a weakening.

The corollary — and the reason §5-O2 was the load-bearing question — is that
this argument **only holds for gates whose escape is a human merge.** A gate
that protects against the model *gaming its own evidence* where a human merge
might not catch it (test-integrity: tests silently deleted; delivery-trust: red
CI merged) stays hard even under `trust`. The dial-able-vs-always-hard line is
resolved in §5-O2.

## 4. Relationship to the existing browser-gate mode [CONFIRMED]

`DEVCLAW_GOAL_BROWSER_GATE_MODE` (`flexible` default / `strict`) already proves
the pattern for one gate — and the browser gate already carries a partial
*trigger* fix (a library-only `*/src/lib/*` diff is `not_triggered`, landed
2026-07-18). This proposal **generalizes the mode** from one env-var-on-one-gate
to a per-goal dial across the dial-able gate set (§5-O2), and the config-only
trigger hole (the `angular.json` case) is closed for free by the deferred
judge-gate (§6). The new per-goal dial **replaces** `DEVCLAW_GOAL_BROWSER_GATE_MODE`;
the env var, if set, becomes only the global default an unset per-goal dial
falls back to (§5-O3).

## 5. Clarify-step resolutions [all RESOLVED 2026-07-22, by Denys]

- **[RESOLVED] O1 — Where the dial lives, and can it change.** A **per-goal
  field**, default `trust`, with a **per-project default** the goal inherits
  when unset. `steer_goal` **may flip it** mid-flight: it changes the
  *consequence-of-a-verdict*, not the objective / done_when / backlog, so it is
  not the kind of field-patch the goals-are-durable rule forbids
  ([[feedback_goals_are_durable_no_field_patches]]) — no cancel + re-file
  needed to change strictness.
- **[RESOLVED] O2 — Which gates are dial-able vs. always-hard.** The
  load-bearing split (§3), ratified as proposed. **Dial-able** (go advisory
  under `trust`): the **browser-E2E gate** and the **pre-PR adversarial review
  gate** — both have the human merge as a backstop. **Always-hard** (ignore the
  dial, stay fail-closed in every mode): **test-integrity**, **delivery-trust**
  (CI-green-before-review), and the **done-gate** grounded `achieved`
  evaluation — these guard against the model gaming its own evidence or closing
  a goal on its own say-so, which the human merge does NOT reliably catch. The
  dial only ever loosens the two review-shaped gates; the three
  evidence-integrity gates are outside its reach by construction.
- **[RESOLVED] O3 — Dial shape + fate of the env var.** **Two levels:
  `trust | strict`.** `balanced` is dropped for now — it only becomes meaningful
  once the judge-gate (§6) exists (judge can block, rigid rules cannot), so it
  waits for that graduation. The per-goal dial **replaces**
  `DEVCLAW_GOAL_BROWSER_GATE_MODE`; a set env var survives only as the global
  default an unset per-goal/per-project dial falls back to.
- **[RESOLVED] O4 — Advisory verdict still counts everywhere.** An advisory
  (non-blocking) verdict is **still written** to the log + `problems` catalog +
  `eval_outcomes`. A `trust`-mode surfaced verdict is **NOT a clean-night
  wedge** — it shipped — **but is listed in the night report** the way a
  self-healed pause is (ADR 0006 §5-O1), so lost quality stays visible without
  failing the night.
- **[RESOLVED] O5 — Where the advisory verdict surfaces to the human.** The
  gate's verdict + reason **rides into the PR body** so the human sees it at the
  merge boundary — this is what makes the human merge the real enforcement point
  for advisory gates (§3). **Mechanical text, zero LLM calls.**

## 6. Deferred graduation — the adversarial judge-gate [CONFIRMED as deferred, NOT this tranche]

Captured so the thinking survives; **explicitly out of this tranche.**

The rigid-rule → judgment upgrade: replace a gate's path/grep rule with a
*second, independent model instance* prompted adversarially to find the break —
the reasoning becomes the verdict. This is the fullest form of "trust the model
more," done as trust-but-**verify** (a fresh skeptic), not trust-the-self-report
(the scar). It composes cleanly with this proposal's dial: the judge is the
*mechanism* (how a verdict forms); the dial is the *consequence* (does its "no"
block). In `trust` mode the judge is a reasoned advisory second opinion in the
log/PR; in `strict` mode the same judge can block.

Why deferred, not now: (1) real engineering — schema, parse, tests, and an
*adversarial* shape (a bare "lgtm?" call hallucinates blocks, which re-creates
wedges under `strict`); the trustworthy shape is skeptic-prompted, defaulting to
*pass* on uncertainty in `trust`. (2) Invisible to the CV audience. (3) Once
advisory-default lands, rigid rules stop *hurting* (they only log), so the
pressure to replace the mechanism evaporates — this becomes a quality upgrade
done on want, not need. Seed already exists: `quality/reachability.py` +
`prompts/browser-reachability.md` is a model-judgment gate for the browser gate
specifically; the graduation generalizes that pattern.

**Also parked here (linked, separate):** the Playwright-in-sandbox timeout that
killed the authorized ui-library fix. With advisory-default, a browser-run
timeout becomes a loud log, not a torn-down goal — so the urgency drops. When it
returns (for `strict` app-surface goals), the candidate fixes are: warm
`npm ci` + a project-version-matched Chromium in the **prep** step (out of the
timed turn, like `mise install` already is); pin `PLAYWRIGHT_BROWSERS_PATH` to
the baked cache to kill the task-time re-download; and swap the flat wall-clock
for a no-output watchdog on e2e tasks. None in this tranche.

## 7. Explicitly out of scope [CONFIRMED]

- The judge-gate mechanism (§6) — deferred graduation, not this tranche.
- The Playwright/sandbox runtime fixes (§6) — linked, separate.
- **Reducing the number of gates** — consolidating overlapping "is it good?"
  checks (adversarial-review vs. done-gate vs. browser) is a distinct lever; this
  proposal touches only strictness/consequence.
- Auto-merge / autodeploy policy — unchanged; open-PR-only + human merge is the
  backstop this proposal *relies on*, not something it edits.

## 8. Opportunity cost — why this earns its place, and its cap [CONFIRMED]

Gate internals are invisible to the portfolio audience; the highest-value work
remains **packaging** ([[scoreboard-cv-learning-2026-07-18]],
[[console-operator-surface-2026-07-18]]). The *only* reason this earns a tranche
now is the part that IS visible: wedged/stalled goals from gate misfires pollute
the demo and the console, and "clean nights" is itself a headline metric
(ADR 0006). This is capped at exactly the dial + advisory-default + the O5
surfacing — **one small tranche, then straight back to packaging.** The judge-
gate and runtime fixes wait.

## 9. Sequencing [CONFIRMED as intent, not schedule]

One small tranche: a strictness field + inheritance (O1), the dial-able/always-
hard split wired into the gate settle path (O2), advisory verdicts that still
record + surface (O4/O5), env-var subsumption (O3). Named regression tests per
`.claude/rules/testing.md`; `invariant-guard` run on the diff before PR (this
proposal touches the loud-failure invariant, so the guard pass is mandatory, not
optional). Sequencing stays Denys's call per the spec lifecycle.
