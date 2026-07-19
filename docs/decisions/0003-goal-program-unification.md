# ADR 0003 — Goal ↔ Program unification: one primitive, one dial

- **Status:** accepted 2026-07-19 (Denys). Design locked; implementation staged
  (see Migration). This record freezes the *decision and rationale*; system
  snapshots inside reflect their writing date.
- **Supersedes:** the two-primitive surface (`create_goal` + `start_program` as
  peer MCP tools) and the two-planner split (`decomposer` vs `plan_goal`).

## Context

devclaw grew two entry points that are the same idea at different ages:

- **`create_goal`** — the durable L2 loop: `scope_grill → firming → decompose →
  checklist → per-tick goal-planner (one ready item) → per-item verify gate →
  periodic done-gate evaluator → steer/resume`. Sequential, evaluated,
  steerable; closes only when a grounded review confirms `done_when`.
- **`start_program`** — the older L4 queue path: `submit_program → plan_goal
  (coarse 1–6-task DAG) → launch ALL ready tasks in parallel →
  terminalize when the DAG drains`. No firming, no evaluation, no steering.

They differ today in **both** planning (rich milestone-tagged `decompose` vs
coarse `plan_goal`) and execution (sequential+evaluated vs parallel-batch).
Live evidence (2026-07-18 exhibits) showed the split hurts in practice: the
micro-step tax on checklist goals (~5 actions per small component), and a
done-gate that catches inert output *late* because scope was fixed once,
up front.

The founding observation (Denys, 2026-07-15): **"a goal and a program is the
same thing."** You cannot plan everything up front — so you plan iteration by
iteration; a program is just the case where you *could* plan it all up front.

## Decision

### 1. One primitive, one dial

There is **one** entry point: `create_goal`. The goal-vs-program distinction is
a single dial — **re-evaluation cadence** — not a second primitive and not a
second execution strategy:

