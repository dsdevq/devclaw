# DevClaw — Build a Project From Scratch (design)

**Status:** Design. Extends [`architecture-v2.md`](./architecture-v2.md) — does not replace it.

---

## One sentence

You say *"build me X."* DevClaw **grills you** until you've both signed a written understanding of *what* to build and *how*; you approve; then it **executes for as long as it takes** — decomposing the spec and driving **OpenHands** in per-task sandboxes — reporting progress and accepting steering along the way.

---

## Why this shape (it's already the agreed direction)

This isn't new invention. It joins two things the project already committed to:

- **The goal layer.** `PLAN.md`'s north star: DevClaw today is a one-shot *task runner*; the missing piece is the *goal* layer — a durable objective an agent drives over time, with **ephemeral body / durable mind** (the process dies between wakeups; state lives on disk). A days-long project build is the canonical instance.
- **The Socratic front-end v1 had and v2 dropped.** The v1 curator was *recon → Socratic → RFC → DAG → verify*. The v2 rewrite kept the DAG executor (`start_program`) but threw away the **Socratic + RFC** front-end — v2 plans a bare goal string blindly. This restores that elicitation phase **on top of the v2 OpenHands engine**.

**Orchestration ⊥ engine (the decoupling).** The planning/goal layer is a separate thing from the execution engine. OpenHands is **pinned as the engine** and plugs in behind a small seam — the planning layer never imports it. "Decoupled" means *orchestration is independent of the engine*, not *remove OpenHands*.

---

## The pipeline — five decoupled phases

```
build_project(idea)
  1. ELICIT      grill the user → converge on a spec        [human-in-loop]
  2. APPROVE     written shared understanding, gated         [human gate]
  3. PLAN        spec → task DAG                              [engine-agnostic]
  4. EXECUTE     each task → OpenHands in a sandbox → PRs     [autonomous, days]
  5. STEER       progress pings + a steering inbox           [human-in-loop, async]
```

Every phase has its own **persisted state** and a clean boundary. The system can crash or restart between (or inside) any phase and resume — that's the durable-mind requirement, and it's what makes "days" safe.

---

## Phase 1 — Elicitation (the "grill")

