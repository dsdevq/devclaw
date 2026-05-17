---
name: define_run
description: "Take an approved proposal at `~/.life/projects/<slug>/proposals-approved/<file>.md` and define a Run — write `runs/<run-slug>/dag.yaml` (task graph with dependencies) + `runs/<run-slug>/status.yaml` (rollup). Called by `propose_change` resolve mode immediately after a `ship it` reply moves the proposal file into `proposals-approved/`. The Curator picks the Run up on its next heartbeat. NEVER invoked directly by the operator — always downstream of `propose_change`. Schema canonical at `~/.life/system/project-curator-architecture.md` §5.3/§5.4."
---

# define_run

You are translating an approved RFC into a machine-walkable Run. The Curator will read your output and spawn tasks against it autonomously — your DAG IS the contract. Get it right.

Phase 5.7c. Architecture: `~/.life/system/project-curator-architecture.md` §2.7 + §5.3/§5.4.

## Hard behavioral rules

- **One Run per invocation.** One proposal → one Run.
- **Idempotent.** If `runs/<run-slug>/dag.yaml` already exists for this proposal, REFUSE — don't overwrite. The proposal is already running (or completed and being re-shipped). Reply: *"Run already defined at runs/<slug>/dag.yaml — investigate the existing Run before re-shipping."*
- **Tasks come ONLY from the proposal's "Step-by-step plan" section.** Don't invent steps, don't merge two, don't split one into "Task A + Task B" unless the proposal itself did. The proposal is the contract; your DAG is its mechanical translation.
- **Every task must be evidence-able.** If a step is "discuss with the team," it's not a task — it's a non-goal that snuck in. Refuse with the specific step quoted: *"step N is not evidence-able — proposal needs an edit:"*
- **No DAG cycles.** `depends_on` must form a DAG. Refuse if a cycle is implied.
- **Dependencies preserve the proposal's parallelism markers.** "(parallel with N)" means `depends_on` set matches step N's. "(depends on N, M)" maps directly to `depends_on: [N, M]`.

## Inputs you receive

A path to an approved proposal: `~/.life/projects/<slug>/proposals-approved/<date>-<short-slug>.md`. You'll be called as part of `propose_change`'s resolve mode — that skill writes the file move, then invokes you.

## Sequence

### 1. Resolve paths

```bash
PROP="<provided path>"
SLUG=$(basename "$(dirname "$(dirname "$PROP")")")    # finance-sentry
PROP_FNAME=$(basename "$PROP" .md)                    # 2026-05-17-banksync-circuit-breaker
RUN_DIR="$HOME/.life/projects/$SLUG/runs/$PROP_FNAME"

if [[ -e "$RUN_DIR/dag.yaml" ]]; then
  # Idempotency refusal.
  echo "Run already defined at $RUN_DIR/dag.yaml — investigate the existing Run before re-shipping."
  exit 0
fi

mkdir -p "$RUN_DIR/tasks"
```

### 2. Read the proposal

Read `$PROP` end-to-end. The sections you care about:

- **Step-by-step plan** — the source of truth for task structure.
- **Acceptance criteria** — these get distributed across tasks where the proposal makes the binding explicit; otherwise the final task carries them all.
- **What changes** — used to assign `target_repo` / file scope hints to each task.
- **Effort estimate** — informs per-task `budget.max_runtime_seconds`.

### 3. Parse the Step-by-step plan into nodes

For each numbered step:

- `id` = `NNN-<3-5-word-kebab-slug>` derived from the step's first phrase. Zero-padded 3-digit prefix preserves natural ordering.
- `title` = the step's text, trimmed to ≤ 100 chars.
- `kind` = inferred:
  - Code work → `code` (touches files in the target repo, opens a PR)
  - Research / draft / chore → match `task_intake`'s classification table
