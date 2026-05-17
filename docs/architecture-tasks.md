# Autonomous Overnight Architecture

**Owner:** the operator (architecture); VPS Kit + PC Kit (execution surfaces).
**Status:** Design doc. Phase 0 (sync only) is current; this defines the target state through Phase 6.
**Last updated:** 2026-05-17.
**Scope:** how the operator delegates complex tasks at bedtime and wakes to either completed artifacts or clear blockers, without the system collapsing into spaghetti as task volume grows.

---

## 1. Executive summary

**Shape: a single source of intent, two execution paths, one ledger.**

Telegram is the only mouth the operator uses to start an overnight task. Every intent lands as a *Task Spec* — a versioned directory under `~/.life/tasks/<id>/` with a YAML frontmatter contract — written by exactly one component (the **Intake skill** on VPS Kit). From there, OpenClaw's native automation primitives decide *when* work runs (cron + scheduled tasks), and a strict typed router decides *where*:

- **Ambient/research/draft/curation/answer-shaped** tasks run as **OpenClaw sub-agents** (`sessions_spawn`) on the VPS, isolated per task, results announced back to the operator's Telegram thread.
- **Code-shaped/build-shaped** tasks are POST-handed-off to **swarm** (`project_builder` graph on `127.0.0.1:2024`) via the existing **BuildEngine port**. swarm runs the long autonomous build loop in its own sandbox; OpenClaw only owns the dispatch + the completion callback that announces a PR URL into Telegram.

The **lifekit-curator** stays the only writer to `~/.life/domains/`. The **Task Spec directory** is the only writer-coordinated surface in `~/.life/tasks/`. Everything else reads. Concurrency is bounded by OpenClaw's existing per-session lanes (`main`/`subagent`/`cron`/`cron-nested`/`nested`) — no new queue. Observability is the existing OpenClaw `tasks` ledger plus a thin `~/.life/tasks/<id>/run.log.jsonl` that swarm and sub-agents both write into.

the operator runs on **Claude Max** with substantial headroom — inference cost is not a design constraint. The ceilings that *are* real: (a) Max-plan concurrent-session cap (low double digits), (b) the ARM cax11 VPS's 2c/4GB resource cap on parallel sub-agent processes, (c) Anthropic's autonomous-SDK restriction that forces swarm onto Codex CLI by June 2026. The architecture is designed so concurrency scales naturally with whichever of those ceilings lifts first.

That's the entire shape. No orchestrator. No supervisor. No bus. One intake, one schema, two runners, one ledger. Everything below is just the contract.

---

## 2. The actors

Eight long-lived components. Each has explicit non-responsibilities — those matter as much as the owned concerns.

### 2.1 OpenClaw Gateway (VPS, `openclaw-gateway.service`)

**Owns:**
- All inbound channels (Telegram bot, voice, future iOS).
- Session lifecycle, queue lanes, sub-agent spawning (`sessions_spawn`), cron, heartbeat.
- Skill loading from `~/.openclaw/workspace/skills/`.
- The `tasks` ledger (`$OPENCLAW_STATE_DIR/tasks/runs.sqlite`) for ACP/subagent/cron/CLI runs.
- The completion announce pipe back to whichever channel the requester is on.

**Does NOT own:**
- The Task Spec format (defined here, written by the Intake skill).
- `~/.life/domains/` mutation (curator only).
- Code-shaped autonomous execution (swarm only).
- Any direct file write to `~/.life/` outside `~/.life/queue.jsonl` and `~/.life/tasks/`.

### 2.2 VPS Kit (the OpenClaw default agent, persona Kit)

The Claude Max session that OpenClaw drives via `agentRuntime.id: "claude-cli"`. There is exactly one default agent at v1; multi-agent (`agents.list[]`) is deferred until single-agent contention is actually observed — not preemptively.

**Owns:**
- Conversational triage of inbound text/voice.
- Decision: "is this a task?" — if yes, invoke the **Intake skill**, which writes the Task Spec.
- Heartbeat checkup (HEARTBEAT.md, opted-in once Phase 2 of the OpenClaw trust ramp lands).
- Running individual *ambient* sub-agents that don't need swarm (e.g. "draft a blog response", "research X and append to ledger").

**Does NOT own:**
- Long-horizon autonomous code-writing. Routes those to swarm via the **dispatch-to-swarm** skill.
- Mutating `~/.life/domains/`. Writes capture events to `~/.life/queue.jsonl` for the curator.
- Cron logic. Cron belongs to OpenClaw; VPS Kit is *called by* cron, not the cron runner.