**Behavior** — reuse [Matt Pocock's `grill-me`](https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md) (MIT) as the elicitation engine's system prompt, credited. Its load-bearing rules become ours:

- Interview **relentlessly** until shared understanding; walk each branch of the design tree, resolving dependencies one-by-one.
- **One question at a time.**
- For each question, **provide a recommended answer** (so the user can often just say "yes" — aligns with the project's *predict-don't-ask* rule).
- If a question is answerable without the user (best-practice default, an obvious convention), **decide it instead of asking.** (Greenfield has no codebase to explore, but the principle still trims the interview.)

**Mechanism vs cognition** (the project's core split). Python owns the loop, the transcript, and the evolving spec; `claude` owns *"what is the single most valuable next question, and what do I recommend?"* No reasoning leaks into Python; no bookkeeping leaks into the prompt.

**Transport.** MCP **elicitation** — FastMCP's `ctx.elicit()` lets a tool call pause, ask the user a question, get the answer, and ask the next — all inside one `build_project` flow. A grilling session is usually one sitting (minutes), so a live `ctx.elicit` loop is the primary path. Because the transcript + draft spec are persisted after every turn, an interrupted grilling resumes via `continue_elicitation(project_id)` — no progress lost if the client disconnects.

**Convergence.** The loop ends when the open-questions set is empty **or** the user says "enough, build it." Output: a **`spec.md`** — the shared understanding.

---

## Phase 2 — Spec + approval

`spec.md` is the contract (the RFC equivalent of v1's `propose_change`). Structured:

```
# <project> — spec
## Goal            — one paragraph, what success is
## Scope           — in / out (explicit out-of-scope list)
## Stack & arch    — decisions made during the grill, with the "why"
## Milestones      — coarse phases the build moves through
## Acceptance      — checkable criteria per milestone
## Constraints     — perf, deps, hosting, non-negotiables
## Open risks      — known unknowns carried into execution
```

**Approval is an explicit gate.** No task runs before `approve_spec(project_id)`. Matches the project's standing *"Denys is the gate; no auto-adopt"* rule. Approval is recorded (who/when) so the executor can prove it had a mandate.

---

## Phase 3 — Plan (spec → DAG)

The existing planner (`planner.py`, today behind `start_program`) generalizes: instead of planning a bare goal string, it decomposes the **approved spec** into a task DAG — milestones → tasks with dependencies. This is the moment "build a project from scratch" becomes legible: the spec fans out into concrete OpenHands tasks.

The planner stays **engine-agnostic** — it emits tasks (`kind`, `goal`, `depends_on`); it does not know what runs them.

---

## Phase 4 — Execute (the days-long grind)

The durable executor. Reuses today's `task_queue` / `state_store` / `sandcastle_runner`, with the additions that make multi-day runs safe:

- **Each task → OpenHands in a per-task docker sandbox → PR.** Unchanged from today.
- **Crash recovery (the gap that blocks "days").** Today the in-process queue holds `_running_by_program` in memory; a restart orphans tasks marked `running` in SQLite forever. Fix: on startup, **reap** orphaned `running` tasks (re-queue or mark for retry) so a restart mid-build is harmless.
- **Heartbeat-driven advancement (ephemeral body / durable mind).** Instead of relying only on the in-process loop, a periodic **tick** re-derives "what's ready / settled / stuck" purely from DB state and advances the DAG. The process can die between ticks; the build resumes from disk. This is what turns a fragile long-running process into a crash-safe multi-day goal.
- **Cheap-idle-tick guardrail (quota — load-bearing).** Each tick first does a **deterministic** DB check ("any ready tasks? any newly settled?") and only spends the engine/LLM if there's real work. (The Pro quota was burned once by ticking unconditionally.)
- **Concurrency + backpressure.** A global cap and a per-program cap bound the number of live containers; ready tasks queue rather than fan out unbounded.

---

## Phase 5 — Steer + report

- **Report.** `notify_url` callbacks (exist) + the live event stream / `/dashboard` (exist) surface progress.
- **Steer.** A `steer(project_id, message)` tool writes to a per-project **inbox**; the executor reads it at each tick and folds direction into the next planning decision — redirect mid-build without stopping it. (North star: *"inject direction between wakeups."*)
- **Blockers.** When a task blocks on a human decision, the goal **parks** and surfaces (a Telegram card via Kit); it resumes when you answer. No silent stalls.

---

## State on disk

Code lives in the repo; **runtime state never does** (public-repo-safe). Per project:

```
$DEVCLAW_STATE/projects/<project_id>/
  idea.md            # the original ask
  transcript.jsonl   # the grilling Q&A, append-only
  spec.md            # the shared understanding (the contract)
  approval.yaml      # who / when approved
  steer/inbox.jsonl  # direction updates, append-only
  log.jsonl          # what happened
```

…plus the program / task / event rows already in `state_store` (SQLite at `$DEVCLAW_DB`). The DB is the source of truth for execution; the files are the human-readable contract + audit trail.

---

## The decoupling, concretely (what you'll see)

```
ElicitationEngine  ── grills → spec.md            ── knows nothing about OpenHands
Planner            ── spec.md → task DAG          ── knows nothing about OpenHands
Executor           ── drives an Engine interface  ── OpenHands is ONE implementation
        │
        └── Engine seam:  run(kind, workspace, goal, on_event) → result
                          (today's sandcastle_runner already IS this shape)
```

OpenHands is pinned as the engine, but behind a one-method seam. The planning/goal layer never imports it — so the engine is swappable and the orchestration is testable with a stub (the tests already do this).

---

## New MCP surface

| Tool | Phase | Does |
|---|---|---|
| `build_project(idea, hints?)` | 1 | Start a project; open the elicitation grill; returns `project_id` |
| `continue_elicitation(project_id)` | 1 | Resume an interrupted grill |
| `get_spec(project_id)` | 2 | The current `spec.md` |
| `approve_spec(project_id)` | 2 | The gate → kicks off plan + execute |
| `get_project(project_id)` | 3–5 | Status across all phases (spec, plan, task DAG, progress) |
| `steer(project_id, message)` | 5 | Inject direction into a running build |

The execution-phase tools already exist (`get_program`, `get_events`, `list_tasks`, the dashboard) — a project's plan **is** a program under the hood.

---

## Build order (each step independently shippable + testable)

1. **Engine seam** — formalize the `Engine` interface around `sandcastle_runner` (tiny refactor, no behavior change). Makes the decoupling explicit and keeps tests stub-driven.
2. **Durability + recovery** — startup reaper for orphaned `running` tasks; heartbeat-tick executor that advances the DAG from DB state. Closes the multi-day gap. *(This is also the "scalability" hardening — it's the same work.)*
3. **Plan-from-spec** — generalize the planner to consume an approved `spec.md`.
4. **Elicitation** — `build_project` + the `ctx.elicit` grill loop (grill-me prompt) + `spec.md` + `approve_spec` gate.
5. **Steer inbox** — `steer()` + tick integration.

Each step is a normal PR with its own tests. Steps 1–2 are pure hardening of what exists; 3–5 add the new front-end.

---

## Reused / credited

- **`grill-me`** by Matt Pocock — [github.com/mattpocock/skills](https://github.com/mattpocock/skills) (MIT). Its interview methodology is embedded as the elicitation system prompt. Not a code dependency — a prompt we adapt, with attribution.

## Open questions (to resolve before/while building)

1. **Elicitation across multiple sittings** — is one live `ctx.elicit` session enough, or do we need the grill to span days too (resume via `continue_elicitation`)? Default: live session primary, resume as fallback; revisit if grills routinely span sessions.
2. **Where the grill cognition runs** — `claude --print` subprocess (consistent with the planner) vs. MCP *sampling* (ask the client's model). Subprocess keeps the no-API-key posture and the mechanism/cognition split clean; lean that way.
3. **Spec mutation mid-build** — when `steer()` changes scope, does it edit `spec.md` and re-plan the affected sub-DAG, or only append? Start append-only + targeted re-plan; full re-spec is a bigger lever.
4. **One repo or many** — does "build a project" target a fresh repo (scaffold + push) or an existing workspace? v1: fresh workspace dir; repo creation is a milestone task.
