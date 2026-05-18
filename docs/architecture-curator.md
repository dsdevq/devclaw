# Project-Curator Architecture

**Owner:** the operator (architecture); VPS Kit + PC Kit (execution surfaces).
**Status:** Design doc, Phase 5.7. Sits above `~/.life/system/autonomous-overnight-architecture.md` (Phase 5.5 — already shipped 2026-05-17). Builds ON Phase 5.5 — does not replace.
**Last updated:** 2026-05-17.
**Scope:** how the operator drives **project direction** while Kit + Curator handle **execution autonomously**, with hard guardrails preventing fake-success and unbounded autonomy on un-understood projects.

---

## 1. Executive summary

**Shape: two concepts (Project + Task), one review boundary (Proposal), one execution unit (Run with DAG), one verification gate.**

the operator's atom of attention is the **Project**, not the task. Kit must understand a project deeply before acting on it (recon for existing repos; Socratic conversation for new ideas). For atomic work, Kit ships a Phase 5.5 task and pings on PR. For anything non-atomic, Kit drafts an RFC-style **proposal** that explains *what changes, why, the step-by-step, and the impact* — the operator reviews like a real design doc, approves or edits, and only then does the **Curator** take over autonomously, walking a **DAG** of tasks while internal blockers get resolved silently. The Curator pings the operator only at **Run completion** (or on the narrow enumerated list of real escalation cases). Every claimed-done task passes through an independent **verify-task** Evaluator before counting toward Run completion — preventing fake-success.

All of it sits on OpenClaw primitives (cron + skills + sub-agents + filesystem). No bespoke scheduler. No bespoke queue. No new long-lived daemons beyond what's already running. Phase 5.5's runners (`code-task`, `research-task`) are reused unchanged — the Curator is just another OpenClaw skill triggered by another OpenClaw cron.

---

## 2. The actors

### 2.1 VPS Kit (Layer 1 — Conversation)

**Owns:**
- All natural-language conversation with the operator about projects.
- Recognizing when an intent is project-shaped vs ambient chat.
- Invoking `project_init` for unknown projects (refusing other actions until recon/Socratic completes).
- Sizing estimate (atomic vs proposal-worthy).
- Invoking `task_intake` for atomic work OR `propose_change` for non-atomic.
- Presenting drafted proposals back to the operator, accepting `"ship it"` / edit feedback.
- On approval, invoking `define_run` to commit the run.

**Does NOT own:**
- Execution. Runs hand off to Curator.
- Mutating any spec.yaml or dag.yaml after intake — those are owned by Curator and runners.
- Anything in `~/.life/domains/` — curator (the memory curator, not the project Curator) owns that surface.

### 2.2 Project Curator (Layer 2 — Autonomous orchestrator)

A new OpenClaw skill (`project_curator`), heartbeat-fired by the `curator_30m` cron. Walks active **Runs** across all projects.

**Owns:**
- Reading every `~/.life/projects/*/runs/*/dag.yaml` where `status: in_progress`.
- Identifying ready tasks (dependencies satisfied, status `pending`).
- Generating per-task `spec.yaml` files under the run's `tasks/` dir.
- Spawning Phase 5.5 sub-agents (via `task_dispatch` — same primitive Phase 5.5 already uses).
- Invoking `verify-task` after each runner reports done.
- Tracking dag.yaml status; on Run completion: posting the announce to Telegram.
- Resolving small internal blockers silently (retry once with stronger context; reroute via parallel branches when possible).
- Escalating to the operator ONLY on the narrow list in §6.3.

**Does NOT own:**
- The work itself. Curator dispatches; runners do.
- The proposal. Proposals are drafted by `propose_change` and approved by the operator before Curator sees them.
- Filesystem mutation outside its run's directory.

### 2.3 Phase 5.5 runners (Layer 3 — Execution, already shipped)

`code-task` and `research-task` — unchanged from Phase 5.5 except for two small additions:

**Additions for Phase 5.7:**
- When a task runs under a project (path matches `~/.life/projects/<slug>/...`), load `plan.md` + `recon.md` (if present) + `runs/<run>/dag.yaml` (if task is in a run) for context.
- When a task completes, update its node in the run's `dag.yaml` (status, completed_at, evidence summary) — using a single Edit-tool call, following the same single-writer discipline as Phase 5.5.

### 2.4 verify-task skill (Layer 2.5 — Independent QA Evaluator)

