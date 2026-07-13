# Delivery flows — how work becomes merged code

How a goal's dispatches turn into PRs, how those PRs reach `main`, and where
the dispatch-cap backstop sits. Three delivery shapes exist; which one a
dispatch uses decides the PR shape you see on GitHub.

Automerge is resolved per goal **before** any of this runs: the project
registry's `automerge` override wins, else the fleet-wide
`DEVCLAW_GOAL_AUTOMERGE` default (`goal/merge.py:resolve_automerge`). "On"
means the tick is handed a merger callable; "off" means it is handed none.

## Shape 1 — legacy/backlog mode: one task = one PR

Standing mission goals (e.g. `closeloop-mission-v2`). The planner invents each
next action; every dispatch forks a fresh branch off current `main`.

```
                 planner picks action
                        |
                        v
        workspace reset to origin/main  (pristine checkout)
                        |
                        v
              engineer runs in sandbox
              commits -> opens PR #N
                        |
                        v
              sandbox verify gate runs
                        |
          +-------------+--------------+
          | gate PASSED               | gate FAILED / task failed
          v                           v
   automerge ON?                 PR left open
     |        |                  counts +1 toward dispatch cap
     | yes    | no               planner told the failure detail
     v        v
  squash-   PR left open,
  merge     planner told
  to main   "pr_state=open (unmerged —
     |       owner review pending)"
     v
  next dispatch forks from
  UPDATED main -> no overlap
```

**Automerge off + dependent tasks is a misconfiguration in this mode**: the
next dispatch forks from a `main` that lacks the previous (unmerged) delivery,
so the engineer re-implements or conflicts. Use checklist mode instead, or
turn automerge on.

## Shape 2 — checklist mode (Pillar 1): one goal = one PR, many commits

Bounded "build X" goals (e.g. `closeloop-bench-2026-07-05`). The decomposer
emits an atomic checklist up front; the checklist is the work surface.

```
   decomposer -> checklist (N items)
                        |
                        v
        every dispatch checks out the SHARED
        branch  goal/<goal-id>   (not main)
                        |
                        v
        item k commits STACK on item k-1
        all pushes go to the SAME PR
                        |
                        v
        automerge deliberately SKIPPED per item
        (merging would delete the shared branch
         and fork item k+1 back to main)
                        |
                        v
        done-gate = the single review moment
        for the one cumulative PR
```

No overlap by construction, nothing for automerge to do mid-goal. This shape
is safe with automerge off.

## Shape 3 — programs: one program = a stack of PRs + reconcile at settle

When the planner decides work is too big for one dispatch it fires
`start_program` — a DAG of tasks the engine runs as a unit. Each task opens
its own PR **based on the previous task's branch** (closeloop #66 → #67 → #68).

A program settles with `gate_passed=None` (many per-task gates, no single
verdict), so Shape 1's automerge can't touch the stack. Before 2026-07-09 the
goal burned follow-up dispatches shepherding its own PRs to main — and when a
shepherding dispatch landed the content as one consolidating squash, the
source PRs stayed open as zombies. The **reconcile step** replaces that:

```
        program settles "done"  (automerge resolved ON)
                        |
                        v
        for each PR in the stack, IN ORDER (base-most first):
                        |
        +---------------+------------------------------+
        |               |               |              |
        v               v               v              v
   already        diff already     mergeable +    conflicting /
   merged/closed  on main          checks green   checks red / probe failed
        |         (reverse-apply        |              |
        |          test passes)         |              |
        v               v               v              v
      skip        close PR with     squash-merge   LEFT OPEN with the
                  "superseded"      (same merger   reason in the summary
                  comment           as Shape 1)        |
                                                       v
                                          planner sees it in
                                          finished_detail and decides
                                          whether a fix dispatch is
                                          worth it (cognition stays
                                          in the planner)
```

Sequential on purpose: merging PR k re-bases k+1 and re-runs its checks, so
k+1's state only means something after k landed. Every branch is best-effort —
a failed probe/close/merge degrades to "left open", never breaks the tick.
With automerge off the reconcile step does not run at all; the owner reviews
program PRs by hand, same contract as Shape 1.

## The dispatch cap (runaway backstop, all shapes)

`cap = max(len(backlog), len(checklist)) + 2`. Progress-aware since #172/#173:

```
   dispatch            -> counter +1
   settle SUCCESSFUL   -> counter -1   (done; gate passed OR gateless —
                                        reviews, programs, no-gate tasks)
   settle FAILED       -> stays        (failed run, or gate FAILED)

   counter >= cap      -> goal BLOCKED, owner notified
   owner steer/resume  -> unblocks, counter reset to 0
```

Both `steer_goal` and `resume_goal` clear the counter; the cap block is
human-gated by design — unlike `mechanical:corrupt_doc`/`mechanical:prep`,
it never auto-heals.

Only a planner looping on **broken** dispatches accumulates to the cap. A
healthy goal — including one that grounds every delivery in a read-only
verification review — never blocks. Churn on successful-but-aimless work is
caught by the direction evaluator (every `EVAL_EVERY` deliveries) and the 6h
no-progress watchdog, not by this counter.

## Field history that shaped this

- 2026-06-26 `finance-sentry-mcp-v3/v4` — PR fan-out / shared-branch deletion
  → Shape 2's skip-automerge rule and stacked goal branch.
- 2026-07-05 `closeloop-bench` — planner claimed "PR merged" for an unmerged
  PR → settle detail now states the PR's real state, built after the merge
  attempt.
- 2026-07-07 `closeloop-mission-v2` blocked at cap 6 with all work merged
  → #172 refund for gated deliveries.
- 2026-07-09 `closeloop-mission-v2` blocked again on its own on_track
  verification reviews; five zombie superseded PRs found open on closeloop
  → #173 refund-all-successful-settles + the Shape 3 reconcile step.