### 2.3 swarm (`swarm-langgraph.service`, `127.0.0.1:2024`)

LangGraph `project_builder` graph. Single endpoint, single graph, narrow contract.

**Owns:**
- Multi-hour autonomous build loops: intake → feasibility → plan → execute → verify → advance/replan/halt.
- Its own project workspaces under `~/.personal-agent/projects/<slug>/` (incl. `workspace/`, `goal.yaml`, `goal.lock.yaml`, `history.jsonl`).
- All `claude-agent-sdk` (→ Codex CLI by June 2026) calls for code-writing.
- Internal budget bookkeeping (`Budget`: iteration, tokens_spent, replans).

**Does NOT own:**
- Inbound channels. swarm has no Telegram, no users, no UI.
- Scheduling. swarm runs when called, not when timed.
- Anything in `~/.life/domains/`.
- The Task Spec format. swarm consumes a minimal `goal.yaml` derived from the Task Spec by the **BuildEngine adapter** (below).

### 2.4 lifekit-curator (`lifekit-curator.service`, planned Phase 3.5 extraction)

**Owns:**
- Draining `~/.life/queue.jsonl` → proposing domain edits → git-committing to `~/.life/`.
- Daily curator digest at `~/.life/journal/curator/YYYY-MM-DD.md`.
- The supervised/unsupervised flag and the auto-apply allowlist for sensitive domains.

**Does NOT own:**
- Writing to `~/.life/tasks/`. Tasks are not domain knowledge.
- Inbound capture. It only drains what others wrote into the queue.

### 2.5 BuildEngine adapter (new, lives inside an OpenClaw skill)