New skill. Spawned by Curator after a runner reports `done`. **Different prompt than the runner.** Skeptical mode.

**Owns:**
- Reading the spec + result.json.
- For each acceptance criterion: verifying evidence (re-running tests in a fresh checkout, hitting `gh api` to confirm PR existence, reading claimed-changed files).
- Flipping status to `blocked` (reason `verification_failed`) if any criterion lacks proof.

**Does NOT own:**
- Re-doing the work. It's a checker, not a fixer.
- Negotiating with the runner. If verification fails, Curator decides what to do (retry once / escalate).

### 2.5 task_intake skill (updated from Phase 5.5)

**Phase 5.5 behavior:** writes free-floating spec.yaml under `~/.life/tasks/<id>/`.

**Phase 5.7 update:**
- Requires a project context (`project: <slug>` either explicit in intent or inferred from `target_repo`).
- Refuses `kind: code` on a project lacking required artifacts:
  - For existing repos: no `recon.md` → refuse, route to `project_init`.
  - For new projects: no `plan.md` → refuse, route to `project_init`.
- **Exception (Option B) for atomic work on unknown projects:** if the work is genuinely atomic and the project doesn't yet exist, auto-create a stub `~/.life/projects/<slug>/` with a minimal `plan.md` noting "no recon yet, atomic-only mode" and proceed. Stub mode is opt-out via `--strict` flag (defaults to lenient for atomic).
- For non-atomic work: ALWAYS refuse without an approved proposal in `proposals-approved/`.

### 2.6 propose_change skill (new — Layer 1 review boundary)

Drafts an RFC-style markdown design doc for non-atomic work. **NOT a one-shot generator** — produces a real, dense doc the operator reads like a design proposal:

- Motivation (tied to project goals from `plan.md`)
- What changes (concrete enumerated list)
- Step-by-step plan (the DAG made human-readable)
- Impact on existing functionality
- Risks + mitigations
- Acceptance criteria (must be evidenceable — Kit enforces this when drafting)
- Effort estimate

Lands as `~/.life/projects/<slug>/proposals/<date>-<short-slug>.md`. Telegram-posted for the operator's review. the operator responds with `"ship it"` / `"edit: <changes>"` / `"reject"`. On `"ship it"`, the doc moves to `proposals-approved/` and `define_run` is invoked.

### 2.7 define_run skill (new — Layer 1→2 handoff)

Takes an approved proposal and writes:
- `~/.life/projects/<slug>/runs/<proposal-slug>/dag.yaml` (tasks + deps + phases + parallelism)
- `~/.life/projects/<slug>/runs/<proposal-slug>/status.yaml` (initial: `in_progress`, 0/N tasks done)

After this, Curator picks up the run on its next heartbeat.

### 2.8 project_init skill (new — Layer 1 entry)

The gatekeeper. Called when the operator first references a project Kit doesn't know.

**Two arms:**

- **Existing repo:** spawn a sub-agent that clones the repo, reads the architecture (READMEs, top-level config files, key modules), maps the code shape, identifies patterns/conventions/TODOs/pain points. Writes `recon.md`. Returns to the operator with informed questions ("here's what I see, here's what I don't know yet, please answer these 5-8 questions"). After the operator answers, writes `plan.md` reflecting the joint understanding.
- **New project:** Socratic mode. 5 hardest decision-shaped questions first. Multi-turn conversation produces `plan.md` collaboratively.

In both cases: `conversation.md` is append-only and captures the full transcript for audit / future-Kit context-load.

### 2.8.1 Human-in-loop posture

**Default: announce and proceed.** Devclaw exists to do autonomous overnight work; blocking on explicit human acknowledgement at every stage defeats the purpose. The system's two upstream layers (`project_init` and `propose_change`) default to *proceed by default, correct by exception*:

- **`project_init`** writes `plan.md` (and `recon.md` for existing repos) in the same turn it's invoked. Socratic Q&A is bounded to **AT MOST one clarifying question**, and only when the operator's brief is genuinely ambiguous (no `target_repo` AND internally contradictory scope). Otherwise Kit picks the strongest defensible interpretation and records its calls in a dedicated `Assumptions` section so the operator can correct in a single chat reply.
- **`propose_change`** auto-promotes drafted RFCs from `proposals/` to `proposals-approved/` and invokes `define_run` immediately. The operator is notified, not gated. Their recourse is the resulting PR (merge or close); they may also reply `cancel` / `hold` to abort a Run that hasn't started yet.
- **Curator** is unchanged — it already runs autonomously and pings only on the narrow §6.3 escalation list and at Run completion.