- `depends_on` = parsed from parenthetical markers:
  - No marker AND not step 1 → `depends_on: [<previous step id>]` (sequential default)
  - "(parallel with N)" → `depends_on:` = step N's `depends_on` (siblings)
  - "(depends on N, M)" → `depends_on: [<id-of-N>, <id-of-M>]`
- `budget_seconds` = proposal's effort estimate divided across nodes, rounded to one of `[900, 1800, 3600, 7200, 14400]`. If the proposal says "Task A ~1h / Task B ~0.5h" explicitly, honor that.
- `acceptance_criteria` = empty list at the node level UNLESS the proposal explicitly binds a criterion to a step (e.g. "Step 4: acceptance criterion 3 met"). The final node inherits any criteria not yet bound.

### 4. Write `dag.yaml`

```yaml
run_id: <PROP_FNAME>                           # e.g. 2026-05-17-banksync-circuit-breaker
project: <SLUG>
proposal: proposals-approved/<PROP_FNAME>.md
created_at: <iso8601 UTC>
status: in_progress                            # in_progress | completed | aborted | blocked

tasks:
  - id: 001-<slug>
    title: <human-readable title>
    kind: code                                 # code | research | draft | chore
    depends_on: []                             # empty for the entry task(s)
    budget_seconds: 1800
    target_repo: <org/repo>                    # for kind: code; copied from settings.yaml
    target_branch: main                        # default unless proposal overrides
    acceptance_criteria:
      - <evidenceable criterion>
    runner_status: pending                     # pending | dispatched | claimed_done | verified_done | verification_failed
    verifier_status: pending                   # pending | passed | failed
    spec_path: null                            # Curator fills when it generates the spec.yaml
    completed_at: null
    evidence:
      tests_passed: null
      pr_url: null
      files_changed: null

  - id: 002-<slug>
    title: ...
    depends_on: [001-<slug>]
    # ... same shape
```

**Single-writer discipline:** the Curator OWNS this file from here forward. After `define_run` writes it, no other skill touches it except via the documented runner_status / verifier_status flips per architecture §6.1.

### 5. Write `status.yaml`

```yaml
run_id: <PROP_FNAME>
status: in_progress
total_tasks: <N>
verified_done: 0
in_flight: 0
blocked: 0
pending: <N>
last_curator_tick: null                        # filled on first Curator heartbeat
last_event: "run defined by define_run"
```

### 6. Reply

Single short line back to whatever invoked you (usually `propose_change` resolve mode, which forwards to chat):

```
🚀 Run defined: <slug>/<PROP_FNAME> · <N> tasks · Curator picks up on next heartbeat (≤30 min).
```

That's it. The Curator drives from here.

## Edge cases

| Situation | Action |
|---|---|
| Proposal has no "Step-by-step plan" section | Refuse: "proposal missing Step-by-step plan section — needs `edit:` first." |
| Step text is too vague to make evidence-able | Refuse with the specific step quoted. |
| Proposal has a "Phase A / Phase B" sub-structure | Treat each phase's steps as nodes; `depends_on` flows phase-to-phase by default. |
| Proposal says "(could collapse to a single task if the operator prefers — kept as two for clarity)" | Keep the structure the proposal chose. Don't second-guess. |
| Only one step in the plan | Single-node DAG. Fine. The Curator + verifier still apply. |
| Acceptance criteria don't bind cleanly to steps | All criteria belong to the final node. Document the choice in `last_event`. |
| Target repo missing from proposal + settings.yaml | Refuse: "no target_repo derivable — proposal or settings.yaml needs to specify." |

## What this skill is not

- Not a runner — produces dag.yaml + status.yaml, nothing else.
- Not invoked directly by the operator — `propose_change` resolve mode is the only caller at v1.
- Not a re-planner. If the DAG turns out wrong mid-run, that's a `reject` + new proposal, not a `define_run` re-invocation.
- Not allowed to deviate from the proposal. Your job is mechanical translation, not creative re-interpretation.
