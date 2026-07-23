# ADR 0010 — Console P3.1: structured decision blocks (§6)

- **Status:** accepted 2026-07-23 (Denys — "let's continue"; firmed the two
  P3.1 decisions: §6-blocks-first, planner-emits-at-block-time). Graduated from
  `proposals/console-operator-surface.md` §6 after P1 (ADR 0008) + P2 (ADR 0009)
  shipped. Freezes the P3.1 decision + sizing.
- **Scope:** the §6 structured-blocks slice of P3 only. **P3.2 (the co-pilot
  chat + the OpenClaw-waiter-over-OAuth integration, §4) is deferred** — a
  distinct, larger undertaking; §5.3 chat-scope stays open for it.
- **Touches layer 3 (cognition) — the most invariant-sensitive slice yet.** No
  new LLM call, no tick-path idle work (see Invariants).

## Context

When a goal is genuinely blocked on the owner (`blocked_kind == needs_answer`),
today the planner emits a **free-text** `question` (`goal/planner.py` →
`PlanResult(decision="blocked", question=…)`, persisted at `tick.py:739` as
`blocked_on`). The owner reads the prose, extracts the choices, and hand-writes a
`steer_goal`. But the planner *already reasons in branches* — the live ng-zorro
block literally wrote "(a) migrate+rename … or (b) drop the clause …" in prose.
§6 captures those branches **structured** so answering is a click.

Two enabling facts make this buildable now:
- **The mutation path already exists.** The console already steers/resumes/
  answers/cancels goals via POST endpoints over the tailnet (GoalDetail's
  BlockedBanner). §6 option-buttons reuse the **existing steer endpoint** — no
  new mutation surface, no new auth story (that resolves the deferred §6a).
- **A structured-block precedent already exists.** Firming-blocked goals carry
  structured `unknowns` (a doc), surfaced by `get_goal` and answered via
  `answer_unknowns`. §6 is the *execution-block* analog of that same pattern.

## Decision

When the planner blocks `needs_answer`, its **existing cognition call** also emits
a structured decision object (Denys's chosen approach — no separate optimizer
pass, no extra LLM call):

```
{ decision: "blocked", question, recommended: "<key>",
  options: [ { key, label, detail, steer } ], allow_custom: true }
```

Each option carries the **pre-baked `steer` message** it would apply. The console
renders a blocked goal's options as buttons + a recommendation + the existing
custom steer box; clicking option N POSTs the existing `/goals/{id}/steer` with
`options[N].steer`; the custom box POSTs free text. **No new state machine** —
it structures the block + reuses the steer plumbing.

### §6 `[OPEN]` resolutions

- **§6a mutation-auth — RESOLVED (already solved).** The console already mutates
  goals over the tailnet; §6 buttons reuse the existing steer POST. No new auth.
- **§6b split-the-ship — RESOLVED.** The cognition/data half ships first
  (P3.1a, backend-only, options in the JSON); the render follows (P3.1b).
- **§6c persistence — RESOLVED.** Options persist as a **per-goal block-options
  doc** written when the block is created (following the firming-unknowns
  precedent — NOT a `goal_status` schema change), read by `get_goal` only while
  blocked, and naturally ignored once `steer` unblocks. Single-writer safe: the
  tick (heartbeat) writes it on the same path as `blocked_on`.
- **§6d recommendation honesty — RESOLVED.** The UI marks `recommended` as *the
  loop's* recommendation, never a pre-made decision, and never hides the
  custom-answer box.

### Scope discipline (unchanged)

Only `blocked_kind == needs_answer` blocks get options. `mechanical:*`
(self-heal), `lost_ref`, `dispatch_cap`, and `bug` blocks are not menu choices —
they emit no options and render exactly as today.

## Sizing (end-of-week cap, ≈2 PRs)

1. **P3.1a — cognition + persistence + wire (backend).** Planner emits/parses
   `options`/`recommended` (`goal-planner.md` prompt + `PlanResult` model +
   `validate()`, blank-safe so existing blocks/tests are byte-unaffected); the
   block path persists the options doc; `/goals/{id}.json` surfaces them. Named
   regression tests: planner parse (options present + absent), persistence,
   wire shape.
2. **P3.1b — the render (frontend).** GoalDetail's BlockedBanner renders the
   options as click-to-steer buttons + the recommendation + the custom box,
   with the §6d honesty labelling.

## Invariants (this is a cognition-layer change — called out explicitly)

- **Zero-token idle guard: untouched.** No new LLM call — options ride the plan
  cognition that already runs when the goal has work and decides to block. A
  blocked goal does not re-plan (blocked ticks stay zero-token), so options are
  emitted **once** at block time and persisted.
- **Grounded / fail-closed cognition:** an absent/malformed `options` field is
  **ignored** (blank-safe → no options rendered, block behaves exactly as
  today), never an error and never a fabricated menu. Follows the "undocumented
  model field is ignored, not honored" rule.
- **Single writer:** the block-options doc is written by the tick on the same
  CAS'd block-transition path as `blocked_on`. The console only reads it.
- **Legibility/UX, not reliability** — §6 makes *legitimate* blocks fast to
  clear; it does not reduce how often devclaw blocks.

## Deferred (named, not this tranche)

- **P3.2** — the co-pilot chat over the existing OpenClaw waiter/OAuth (§4), the
  full conversational control surface, and §5.3 chat-scope (global vs per-goal).