**Hard-keep exceptions (still gated on explicit `ship it`):**

1. **`~/.life/domains/` writes.** Memory curator's sovereign surface. Any proposal that touches a domain file is drafted into `proposals/` (not `proposals-approved/`), and stays there until the operator replies `ship it`. `project_init` never writes to `~/.life/domains/` at all — it surfaces implications in `plan.md`'s "Domain implications" section.
2. **Paid infrastructure.** VPS deploy changes, paid GitHub Actions workflows, `openclaw.json` rewrites, anything with a non-trivial $ cost. Same draft-into-`proposals/`-and-wait pattern. Money decisions stay human-in-the-loop because cost regret is harder to roll back than code regret.

This posture inverts the Phase 5.7b default. Before: human review was the gate; auto-proceed was the exception. After: auto-proceed is the gate; human review is the exception, enumerated above. The trade is bounded — the PR review still exists, and the cancel/hold override exists for the narrow window between dispatch and first task — but the day-to-day friction of running the system drops to near-zero.

### 2.9 settings.yaml (per-project config, new file)

Lives at `~/.life/projects/<slug>/settings.yaml`. Captures opt-in choices:

```yaml
github_repo: dsdevq/finance-sentry        # optional; null for non-GitHub projects
github_visibility: private                # auto-detected via `gh api`
mirror_to_issues: true                    # opt-in flag, default false
default_base_branch: main
test_command: npm test                    # optional
notes: |
  Any project-specific preferences Kit should honor.
```

---

## 3. The domain map

| Domain | Owner | Layer |
|---|---|---|
| Project understanding (recon, plan) | `project_init` | 1 |
| Sizing decision (atomic vs proposal-worthy) | VPS Kit conversational turn | 1 |
| Proposal drafting | `propose_change` | 1 |
| Proposal approval | the operator (chat reply) | 1 (human) |
| Run definition (DAG) | `define_run` (after approval) | 1→2 handoff |
| Run orchestration (walk DAG, dispatch tasks, retry, escalate) | `project_curator` | 2 |
| Task execution (code or research/draft/chore) | `code-task` / `research-task` | 3 |
| Task verification | `verify-task` | 2.5 |
| Task spec creation | `task_intake` (atomic) OR Curator (run-bound) | 1 (atomic) / 2 (run-bound) |
| Spec mutation (status, dispatch_*, result_summary) | `task_update` rules; followed by all mutators | cross-cutting |
| GitHub Issue mirror (opt-in, private repos) | `task_intake` side-effect via `gh issue create` | 1 |
| Telegram channel I/O | OpenClaw Gateway + telegram plugin | infra |
| Scheduling (heartbeats, dispatch ticks) | OpenClaw `cron` (3 jobs: `task_dispatch_15m`, `curator_30m`, `lifekit-curator` 30m) | infra |
| Sub-agent spawn | OpenClaw `sessions_spawn` | infra |
| Memory mutation (`~/.life/domains/`) | `lifekit-curator` only | unchanged from Phase 5.5 |
| Project knowledge mutation (plan.md, recon.md) | `project_init` + the operator edits | 1 |

---

## 4. The lifecycle (end-to-end)

### 4.1 Atomic task on a known project

```
You (Telegram): "In dsdevq/lifekit-stack, fix the typo in README.md line 42."

VPS Kit:
  ├─ recognize: project=lifekit-stack, intent=atomic
  ├─ check: ~/.life/projects/lifekit-stack/plan.md exists? recon.md exists?
  │    └─ yes, both → proceed
  ├─ sizing: clearly atomic (1 file, 1-line change) → skip proposal
  ├─ invoke task_intake → writes ~/.life/projects/lifekit-stack/tasks/<id>/spec.yaml
  │    └─ if settings.mirror_to_issues && repo is private: also `gh issue create`,
  │       capture issue number in spec
  ├─ reply: "📋 task <id> · atomic · will run on next dispatch tick (≤15 min)"
  ↓
task_dispatch_15m cron fires:
  ├─ scan all ~/.life/projects/*/tasks/ AND ~/.life/projects/*/runs/*/tasks/
  ├─ pick this spec (status: ready)
  ├─ spawn code-task sub-agent
  ↓
code-task sub-agent:
  ├─ load context: plan.md, recon.md, settings.yaml
  ├─ clone, edit, commit, push, gh pr create (with `Closes #<issue>` if mirrored)
  ├─ write result.json with evidenced acceptance criteria
  ├─ update spec.yaml: status=done, completed_at, result_summary
  ↓