A thin **TypeScript** layer (per the TypeScript-default rule; Python only if `claude-agent-sdk` integration forces it — it doesn't, the adapter just HTTP-POSTs LangGraph). Implements the `BuildEngine` port already documented in `~/.life/system/adapters.md`.

**Owns:**
- Translating a Task Spec → swarm's `goal.yaml` shape (project slug, intent, acceptance criteria).
- POSTing to `127.0.0.1:2024/threads/<thread_id>/runs/wait` with deterministic `thread_id = uuid5(NS, task_id)` so a re-dispatched task resumes the same swarm thread.
- Receiving swarm's final state, writing a result JSON into `~/.life/tasks/<id>/result.json`, and calling back into OpenClaw's `message` tool to announce.
- Surfacing swarm budget exhaustion / halt as a "blocked, needs human" state.

**Does NOT own:**
- The build loop itself. It just dispatches and reports.
- Retrying failed builds. Swarm halts → spec moves to `status: blocked`. the operator re-runs explicitly.

### 2.6 Intake skill (new OpenClaw workspace skill)

`~/.openclaw/workspace/skills/task_intake/SKILL.md`. The *only* writer of new Task Spec directories.

**Owns:**
- Validating the intent text against the Task Spec schema (§5).
- Generating `task_id = YYYY-MM-DD-<slug>-<rand4>`, creating `~/.life/tasks/<task_id>/`, writing `spec.yaml`.
- Classifying `kind` (`research` | `draft` | `code` | `chore` | `decision`) — this drives routing.
- Returning the spec path to VPS Kit so it can confirm with the operator.

**Does NOT own:**
- Execution. The skill returns; OpenClaw cron or VPS Kit picks the spec up next.
- Editing existing specs. Spec updates go through a separate `task_update` skill (§5) so the only-writer rule survives.

### 2.7 Dispatch skill (new OpenClaw workspace skill)

`~/.openclaw/workspace/skills/task_dispatch/SKILL.md`. Reads ready specs and routes them.

**Owns:**
- Reading `~/.life/tasks/*/spec.yaml` where `status: ready`.
- For `kind: code` → calls BuildEngine adapter, transitions `status: dispatched-build`.
- For `kind: research|draft|chore` → calls `sessions_spawn` with the spec as input, transitions `status: dispatched-subagent`.
- For `kind: decision` → posts the spec to Telegram for the operator, no autonomous execution.

**Does NOT own:**
- Scheduling. Run by an OpenClaw cron at the configured cadence (e.g. `*/15 * * * *` overnight, or `--at` for one-shot).
- Mutating spec content outside the `status` and `dispatch_*` frontmatter fields.

### 2.8 The Task Spec directory (`~/.life/tasks/`)

Not a process — the *contract surface* between humans, runners, and reporters. See §5.

---

## 3. The domain map

Every concern in the system maps to exactly one owner. If you find yourself wanting to put something in two places, you've found a bug in this map; fix the map, then the code.

| Domain | Owner | Notes |
|---|---|---|
| **Channel I/O** (Telegram in/out, voice, media) | OpenClaw Gateway | Bind ports already exist in stack. |
| **Intent triage** ("is this a task?") | VPS Kit conversational turn | Pure model judgment. |
| **Task Spec creation** | Intake skill | Only writer of new `~/.life/tasks/<id>/spec.yaml`. |
| **Task Spec update** (`status`, `dispatch_*`, `result_*`) | task_update skill called by Dispatch / Sub-agent / BuildEngine adapter | One skill, controlled mutation list. |
| **Scheduling / timing** | OpenClaw cron + heartbeat | No new scheduler. |
| **Routing decision** (where does this task run?) | Dispatch skill | Driven by `kind` field, not by content inspection. |
| **Sub-agent execution** (research, draft, chore) | OpenClaw `sessions_spawn` | Bounded concurrency on `subagent` lane. |
| **Build execution** (code-shaped autonomous work) | swarm `project_builder` graph | Owns its own workspace + budget. |
| **Memory mutation** (`~/.life/domains/`) | lifekit-curator | Git-committed, auditable. |
| **Memory capture** (events going into the queue) | Any actor via append-only `~/.life/queue.jsonl` writes | Append-only is conflict-free. |
| **Human handoff** ("blocked, needs you") | BuildEngine adapter / Dispatch skill / Sub-agent announce | All route through OpenClaw `message` tool to Telegram. |
| **Observability ledger** (what ran, when, outcome) | OpenClaw `tasks` SQLite + per-task `run.log.jsonl` | One canonical store, one human-readable per-task log. |
| **Budget control** (model spend per task) | swarm internal `Budget` for builds; sub-agent `runTimeoutSeconds` for OpenClaw runs | No global token meter at v1. |
| **Failure escalation** | OpenClaw cron `failureDestination` + spec `status: blocked` | Single escalation pipe. |
| **Concurrency caps** | OpenClaw lanes (`agents.defaults.subagents.maxConcurrent`, `cron.maxConcurrentRuns`) | No bespoke lock layer. |
| **Sandboxing** | OpenClaw per-agent `sandbox` config + swarm's own project_dir isolation | Two layers, two scopes; no overlap. |
| **Persona/voice** | OpenClaw SOUL.md + IDENTITY.md (Kit) | Only one source. |

---

## 4. The task lifecycle

End-to-end path from intent to artifact, with explicit owners at each hop. Failure paths and the blocked-needs-human path are inline.

### 4.1 Happy path

1. **Intent.** the operator speaks/types into Telegram: *"Overnight: research the cheapest Hetzner ARM SKU that can run Whisper-large + Ollama at the same time, write the comparison into `~/.life/system/proposals.md` as a draft."*
   - **Owner:** Telegram → OpenClaw Gateway.

2. **Triage.** VPS Kit's conversational turn decides "this is a task, not a chat." Invokes the **Intake skill** with the verbatim intent.
   - **Owner:** VPS Kit.

3. **Spec creation.** Intake skill writes `~/.life/tasks/2026-05-17-hetzner-arm-research-a7f2/spec.yaml` with `kind: research`, `status: ready`, `created_at`, `requester_route` (Telegram chat id), the verbatim intent, and a parsed acceptance criteria list. Skill returns the spec path; VPS Kit confirms with the operator: *"Created task 2026-05-17-hetzner-arm-research-a7f2 (research). Will dispatch on the next 15-min tick. OK?"*
   - **Owner:** Intake skill.

4. **Dispatch.** OpenClaw cron `task-dispatch` (e.g. `*/15 * * * *`, session `isolated`, light context) fires. The Dispatch skill enumerates `status: ready` specs, classifies by `kind`, and either:
   - `code` → calls BuildEngine adapter; spec becomes `status: dispatched-build` with `dispatch_target: swarm`, `dispatch_run_id: <swarm thread id>`.
   - Else → calls `sessions_spawn` with a task prompt that references the spec path; spec becomes `status: dispatched-subagent` with `dispatch_target: subagent`, `dispatch_run_id: <openclaw task id>`.
   - **Owner:** Dispatch skill (invoked by OpenClaw cron).

5. **Execution.**
   - **Sub-agent path:** the spawned sub-agent reads the spec, does the work, writes intermediate notes into `~/.life/tasks/<id>/run.log.jsonl`, writes the final artifact wherever the spec said (e.g. proposes a patch under `~/.life/.curator-proposed/` if it wants a `~/.life/domains/` mutation, or writes a draft file under `~/.life/tasks/<id>/output/`). On finish, OpenClaw announces back to Telegram via the announce pipeline.
   - **Build path:** swarm walks its graph. On `finalize_done` / `finalize_halt` / `finalize_budget`, BuildEngine adapter receives the final state, writes `~/.life/tasks/<id>/result.json`, transitions spec `status: done|blocked`, calls OpenClaw `message` to announce.
   - **Owner:** sub-agent or swarm.

6. **Completion.** Spec frontmatter updated to `status: done`. A one-line `result_summary` is filled. The OpenClaw `tasks` SQLite has the formal record; the per-task `run.log.jsonl` has the human-readable trace. the operator wakes up, scans `~/.life/tasks/`, opens the few that need eyes.

### 4.2 Failure paths

| Failure | Detected by | Action | Spec terminal status |
|---|---|---|---|
| Sub-agent timeout (`runTimeoutSeconds` exceeded) | OpenClaw tasks runtime | Task marked `timed_out`; announce posts the timeout; spec moves to `blocked` with `result_summary: "timed out at <duration>"` | `blocked` |
| Sub-agent error | OpenClaw tasks runtime | Task marked `failed`; announce posts the error; spec → `blocked` | `blocked` |
| Swarm halt-infeasible | swarm `finalize_halt` node | BuildEngine adapter writes the `halt` rationale into `result.json`; spec → `blocked`; announce posts the rationale | `blocked` |
| Swarm budget exhausted | swarm `finalize_budget` | Same as halt, but `result_summary` flags budget vs feasibility so the operator can choose to raise budget and re-dispatch | `blocked` |
| Dispatch skill cron itself fails | OpenClaw cron `failureDestination` | OpenClaw posts the cron failure to Telegram failure channel | n/a (spec stays `ready`) |
| Gateway restart mid-run | OpenClaw `tasks audit` sweeper (every 60s) | Run marked `lost` after 5-min grace; reconciliation surfaces it; spec stays `dispatched-*` until human intervenes | n/a until human |

### 4.3 Blocked-needs-human

A single, well-known channel: **the spec moves to `status: blocked` and OpenClaw `message` announces the blocker to Telegram with a `/tasks blocked` count summary in the morning brief.** No bespoke "needs review" inbox. The morning brief's existing skill (already wired) is extended to read `~/.life/tasks/*/spec.yaml` where `status: blocked` and list them.

---

## 5. The "task" primitive

### 5.1 Directory shape

```
~/.life/tasks/
├── 2026-05-17-hetzner-arm-research-a7f2/
│   ├── spec.yaml              # the contract (frontmatter only; mutated via task_update skill)
│   ├── run.log.jsonl          # append-only execution trace (any actor may append)
│   ├── result.json            # written exactly once, by the executor at terminal status
│   └── output/                # optional; artifacts (drafts, patches, screenshots, generated files)
│       └── proposals-draft.md
├── 2026-05-17-add-curator-supervised-flag-build-b903/
│   ├── spec.yaml
│   ├── run.log.jsonl
│   ├── result.json            # contains PR URL when kind=code completes
│   └── output/                # for code tasks: not used (the artifact is the PR in the project repo)
└── INDEX.md                   # auto-regenerated by a /tasks status cron; human-readable table
```

### 5.2 `spec.yaml` schema

```yaml
# --- created once by Intake skill, never re-written holistically ---
task_id: 2026-05-17-hetzner-arm-research-a7f2
created_at: 2026-05-17T22:43:11Z
created_by: vps-kit                          # always vps-kit at v1
requester_route:                             # OpenClaw route — enables the announce pipe
  channel: telegram
  to: "<your-chat-id>"
  account_id: default
verbatim_intent: |
  Overnight: research the cheapest Hetzner ARM SKU that can run
  Whisper-large + Ollama at the same time, write the comparison into
  ~/.life/system/proposals.md as a draft.
kind: research                               # research | draft | code | chore | decision
acceptance_criteria:
  - At least 3 SKUs compared on price, RAM, vCPU, ARM-vs-x86
  - Conclusion picks one with reasoning
  - Output is a markdown patch proposed under ~/.life/.curator-proposed/
budget:
  max_runtime_seconds: 7200                  # 2 hours; sub-agent runTimeoutSeconds maps to this
  max_subagent_spawns: 0                     # research kind: leaf only at v1

# --- mutated by task_update skill only; mutation contract: only these fields, only these transitions ---
status: ready                                # ready → dispatched-* → done|blocked
dispatch_target: null                        # subagent | swarm | human
dispatch_run_id: null                        # OpenClaw task id OR swarm thread id
dispatched_at: null
completed_at: null
result_summary: null                         # one line; full data lives in result.json
```

### 5.3 Invariants

These must hold or the system is broken. Each one is a one-line property check the Dispatch skill verifies before acting.

1. **Single-writer for spec.yaml.** Only the Intake skill creates; only the task_update skill mutates; only the four mutable fields (`status`, `dispatch_*`, `result_summary`) ever change. Anyone touching the file outside that skill violates the contract.
2. **Append-only for run.log.jsonl.** Any actor may append; nobody overwrites. Conflict-free by construction.
3. **Write-once for result.json.** Written exactly once, at terminal status. If a file already exists, the runner halts with `result_already_exists`.
4. **Status monotonicity.** Allowed transitions: `ready → dispatched-* → done|blocked`. No `blocked → ready` auto-resume — that's a human re-dispatch (which is just deleting the spec dir and recreating).
5. **`kind` is set at intake and immutable.** A task is one shape forever. If the shape was wrong, file a new task.
6. **`requester_route` is captured at intake.** This is what makes announce idempotent. Without it, dispatch refuses to run.
7. **No spec writes from anything other than the OpenClaw workspace.** PC Kit may read, never write. Enforced socially by the curator + audit; not enforced by FS permissions at v1.

---

## 6. Concurrency model

The system handles multiple overnight tasks without bespoke locking, because every primitive is either single-writer-single-file or append-only.

### 6.1 What enforces "no two agents touch the same file"

| Surface | Mechanism |
|---|---|
| `~/.life/domains/*.md` | Only `lifekit-curator` writes. Every other actor reads. |
| `~/.life/tasks/<id>/spec.yaml` | Single-writer contract (§5.3). Status transitions are monotonic, so two dispatchers racing on `ready → dispatched-*` resolve to "second one sees `dispatched-*`, skips." |
| `~/.life/tasks/<id>/run.log.jsonl` | Append-only with `O_APPEND` semantics from any writer; POSIX guarantees atomic line-level appends. |
| `~/.life/tasks/<id>/result.json` | Write-once. Runner checks existence first. |
| `~/.life/tasks/<id>/output/` | One owner per task (the dispatched executor). |
| `~/.life/queue.jsonl` | Append-only. |
| OpenClaw sessions | OpenClaw already serializes per session via the `session:<key>` lane (`/concepts/queue`). No extra work needed. |
| swarm project_dirs | swarm uses one `project_dir` per slug and writes `goal.lock.yaml` atomically; concurrent invocations of the same slug share the same LangGraph thread (deterministic `thread_id`) so re-dispatch resumes rather than races. |

### 6.2 How parallelism is *bounded*

| Lane | Cap | Set where | Purpose |
|---|---|---|---|
| `main` | `agents.defaults.maxConcurrent` (default 4) | OpenClaw config | Inbound chat replies; not for overnight tasks. |
| `subagent` | `agents.defaults.subagents.maxConcurrent` (default 8) | OpenClaw config | Cap on parallel overnight sub-agents. **Recommend: 3 at v1.** ARM VPS is small. |
| `cron` / `cron-nested` | `cron.maxConcurrentRuns` | OpenClaw config | Cap on concurrent cron-spawned isolated runs. **Recommend: 1.** Dispatch is fast; we don't need many cron runners. |
| swarm | LangGraph thread serialization per `thread_id` | swarm itself | Same task slug never runs twice concurrently. Different slugs share the single LangGraph process; LangGraph handles its own scheduling. |

**Verdict:** at v1, **3 concurrent overnight sub-agents + 1 in-flight swarm build** is the cap. The ceiling is *VPS hardware*, not Claude inference cost (Max has plenty of headroom) and not Max-plan session concurrency (it'd allow ~10+ before any plan-side rate limit kicks in). When the operator lifts the VPS to something bigger — or moves swarm off the cax11 — these caps lift in one config change (`agents.defaults.subagents.maxConcurrent`). No architectural rewrite; the lanes already exist.

**On model selection per role.** Don't pick by cost — pick by fit. Defaults:

| Role | Model | Why |
|---|---|---|
| VPS Kit (default conversational + intake triage) | Opus 4.7 | Triage decisions cascade; bad triage breaks downstream routing. |
| Dispatch skill orchestration | Opus 4.7 | Same — routing mistakes are expensive. |
| Research / draft sub-agents | Sonnet 4.6 | Long context, good reasoning, faster than Opus, runs many in parallel cleanly. |
| Chore sub-agents (formatting, extraction, classification) | Haiku 4.5 | Cheap-and-fast wins; quality ceiling is rarely the bottleneck for these. |
| swarm `project_builder` agent loops | Codex CLI (post-June 2026 migration) | Anthropic policy + swarm is already migrating. |

These are defaults; `spec.yaml` may carry an explicit `model:` override (see §7.3) when a task wants something else. The point: role-fit drives the pick, not budget. Max enables this without trade-off.

### 6.3 Conflict resolution

There is none, because there are no conflicts:
- Two tasks editing the same domain file? Impossible — only curator writes domains. Two tasks both proposing a curator patch land as separate files in `~/.life/.curator-proposed/<task_id>.patch`; curator applies them in order, conflicts surface as patch failures.
- Two tasks writing to the same output? Impossible — `output/` is task-local.
- Two tasks racing on the queue? Impossible — appends are atomic.

---

## 7. The ramp

What ships when. Tonight's MVP is small; everything else is deferred behind real prerequisites.

### 7.1 Tonight (MVP — research/draft only, no swarm wiring)

What runs end-to-end without writing TypeScript:

- **Intake skill** + **Dispatch skill** + **task_update skill** as three OpenClaw workspace skills (Markdown SKILL.md + minimal Python helpers in `lifekit` if needed; nothing custom outside what skills already support).
- `~/.life/tasks/` directory + schema documented (this file).
- Dispatch cron: `*/15 * * * *` overnight (active hours `22:00–07:00 Europe/Dublin`), session `isolated`, calls Dispatch skill.
- Sub-agent execution for `kind: research|draft|chore` via `sessions_spawn`. swarm path is stubbed: `kind: code` specs go to `status: blocked` with `result_summary: "code dispatch not yet wired — Phase 6"`.
- Morning brief skill extended to list `~/.life/tasks/*/spec.yaml` where `status in [blocked, dispatched-*]` so the operator wakes up to the digest.

This is enough to delegate **research/draft/chore** overnight tonight. It dogfoods the whole spec → dispatch → sub-agent → announce loop without touching swarm.

### 7.2 Phase 5.5 — lightweight `kind: code` via sub-agent + skill (added 2026-05-17)

Surfaced from the AFK-dev-agent survey (`~/.life/system/afk-dev-agent-survey.md`) after studying Hermes Agent's autonomous-coding pattern: **simple code tasks don't need swarm**. Hermes proves that a SKILL.md teaching the LLM how to delegate is sufficient for bounded code work. Translating that pattern into our stack:

- A new **`code-task` workspace skill** instructs a Claude Max sub-agent (spawned via the existing `sessions_spawn` primitive — same runner as research/draft) to:
  1. Read the Task Spec at `~/.life/tasks/<id>/spec.yaml`
  2. Clone the target repo into `/tmp/<task_id>/` (no bind-mount of `~/projects/`)
  3. Work inside a fresh git branch
  4. Make changes, run tests, iterate
  5. `git push` + `gh pr create` if green
  6. Write `result.json` with status (`done`/`blocked`) + PR URL + summary
- Dispatch skill's `kind: code` branch routes to this skill instead of the "blocked stub" from §7.1.
- Hard caps: `runTimeoutSeconds` (sub-agent budget), explicit "stop and report blocked" instruction in skill prompt for irrecoverable failures.

**Why this works without swarm:** Max OAuth is already wired in the container (no Codex-auth question). `sessions_spawn` is the runner the architecture already uses. The §8.2 anti-pattern warning ("no `claude` CLI loops") was about *unstructured* loops — a bounded sub-agent with explicit acceptance criteria + time budget *is* the structured pattern.

**Best for:** small features, bug fixes, refactors, doc updates, dependency bumps — tasks finishable in <4 hours with at most one round of replan.

**Not for:** multi-day work, multi-cycle critique loops, anything where deterministic resume across container restarts matters. Those still want swarm.

**Prereqs:**
- `gh` CLI installed + authenticated inside the container (one-time setup; auth persisted via `~/.config/gh` bind-mount or via `GH_TOKEN` env)
- `git` user.name / user.email configured in container
- The target repo must be one the auth principal can push to

**Acceptance test:** one real `kind: code` task end-to-end against a sandbox repo (e.g. add a typo fix + PR), result.json populated, PR URL announced to Telegram.

### 7.3 Phase 6 (swarm wiring — heavier `kind: code` runner)

For the class of work Phase 5.5 can't reach: multi-day, multi-cycle replan, durable-resume-across-restart, formal Planner/Generator/Evaluator critique.

- **BuildEngine adapter** as a TypeScript skill in OpenClaw (it just HTTP-POSTs to `127.0.0.1:2024` — no `claude-agent-sdk` import).
- Dispatch skill grows a second `kind: code` branch: `code-heavy` → swarm; default `code` → Phase 5.5 sub-agent skill. Either the spec carries a `runner: light|heavy` hint or Dispatch infers from `budget.max_runtime_seconds`.
- swarm's `project_builder` graph already exists; nothing changes inside swarm.
- Acceptance test: one real `code-heavy` task end-to-end, PR URL announced to Telegram.

Prereq: the migration from `claude-agent-sdk` to Codex CLI must land in swarm before June 2026 per Anthropic policy — but that's swarm's internal change and doesn't touch this architecture.

### 7.3 Phase 7+ (deferred — only when real friction appears)

- **Heartbeat-driven self-scheduling.** A heartbeat tick checks blocked tasks for ones that became unblocked (e.g. external dependency lifted). Skip until there's a real example.
- **Task chaining / DAGs.** One task spawns another by writing a follow-up `spec.yaml`. The current `~/.life/tasks/` directory makes this trivial when needed — but don't build it preemptively.
- **Per-task model overrides.** Defaults are set by role (see §6.2 model table). Add `model:` to the spec only when a specific task needs a different fit — e.g. "use Opus for this draft because the prose quality matters more than throughput." Not a cost lever; a fit lever.
- **Multi-agent OpenClaw split** (separate agent for ops vs personal). Defer until the single-agent setup actually contends for context.
- **Cross-device PC Kit dispatch.** PC Kit could write specs into `~/.life/tasks/` over the SSHFS mount. Defer until the operator actually wants to file overnight tasks from his desk. Read-only PC Kit is fine for v1.

---

## 8. What this architecture explicitly forbids

These are the rules that prevent rot. If you find code violating one, the code is wrong, not the rule.

1. **No skill writes to `~/.life/domains/`.** Ever. Only `lifekit-curator`. Capture goes to `queue.jsonl`; proposals go to `~/.life/.curator-proposed/<task_id>.patch`.
2. **No code-shaped autonomous work runs through `claude` CLI loops inside VPS Kit.** That's the dev-agent anti-pattern this architecture is built to avoid. Code-shaped work → swarm via BuildEngine adapter. Full stop.
3. **No bespoke queue, no bespoke scheduler, no bespoke locking.** OpenClaw's `queue`, `cron`, `tasks`, and per-session lanes are the primitives. If a need can't be expressed with them, document it as a deferred decision; don't invent a parallel system.
4. **No two skills write the same field.** Spec mutation goes through one `task_update` skill with an explicit mutation list. Other skills compute, then call it.
5. **No new long-lived services.** v1 adds zero new daemons. Everything is OpenClaw skills + the already-existing `swarm-langgraph.service` + the planned `lifekit-curator.service` (already in Phase 3.5).
6. **No cross-vendor sprawl.** Claude Max via `claude-cli` runtime is the only model surface for OpenClaw — not because Max is cheap (cost isn't the issue) but because one auth profile + one provider keeps the system understandable and the failure modes few. swarm uses Claude Max via `claude-agent-sdk` today, Codex via Codex CLI by June 2026 (per Anthropic's autonomous-SDK restriction — not optional). If a future task genuinely needs a non-Claude model (e.g. Gemini for native video understanding), file it as a `deferred — needs tooling decision` proposal; do not add a key impulsively.
7. **No PC → VPS dispatch path that requires a bind-mount of `~/projects/` into the OpenClaw container.** swarm runs on the VPS in its own service; it has its own project workspaces. PC's `~/projects/` is intentionally separate.
8. **No skill imports from OpenClaw internals.** Skills call `sessions_spawn`, `message`, `cron`, `tasks` as documented public tools. If a skill needs OpenClaw internals, file an upstream issue; don't reach in.
9. **No status-polling loops.** OpenClaw is push-based (announce pipeline + heartbeat wake on task completion). If a skill is sleeping in a loop checking task status, it's wrong. Use the announce / heartbeat-wake path.
10. **No task spec exists without `requester_route`.** Without a route, completion can't be announced and the task is invisible. Intake rejects specs without it.
11. **No domain file is read by a sub-agent without going through the standard `read` tool.** No bespoke loaders, no per-skill caches. Cache invalidation is everyone's problem; don't take it on.
12. **No frontmatter field is added to `spec.yaml` without updating §5.2 of this doc first.** The schema is the contract; the doc is the schema.

---

## 9. Alternatives considered (one paragraph each, max two)

**Heavy supervisor / orchestrator agent.** Build a "Kit-Supervisor" sub-agent whose only job is to read `~/.life/tasks/`, decide what runs next, monitor health, and route between sub-agents and swarm. Rejected: this is exactly the rot-vector OpenClaw's [parallel-specialist-lanes](https://docs.openclaw…/concepts/parallel-specialist-lanes) doc warns against — "do not start with a coordinator; a coordinator without lane contracts just coordinates chaos." Our `kind` field + Dispatch skill is the lane contract; cron is the coordinator. No agent needed.

**Use Task Flow instead of bare cron + spec dir.** OpenClaw's Task Flow is a real durable-flow primitive and could host the lifecycle (`ready → dispatched → done`) natively. Rejected for v1 because (a) the `~/.life/tasks/<id>/` directory is the right substrate regardless — it's git-backed, human-readable in `cat`, and survives any OpenClaw migration; (b) Task Flow's value is multi-step orchestration *across* sub-agents, which we don't have yet — every task is one runner. Revisit at Phase 7 if cross-runner orchestration (e.g. "research → draft → review → publish") becomes a recurring shape.

---

## 10. Open questions for VPS Kit to resolve

These are things this doc cannot answer alone and that the VPS Kit review should close out:

1. **HEARTBEAT.md content.** Should heartbeat (Phase 2 of the trust ramp) actively call the Dispatch skill, or should we leave dispatch purely cron-driven? Recommendation: cron only at v1; heartbeat stays a checkup, not a worker.
2. **Sub-agent SOUL inheritance.** OpenClaw sub-agents only inherit `AGENTS.md` + `TOOLS.md`, not `SOUL.md`. The Kit persona may drift in overnight runs. Decision: do we drop persona for autonomous tasks (recommended — work is the work, voice doesn't matter), or extend `AGENTS.md` to carry the minimum persona for completion announces?
3. **swarm's `project_dir` rootedness.** Current code roots at `~/.personal-agent/projects/<slug>/` on the VPS. That's fine, but the Task Spec → `goal.yaml` translation should be explicit about whether the swarm project lives under `~/.life/tasks/<id>/swarm-project/` (nicer for archeology) or stays at `~/.personal-agent/projects/<slug>/` (matches existing swarm assumptions). Recommendation: keep swarm where it is; the spec carries a `swarm_slug` field and the result.json links the project_dir path.
4. **Curator interaction with task output.** If a sub-agent writes `~/.life/.curator-proposed/<task_id>.patch`, does the curator drain it on its normal loop, or does it require a `queue.jsonl` event too? Recommendation: write both — the patch is the artifact, the queue event is the notification — so curator's existing drain logic doesn't have to learn a new input.
5. **`task_id` collisions.** The `YYYY-MM-DD-<slug>-<rand4>` shape is fine for single-VPS volume. If PC Kit ever starts writing specs, add a `created_on: vps|pc` prefix to remove any collision risk.

---

## 11. One-line summary

**Telegram → Intake skill → `~/.life/tasks/<id>/spec.yaml` → Dispatch cron → (sub-agent | swarm) → result.json → announce → morning brief.** No supervisor. No bus. One ledger. One curator. Two runners. Eight forbidden patterns.