- **One-shot goal** (today's `start_program`): fully specified up front; the
  plan→execute loop runs ONCE (steerable, but one pass absent a steer).
  Re-evaluation OFF — there is nothing to re-discover.
- **Long-lived goal**: a *direction*, not a spec ("build a CRM that takes the
  best from every CRM ever built"). SAME execution loop, but after each batch
  the outer loop re-evaluates: look at what got built, re-read the goal, plan
  the next wave on top. Re-evaluation ON, per batch.

The execution machinery (task running, verify gates, delivery) is **identical**
in both modes. Do NOT reintroduce a "parallel batch vs sequential evaluated"
execution split — that was an incidental property of the two legacy code
paths, explicitly rejected 2026-07-15.

**Long-lived ≠ eternal.** A *bounded* long-lived goal terminalizes when the
evaluator confirms `done_when`; a *standing* one never self-terminalizes (a
"no new scope this wave" heartbeat means *idle, keep watching*, not *done*).
Standing-vs-bounded is a sub-property of long-lived, not the naming axis.

### 2. One planning spine

Both modes run `scope_grill → firming → decompose`. The coarse `plan_goal`
planner retires; `plan_spec` (already dead — zero production callers) is
deleted. Grilling is not goal-only: a fully-specified one-shot input is
accepted without re-interrogating what is already specified (the grill's
recommended-default mechanism makes asking cheap and skippable).

### 3. The unit of iteration is a shippable release

Each long-lived iteration is a full product cycle, not a task batch:

```
research → plan wave → build → verify → deploy → evaluate → checkpoint → (next wave)
```

The deliverable at every checkpoint is **instructions + a deployed version the
owner can open** (MVP → v2 → v3), never just merged PRs.

### 4. Work sizing — the two-level rule

- **Task** = one agent-session's worth of context (~100K tokens of work — the
  wayfinder ticket rule). Concrete and defensible; kills both the micro-step
  tax (tasks too small) and the un-reviewable mega-diff (tasks too large).
- **Wave/iteration** = the *smallest shippable increment* on top of what
  exists — a release boundary, not a task count.
- **No numeric caps anywhere.** The "Aim for 1–6 tasks" instinct is the
  documented failure mode this replaces. The decomposer prompt states both
  rules and sizes to scope.

### 5. The scope map — fog-of-war, gold-plating dies at the boundary

Long-lived scope is *discovered, not given*. Research writes into a persistent
**scope map** with four sections (adapted from wayfinder):

```
Destination | Decided so far | Not yet specified | Out of scope
```

- Anything researched that does not serve the Destination goes to **Out of
  scope and never graduates** — the anti-gold-plating filter lives at the map
  boundary, not in per-wave planner judgment.
- The map is the **cross-iteration memory**: it (plus `goal_log`/`goal_docs`
  rationale, deferrals, checkpoint choices) is what the next re-evaluation
  reads. The repo is part of the state but not all of it.
- **Write discipline (adapted from gnhf):** map updates are appended on
  *verified* iteration success; a failed iteration's partial findings and the
  failure itself are carried forward as first-class content so the next pass
  knows what went wrong. Storage: `goal_docs` (SQLite, single-writer), with
  generated views only.

### 6. Checkpoints — scheduled grill, auto-continue on silence

- The **scheduled checkpoint grill is the backbone**: questions batch to the
  inter-iteration checkpoint; the agent arrives with "here's what I shipped,
  here's the proposed next slice (recommended default) — redirect or
  continue?" No anxious per-tick "should I ask now?" judgment.
- The gate is **non-blocking**: it opens a time-gap window for feedback and,
  absent any, proceeds with the recommended default. Silence = "no new
  requirements this round," never "stop."
- **`done_when` sharpens through checkpoint feedback** — discovered
  iteratively, exactly like scope. Feedback rides the **steering channel**
  (accumulated steering refining the target); there is NO `update_goal` /
  field-patch tool (goals are durable — cancel + recreate for a wrong
  contract).
- **Done = the scope queue is drained AND feedback has gone quiet.** Both
  conditions; silence alone never terminates (that resolves the
  silence=continue vs silence=done collision).
- Mid-iteration interrupt grills remain an **unconfirmed candidate list**
  (product fork / irreversible commitment / unobtainable input / contradiction
  / owner-priced tradeoff), governed — if ever adopted — by the
  reversibility test. The checkpoint grill may prove sufficient.

### 7. Cost backstops — hard caps, not judgment

Auto-continue-on-silence means the owner cannot be the spend brake. The
backstops are mechanical (shape adapted from gnhf):

- a per-goal **iteration cap** and **token budget**;
- an **N-consecutive-failed-iterations abort**, where a complete no-op
  iteration counts as a failure (the existing no-progress watchdog, extended
  to the outer loop);
- the **zero-token idle guard is unchanged and sacred**: research, re-scope,
  and grills fire only at real events (iteration boundaries, wave
  completion) — never on an idle or in-flight tick.

### 8. Iteration failure — fail clean, don't wedge

A failed iteration marks itself failed, rolls the workspace back to the last
good state, records the failure in the scope map, and lets the next planning
pass (or checkpoint) decide — the long-lived goal does not block on it.
Exception, consistent with existing per-task semantics: a *delivery* failure
preserves work-in-progress for a repair pass rather than resetting it. All
existing fail-closed task/gate behavior is unchanged underneath.

### 9. What is explicitly rejected

- **Agent-reported completion.** gnhf's `--stop-when` (loop ends when the
  agent's own output claims the condition is met) is the anti-pattern; the
  closeloop exhibit (shipped inert dead code) is the empirical case. "Done"
  stays a *proposal* gated on the grounded evaluator — for every mode.
- **A second execution strategy** (parallel-vs-sequential as the axis).
- **Field-patch tools** (`update_goal`, `done_when` patching).
- **Fixed numeric task caps** in any planner prompt.
- **One-small-commit-per-iteration granularity** at the outer loop (that is
  the micro-step tax; gnhf's loop altitude is our *inner task* loop).

## Migration (staged; `main` stays shippable after every PR)

1. **PR-B — unify breakdown on the decomposer** (the no-regrets prereq):
   route the queue's `_planner` slot and `start_program` through a
   `decompose → PlannedTask` adapter (id→key, requirement+evidence_target→goal,
   depends_on→depends_on_keys, milestone→milestone). **Thread the `scaffold`
   flag** through `PlannedTask → _persist_plan → create_task` (or program-path
   scaffolds lose their review-skip and the gate fails closed on generated
   diffs). Retire `plan_goal` + `plan-goal.md` + the `cognition plan` CLI
   (`cognition breakdown` is the replacement lens); delete dead `plan_spec`.
2. **Collapse the surface:** `start_program` becomes sugar for (then an alias
   of) `create_goal(mode=one_shot)`; the goal-planner's pre-checklist
   "backlog mode" simplifies away once every live goal has a checklist.
3. **Build the iterative loop** (the genuinely new part): the scope map, the
   research→wave planning step on top of existing state, the checkpoint
   (grill + deploy + auto-continue), the cost backstops. `world_research` /
   `research.py` callers already exist; the *loop around them* is the build.

## Open items (flagged, not blocking)

- Interrupt-grill taxonomy (candidate list above — adopt only if checkpoint
  grills prove insufficient).
- Deploy-target per iteration: likely a first-checkpoint "external input"
  question, not a design gap — but the checkpoint deliverable *requires* a
  real deploy target each cycle.
- Steering vs checkpoint unification (steering ≈ a checkpoint triggered
  early) — decide during stage 3.

## Prior art

- **wayfinder** (mattpocock/skills, MIT): fog-of-war map, HITL/AFK ticket
  types, session-sized tickets, out-of-scope as first-class. Methodology
  adapted; plugin NOT installed (model-agnostic layer-5 invariant).
- **gnhf** (kunchenguid/gnhf): notes-on-verified-success write discipline,
  hard runtime caps with no-ops-count-as-failures, rollback+preserve-for-repair.
  Its agent-reported stop condition is the documented anti-pattern.