Curator next heartbeat sees no active run for this project → no Run-level action.
↓
Task is atomic → no verify-task Evaluator pass (atomic work skips verification —
  the task itself is small enough that runner's own checks are sufficient).
↓
Telegram: "✅ <task_id> · PR: <url>"
```

### 4.2 Atomic task on an UNKNOWN project (Option B — stub mode)

```
You: "In dsdevq/some-personal-script-repo, fix the typo in README."

VPS Kit:
  ├─ project=some-personal-script-repo: doesn't exist
  ├─ sizing: atomic
  ├─ Option B: auto-create stub:
  │    ~/.life/projects/some-personal-script-repo/
  │      ├─ plan.md (minimal: "Auto-created stub for atomic work; no recon yet.")
  │      └─ settings.yaml (mirror_to_issues: false default)
  ├─ proceed as atomic flow above
  ├─ reply: "📋 task <id> · atomic · note: stub project (no recon). Run recon
  │           anytime via /recon some-personal-script-repo."
```

### 4.3 Non-atomic work on a known project (the headline flow)

```
You: "In dsdevq/finance-sentry, rework BankSync retry logic to use a circuit
       breaker."

VPS Kit:
  ├─ project=finance-sentry: exists, recon.md present
  ├─ sizing: non-atomic (touches multiple modules, needs design)
  ├─ invoke propose_change
  ↓
propose_change drafts:
  ~/.life/projects/finance-sentry/proposals/2026-05-17-banksync-circuit-breaker.md
  (the markdown design doc — motivation, changes, steps, impact, risks, 
   acceptance criteria, effort estimate)
  ↓
Telegram: "📝 Proposal drafted: <title> at <path>. Review and reply
            'ship it', 'edit: <changes>', or 'reject'."

You (after reading): "ship it"
  ↓
VPS Kit moves proposal → proposals-approved/, invokes define_run
  ↓
define_run writes:
  ~/.life/projects/finance-sentry/runs/2026-05-17-banksync-circuit-breaker/
    ├─ dag.yaml      (5 tasks: migration → CB class || retry strategy → integrate → tests)
    └─ status.yaml   (in_progress, 0/5)
  ↓
Telegram: "🚀 Run started: <slug>. Curator owns it now. I'll ping you on
            completion or genuine blocker. (you can disengage now)"

─────────────────── YOU ARE NOT INVOLVED FROM HERE ───────────────────

Curator (next 30m heartbeat):
  ├─ scan ~/.life/projects/*/runs/*/dag.yaml where status: in_progress
  ├─ find banksync run, identify ready tasks (deps satisfied)
  ├─ generate spec.yaml for each ready task under runs/.../tasks/<id>/
  ├─ status: pending → status: dispatched (in dag.yaml)
  
task_dispatch_15m next tick:
  ├─ pick up ready specs
  ├─ spawn code-task sub-agents (parallel where DAG allows)
  
Each sub-agent:
  ├─ load context: plan.md + recon.md + dag.yaml + sibling tasks' status
  ├─ do the work, write result.json with evidenced acceptance criteria
  ├─ update its dag.yaml node: runner_status: claimed_done

Curator (next heartbeat):
  ├─ for each claimed_done task: spawn verify-task
  
verify-task (independent Evaluator):
  ├─ re-clone in a fresh dir
  ├─ for each acceptance criterion: check evidence
  │    - tests pass? re-run them. capture output.
  │    - PR opens? hit gh api.
  │    - files changed? read them, diff against spec.
  ├─ all pass → mark dag node: verified_done
  ├─ any fail → mark dag node: verification_failed, reason: <which criterion>

Curator (sees verification_failed):
  ├─ retry once with stronger context ("previous attempt failed: <reason>")
  ├─ after second failure → escalate to Telegram (this counts as a real blocker)

Curator (sees all dag nodes verified_done):
  ├─ status.yaml → completed
  ├─ post the Run-complete announce to Telegram with summary
  
Telegram: "🎉 Run complete: banksync-circuit-breaker.
            5/5 tasks verified done.
            PRs: <links>.
            What's next on finance-sentry?"
```

### 4.4 Genuine escalations (the narrow list)

Curator pings the operator mid-Run only on:

1. **Acceptance criteria cannot be met as defined** (proposal needs revision)
2. **Architectural decision discovered mid-work** that contradicts the proposal (not just an implementation choice)
3. **Security/compliance concern surfaced** during the work
4. **External system unavailable for >2 hours** (e.g., GitHub down, Plaid sandbox down)
5. **Same task failed verification 2x** (the runner can't seem to do this — needs human)
6. **The scope of work appears genuinely unattainable** given budget

That's the whole list. Everything else (failing tests, missing imports, type errors, naming choices, merge conflicts, lint warnings, library deprecations) is internal — Curator/runner resolve silently.

---

## 5. The file cascade — schemas

### 5.1 `~/.life/projects/<slug>/` directory shape

```
~/.life/projects/<slug>/
├── plan.md                                  ← living vision; the operator + Kit co-authored
├── recon.md                                 ← code analysis (existing repos only)
├── conversation.md                          ← append-only Kit↔the operator transcript audit
├── settings.yaml                            ← per-project config (§2.9)
├── proposals/                               ← RFCs in flight
│   └── <date>-<short-slug>.md
├── proposals-approved/                      ← archived approvals (history)
│   └── <date>-<short-slug>.md
├── runs/                                    ← active multi-task orchestration
│   └── <approved-proposal-slug>/
│       ├── dag.yaml                         ← task graph (§5.3)
│       ├── status.yaml                      ← run-level rollup (§5.4)
│       └── tasks/<task_id>/                 ← Curator-generated Phase 5.5 spec dirs
│           ├── spec.yaml
│           ├── result.json
│           └── run.log.jsonl
└── tasks/<task_id>/                         ← atomic tasks (no run wrapper)
    ├── spec.yaml
    ├── result.json
    └── run.log.jsonl
```

### 5.2 `proposals/<date>-<slug>.md` shape (the RFC)

```markdown
# <Concise title — what changes>

**Status:** proposed | approved | rejected
**Project:** <slug>
**Drafted:** <iso8601>
**Estimated effort:** <X tasks, ~Y hours agent time>

## Motivation
<2-4 sentences. Tied to project goals in plan.md.>

## What changes
<Bulleted list of concrete changes. Files, modules, behaviors.>

## Step-by-step plan
<Numbered list. Each step is a future task. Parallelism noted inline.>
1. <step 1>
2. (parallel with 3) <step 2>
3. (parallel with 2) <step 3>
4. (depends on 1, 2, 3) <step 4>
...

## Impact on existing functionality
<What gets touched. What might break. What data is at risk. What rolls back cleanly.>

## Risks + mitigations
<Bulleted list. Each risk paired with mitigation.>

## Acceptance criteria
<Each criterion MUST be evidenceable — testable, gh-api-checkable, file-checkable.
NOT: "the code is cleaner" (unevidenceable).
YES: "BankSyncJobTest.StormScenario_RetainsMaxPlaidCallsUnder5 passes".>

## Reply in chat to advance:
- "ship it" → moves to proposals-approved/, triggers define_run
- "edit: <changes>" → Kit redrafts
- "reject" → moves to proposals-rejected/ (kept for history)
```

### 5.3 `runs/<slug>/dag.yaml` shape

```yaml
run_id: 2026-05-17-banksync-circuit-breaker
project: finance-sentry
proposal: proposals-approved/2026-05-17-banksync-circuit-breaker.md
created_at: <iso>
status: in_progress | completed | aborted | blocked

tasks:
  - id: 001-migration
    title: Add BankSyncCircuitState migration
    depends_on: []                         # no deps
    kind: code
    runner_status: pending | dispatched | claimed_done | verified_done | verification_failed
    verifier_status: pending | passed | failed
    spec_path: tasks/001-migration/spec.yaml      # filled when Curator generates
    completed_at: null
    evidence:                              # filled by verify-task, the runner, or the watchdog
      tests_passed: null
      pr_url: null
      files_changed: null
      verification_failure_reason: null    # set by verify-task on AC failure, OR by task_dispatch watchdog (= "runner_silent_past_deadline")
      reaped_by_dispatcher: null           # set true by task_dispatch reap pass (§6.3.1)
      ghosted_by_watchdog: null            # set true by task_dispatch watchdog pass (§6.3.1)

  - id: 002-cb-class
    title: CircuitBreaker class + unit tests
    depends_on: [001-migration]
    kind: code
    # ... same shape as above

  - id: 003-retry-strategy
    title: IRetryStrategy implementation
    depends_on: [001-migration]            # parallel with 002 — both depend only on 001
    kind: code

  - id: 004-integrate
    title: Integrate into BankSyncJob
    depends_on: [002-cb-class, 003-retry-strategy]
    kind: code

  - id: 005-integration-test
    title: Storm scenario integration test
    depends_on: [004-integrate]
    kind: code

# Parallelism is inferred from depends_on. Tasks with the same depends_on set
# (and no inter-dependency) can run concurrently — subject to OpenClaw
# subagent.maxConcurrent cap.
```

### 5.4 `runs/<slug>/status.yaml`

```yaml
run_id: 2026-05-17-banksync-circuit-breaker
status: in_progress
total_tasks: 5
verified_done: 1
in_flight: 2
blocked: 0
pending: 2
last_curator_tick: <iso>
last_event: "dispatched 002-cb-class and 003-retry-strategy (parallel)"
```

### 5.5 `settings.yaml`

(See §2.9.)

### 5.6 Per-task `spec.yaml` (extends Phase 5.5)

Inherits from Phase 5.5 schema (`~/.life/system/autonomous-overnight-architecture.md` §5.2). New optional fields for project-bound tasks:

```yaml
project: <slug>                            # always present when project-bound
run: <run-slug>                            # present for run-bound tasks
github_issue: <number>                     # present when settings.mirror_to_issues triggered
watchdog_deadline: <iso8601 UTC>           # written by task_dispatch at dispatch time = dispatched_at + budget + 300s grace. consumed by §6.3.1 watchdog pass.
```

---

## 6. Concurrency, coordination, and verification

### 6.1 What enforces "no two agents touch the same file"

Same single-writer / append-only / write-once discipline as Phase 5.5 (`~/.life/system/autonomous-overnight-architecture.md` §6.1). Plus:

- `proposals/<slug>.md` — written once by `propose_change`, may be re-drafted on `"edit: ..."` feedback (full rewrite, atomic file replace).
- `proposals-approved/<slug>.md` — write-once via move from `proposals/`.
- `runs/<slug>/dag.yaml` — single-writer is Curator. Runners append to their own task node (one Edit call per completion) — Curator reconciles on next heartbeat.
- `runs/<slug>/status.yaml` — single-writer is Curator. Recomputed on every heartbeat from dag.yaml.

### 6.2 Parallelism caps

| Lane | Cap (v1) | Where set |
|---|---|---|
| `main` (inbound chat) | 4 | OpenClaw config |
| `subagent` (overnight runners + verify-task) | 3 | OpenClaw config |
| `cron` | 1 | OpenClaw config |
| Concurrent runs across projects | unbounded | (Limited by subagent lane via task spawn) |
| Tasks per run | unbounded (limited by DAG depth + subagent cap) | natural |

### 6.3 Internal-resolution vs escalation

**Internal (Curator resolves silently — no the operator ping):**
- Test fails after agent's change → debug, retry, fix
- Build error / type mismatch / missing import → fix
- Merge conflict in feature branch → resolve
- Naming, file layout, code style choices
- Library deprecation → pick the alternative
- Lint warnings → fix
- Two reasonable implementation choices → pick one (document choice in result.json.notes)
- Task fails verification once → retry with stronger context

**Escalate to the operator (Curator pings):**
1. Acceptance criteria cannot be met as defined
2. Architectural decision discovered mid-work that contradicts the proposal
3. Security/compliance concern surfaced
4. External system unavailable >2 hours
5. Same task failed verification 2x
6. Scope of work genuinely unattainable given budget

### 6.3.1 Runner ghosts (added 2026-05-18)

A runner can disappear between `sessions_spawn` returning a `run_id` and the runner's first dag/spec mutation — image pull fails, OOM, container exits before the first Edit-tool call, gateway transient. The dag node sits at `runner_status: dispatched` and the spec sits at `status: dispatched-*` with no progress signal the Curator can interpret. Pure §6.3 cases don't fire because there's no `result.json`, no failed test, no claimed-done node to verify.

**Mechanism (owned by `task_dispatch`, not Curator):**

1. On every dispatch, `task_dispatch` writes `watchdog_deadline: <dispatched_at + budget + 300s grace>` into the spec.yaml. Without this field, the spec is unwatchable — older specs from before this mechanism survive as-is (they only re-trigger the watchdog if they ever get re-dispatched).
2. On every cron tick (every 15 min), `task_dispatch` runs a **reap pass** before its watchdog pass: any `dispatched-*` spec whose runner produced a `result.json` / `findings.md` / `subagent_complete` event but never flipped the spec gets reaped to `done` with the artifact's values. This catches "runner finished, just forgot to update state" — the dominant failure mode of the Phase 5.7c run-bound research/draft/chore kinds whose skills aren't fully run-aware yet.
3. Then a **watchdog pass**: any `dispatched-*` spec past `watchdog_deadline` with no completion evidence gets flipped — atomic specs to `status: blocked`, run-bound specs to `status: blocked` AND dag node to `runner_status: verification_failed` with `evidence.verification_failure_reason: runner_silent_past_deadline`.
4. The Curator's existing retry-once path picks up the `verification_failed` node on its next heartbeat. `runner_silent_past_deadline` is on the internally-resolvable list (one retry with original budget). Second ghost → §6.3 case 5 escalation.

**Why `task_dispatch` and not Curator owns this:** `task_dispatch` already scans every spec on a 15-min cadence and already owns the `status: dispatched-*` write. Splitting the watchdog into a separate skill or moving it to Curator would double the scan and split the write contract.

**Why a watchdog instead of a runner-side heartbeat:** the dominant ghost mode is the runner never starting (image pull, OOM, sessions_spawn-returns-but-process-dies). A heartbeat inside `code-task` can't fire if the runner never gets to its first Bash tool call. The watchdog is external and indifferent to runner liveness.

### 6.4 Verification gate (Generator/Evaluator pattern)

After every runner reports `claimed_done`, Curator spawns `verify-task` with:
- The spec
- The result.json
- Fresh container scratch dir

`verify-task`:
1. Re-clones the target branch (NOT main — the runner's feature branch).
2. For each acceptance criterion, runs the EXACT command that proves it:
   - "Tests pass" → `npm test` (or whatever's in settings.test_command), capture output, require non-zero exit signals failure.
   - "PR opens" → `gh api repos/<org>/<repo>/pulls/<n>` → require 200.
   - "File X exists with Y content" → Read + grep.
3. If any check fails → flip dag node to `verification_failed`, reason = which criterion + the failing output.
4. If all checks pass → flip to `verified_done`, capture evidence summary.

Atomic tasks (not run-bound) skip verify-task — the runner's own checks are sufficient.

---

## 7. The ramp (build order)

Phase 5.7 ships in four sub-phases. Each is independently dogfoodable.

| Phase | Deliverable | Effort | Notes |
|---|---|---|---|
| **5.7a** | `project_init` skill + recon arm + Socratic arm + hard guardrail in `task_intake` | 1 session | First real value: Kit refuses to act on un-understood projects. Manual task drops still work as today. |
| **5.7b** | `propose_change` skill + RFC review flow + `proposals-approved/` archive | 1 session | The PO/dev boundary lands. You read RFCs in chat and approve/edit. Curator not built yet — Curator behavior is done manually by you using Phase 5.5 task drops referencing the approved proposal. |
| **5.7c** | `define_run` + `project_curator` + `verify-task` + `curator_30m` cron + dag.yaml/status.yaml | 1-2 sessions | The autonomous orchestrator lands. Approved proposals execute hands-free. Verification prevents fake-success. |
| **5.7d** | Optional GitHub Issues mirror (settings.yaml + `gh issue create` side-effect) | 1/2 session | Phone-friendly visibility for private repos. Opt-in per project. |

**Total:** 3-4 focused sessions to ship Phase 5.7 end-to-end.

### What stays deferred

- **Phase 6 (swarm wiring)** — Curator now covers what swarm would do for project work. Defer indefinitely; reassess only if true "multi-week no-human-touch builds" become a real need.
- **Cross-project dependencies** — every project is sovereign at v1.
- **Project-level dashboard UI** — `cat plan.md` + morning brief rollup are sufficient.
- **Curator self-improvement / auto-replan-from-failure** — at v1, after a 2x verification failure, escalate. Don't try to auto-replan around it.

---

## 8. What this architecture explicitly forbids

The rules that prevent rot. If code violates one, the code is wrong, not the rule.

1. **No execution without project understanding.** `kind: code` against an unknown project (no recon for existing, no plan for new) is REFUSED. The Option B stub-mode for atomic-on-unknown is the ONLY exception, and even it auto-creates the project artifact.
2. **No code-shaped non-atomic work without an approved proposal.** Phase 5.5's `task_intake` refuses unless `proposals-approved/<matching-slug>.md` exists.
3. **No false-success.** Tasks in a run cannot be `verified_done` without passing `verify-task`. Acceptance criteria must be evidenceable assertions, not vibes.
4. **No skill writes to `~/.life/domains/`.** That's `lifekit-curator`'s sovereign surface, unchanged from Phase 5.5.
5. **No bespoke scheduler / queue / locking.** OpenClaw cron + filesystem + single-writer discipline IS the substrate.
6. **No private data in public-repo GitHub Issues.** Mirror is gated on `github_visibility: private` AND opt-in `mirror_to_issues: true`. Default is no mirror.
7. **No Jira / external PM tools.** Personal solo autonomous agent doesn't warrant enterprise PM tooling. Reassess if/when this stack ever serves a team.
8. **No cross-project DAG dependencies.** Each project sovereign. If a project's work blocks another's, that's a human decision, not an autonomous one.
9. **No Curator decisions outside the published rules.** Internal-resolution scope is enumerated (§6.3). Escalation list is enumerated (§6.3). If a situation doesn't fit either, Curator escalates by default.
10. **No mid-run scope creep.** Once a proposal is approved and a run is in flight, the run completes against the original scope. Scope changes require: new proposal, your approval, new run.
11. **No proposal without effort estimate + acceptance criteria.** `propose_change` refuses to draft without both. They're the contract.
12. **No frontmatter field added to any schema without updating this doc first.** Schemas are contracts; the doc IS the schema.

---

## 9. Open questions (for review before / during build)

1. **DAG schema evolution.** The §5.3 shape is the v1. As we hit edge cases (a task needing to inject new tasks mid-run? optional tasks? task retries with backoff?), the schema may need additions. Plan: extend, don't fork.

2. **Sub-agent context for project-bound tasks.** OpenClaw sub-agents only inherit `AGENTS.md` + `TOOLS.md` (per Phase 5.5 §10). For project-bound work, sub-agents need to load `plan.md` + `recon.md` + `dag.yaml`. Approach: the spec.yaml carries explicit `context_files: [...]` and the runner Read-tools those first. Confirm pattern in 5.7a build.

3. **Run-level checkpoint / pause.** Today's design: kill switch is `~/.life/system/cron-paused` (all-or-nothing). Per-project or per-run pause is deferred until the multi-project case actually contends. Document as Phase 7+.

4. **Proposal vs Run lifecycle when scope changes mid-conversation.** If the operator is iterating on a proposal (`"edit: ..."`) but already approved an earlier version — what happens? Decision: approval is at the file level. Once moved to `proposals-approved/`, it's locked. Edits before approval are full re-drafts of the proposals/ file.

5. **GitHub Issue mirror failure modes.** What if `gh issue create` fails (rate limit, network)? Decision: fail-soft. Task proceeds without mirror. Log a `mirror_failed` event in run.log.jsonl. the operator sees it in morning brief if he cares.

6. **Curator session-context size.** Curator reads many dag.yaml files per heartbeat. As projects multiply, context grows. Mitigation: Curator is `light-context` (per Phase 5.5 cron settings). Reads only the dag.yaml files, not the whole plan.md/recon.md. Revisit if context exhaustion hits.

7. **What's the "context" runners load?** For atomic tasks: spec.yaml only. For run-bound tasks: spec.yaml + plan.md + recon.md + dag.yaml. For verify-task: spec.yaml + result.json + dag.yaml (to know acceptance criteria + sibling task state). Locked in 5.7c.

---

## 10. One-line summary

**the operator drives direction at the project level. Kit understands projects deeply (recon for existing, Socratic for new), drafts RFC-style proposals for non-atomic work, and hands approved proposals to a heartbeat-driven Curator that walks a DAG of tasks autonomously — with an independent verify-task Evaluator gating every task's done-claim against evidenceable acceptance criteria. Telegram pings only at run completion or genuine blockers. All on OpenClaw primitives, all in `~/.life/projects/<slug>/`, no swarm needed.**
