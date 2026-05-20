# Architecture: orchestrator port to LangGraph

**Status:** in-progress (v0.0.1 slice shipping 2026-05-18).
**Decision-driven by:** `~/.life/system/proposals.md#2026-05-18-workflow-engine-port-decision`.

## Why port

The devclaw v0.1 architecture executes every skill — including pure-mechanism work like task dispatching, watchdogging deadlines, and reconciling state — as an **LLM agent following a markdown contract**. Each cron tick spawns a Claude Code subagent that reads `task_dispatch/SKILL.md` and uses Bash + Edit tools to do yaml string-replacement.

This is a category error:

- **Cost:** every dispatch tick burns ~5K-20K Claude tokens to do work Python would do in 200ms.
- **Reliability:** the runner can mis-edit yaml, forget to flip a status field, or hallucinate. This is the source of the Phase 5.7c gaps #1-#3 and the ghosted-runner gap (closed by PR #3).
- **Latency:** dispatch loop runs in 30s LLM-roundtrip instead of 200ms Python.
- **Observability:** stack traces are vibes, not bytes.

The orchestrator port separates two layers and routes each to the right tool.

## The mechanism / cognition split

| Layer | Implementation | What lives here |
|---|---|---|
| **Mechanism** | Pure Python, runs inside LangGraph nodes, zero LLM calls | `task_dispatch`, reap pass, watchdog pass, `task_update`, `project_curator` state machine, `verify-task` command runner, retry policy, escalation routing |
| **Cognition** | LangGraph nodes that shell to `claude --print` or `codex exec --json` via subprocess | `task_intake` (NL→spec), `code-task`, `research-task`, `propose_change`, fuzzy-AC verification |

The current markdown skills under `../skills/` remain authoritative until each one is ported; the two coexist during migration.

## Hard constraints

These are decisions, not defaults. Every PR that changes orchestrator code must respect them.

### No API keys

- ❌ No `ANTHROPIC_API_KEY`, no `OPENAI_API_KEY`, no `langchain_anthropic.ChatAnthropic`, no `langchain_openai.ChatOpenAI`, no Anthropic / OpenAI Python SDK imports.
- ✅ All LLM calls go through the user's CLI OAuth session: `claude --print ...` or `codex exec --json ...` as subprocesses from LangGraph nodes.
- **Why:** the design intent is "every overnight run is $0 marginal" — the user's Pro/Max subscription covers CLI-bound autonomous work. See `~/.life/system/proposals.md#2026-05-13-buildengine-migration-anthropic-to-codex` for the Codex CLI verification.

### Durable across process restarts

- LangGraph checkpointer required for every compiled graph. Default: SQLite. Production: Postgres (the VPS already has it).
- Each Run gets a `thread_id` so resumption is keyed on Run identity, not on what host the worker happens to be on.

### Idempotent side effects

- All side-effecting operations (git ops, PR opens, Telegram sends, spec.yaml writes) wrapped in LangGraph `@task` so resume-after-pause doesn't double-execute.
- This is the single new discipline the port introduces. The first reviewer who finds a node opening a PR twice on resume blocks merge.

### Same failure-mode contract

- Curator's internally-resolvable list (now including `runner_silent_past_deadline`) ports verbatim to the verify-route conditional edge.
- §6.3 escalation list ports verbatim to the escalate node. The contract is "Curator pings the operator only on these 6 cases" — the port doesn't change that contract.

## v0.0.1 slice — what ships first

```
START
  ↓
[code_task]              cognition: claude --print → captures Result
  ↓
[verify]                 mechanism: runs each acceptance-criterion bash command
  ↓
{route_after_verify}     mechanism: pure routing function
  ├── complete  →  END   (status=done, mark spec done)
  ├── retry     →  [code_task]  (one-shot — guarded by retry_count)
  └── escalate  →  END   (status=blocked, error set for caller to announce)
```

Out of scope for v0.0.1:

- `task_intake` (LLM step) — for now, accept structured specs directly.
- Multi-task DAG (`project_curator` over a Run) — separate supervisor graph composed on top once this slice is solid.
- Telegram I/O — still goes through OpenClaw; the orchestrator emits structured Results and the caller decides what to ferry to the user.
- `research-task` / `propose_change` — same shape as `code_task` (subprocess to `claude --print`), ported once `code_task` is proven.

## Module layout

```
orchestrator/
├── pyproject.toml
├── README.md
├── src/orchestrator/
│   ├── __init__.py
│   ├── cli.py                  # `devclaw-orchestrator dispatch <spec.yaml>`
│   ├── graph.py                # LangGraph wiring + compile
│   ├── dispatch.py             # deterministic core (dispatch + reap + watchdog)
│   ├── state/
│   │   ├── __init__.py
│   │   └── models.py           # Pydantic: TaskSpec, DagNode, Run, Result, GraphState
│   ├── nodes/                  # deterministic LangGraph nodes
│   │   ├── __init__.py
│   │   └── verify.py           # verify_node, route_after_verify, complete, escalate, retry
│   └── runners/                # cognition-layer LangGraph nodes (CLI subprocess wrappers)
│       ├── __init__.py
│       └── code_task.py        # code_task_node — invokes `claude --print`
└── tests/
    ├── test_dispatch.py        # pure-function tests of dispatch/reap/watchdog
    └── test_graph.py           # end-to-end graph wiring tests using stub runner
```

## Two gotchas to internalize

### 1. Nodes resume from their start, not from the `interrupt()` line

LangGraph re-runs node code from the top on resume. Any side effect placed before an `interrupt()` call **runs again**. The fix is `@task` — its results are checkpointed and don't re-execute. We don't use `interrupt()` in the v0.0.1 slice, but Phase 5.7c human-approval gates will, so this discipline kicks in then.

### 2. Mid-step subprocess crashes are NOT magically recoverable

If the LangGraph worker dies while `claude --print` is running, the next resume starts the subprocess from scratch. LangGraph checkpoints **between** `@task` calls, not within them. To get mid-subprocess durability you'd need the subprocess itself to checkpoint to a known path. Our `code_task` runner mitigates this by having `claude --print` produce a `result.json`; future re-invocations could check for it and short-circuit (not implemented v0.0.1).

## What this does NOT solve

Honest list of things the orchestrator still doesn't handle:

1. **Scheduling/cron** — OpenClaw cron (or a separate scheduler) still triggers the orchestrator. LangGraph isn't a scheduler.
2. **Telegram round-trip** — OpenClaw still owns Telegram I/O. The orchestrator emits structured Results; the caller decides what to send.
3. **Sub-second performance at high concurrency** — neither the old system nor the new one has been load-tested. Not a problem at one-user scale.
4. **Multi-machine durability** — the SQLite checkpointer is single-machine; Postgres would handle this, but moving from one VPS to two is out of scope.

## Alternatives considered (and rejected)

See `~/.life/system/proposals.md#2026-05-18-workflow-engine-port-decision` for the full comparison. Short version: Temporal/Inngest/Trigger.dev/Restate all treat LLM calls as opaque steps and aren't LLM-flow-aware. LangGraph models the agent loop natively, is already in the stack via swarm, and is on-brand for the Anthropic-Dublin career path.

## Migration path (per skill)

Each markdown skill ports independently:

1. **Write the Python equivalent** in `orchestrator/` (deterministic Python OR a LangGraph node that shells out to `claude --print`).
2. **Add unit tests** that don't require Claude (use the deterministic stub).
3. **Add an integration test** that runs the real Claude CLI on a throwaway spec.
4. **Cut over** by swapping the OpenClaw cron from invoking the markdown skill to invoking `devclaw-orchestrator <subcommand>`.
5. **Mark the markdown skill deprecated** (frontmatter `deprecated: true`); keep it around until the port has baked for at least 2 weeks of dogfood.
6. **Delete the markdown skill** once dogfood passes.

## Status snapshot (2026-05-18, end of day-1 port)

| Markdown skill | Python equivalent | Status |
|---|---|---|
| `task_intake` | `orchestrator/intake.py` + `devclaw-orchestrator intake` | ✅ ported (incl. parallel-frontend-conflict guard — forces serial dispatch when two in-flight code specs target the same repo and both mention shared SPA-root markers; bypass with `parallel_safe: true`) |
| `task_dispatch` (dispatch + reap + watchdog) | `orchestrator/dispatch.py` + `orchestrator/sweep.py` + `devclaw-orchestrator sweep` | ✅ ported |
| `code-task` | `orchestrator/runners/code_task.py` | ✅ ported (live-smoke-validated) |
| `research-task` | `orchestrator/runners/research_task.py` | ✅ ported |
| `propose_change` | `orchestrator/runners/propose_change.py` | ✅ ported |
| `verify-task` | `orchestrator/nodes/verify.py` | ✅ ported (deterministic AC runner; cognitive-AC variant deferred) |
| `project_curator` | `orchestrator/supervisor.py` + `devclaw-orchestrator supervise[-all]` | ✅ ported |
| `task_update` | `orchestrator/dispatch.py::persist_spec` helper | ✅ ported |
| `define_run` | (not yet — atomic-creation today goes through `intake`; multi-task DAGs come from `propose_change` approval workflow) | 🟡 deferred |
| `project_init` | (not yet — initial project bootstrap is rare; stays markdown until needed) | 🟡 deferred |

**Cutover** (replacing the markdown crons): not yet. The Python and markdown systems coexist via different intake paths — markdown reads `~/.life/tasks/*/spec.yaml` produced by the markdown `task_intake`; Python reads specs produced by the Python `intake` subcommand. Cutting over the cron entries is a separate VPS-deploy PR.

## Architectural rules (the things that hold across the port)

1. **No API keys.** Every cognition node shells to `claude --print` or (future) `codex exec --json`. Subscription OAuth only.
2. **Pure-Python orchestration owns all yaml writes.** Cognition nodes return state; deterministic nodes persist. Never write yaml from inside an LLM call.
3. **Reap > Watchdog > Dispatch order**, every cron tick. A late-but-complete runner keeps credit; ghosts are killed; new ready specs fire last.
4. **One retry per node, ever**, on the internally-resolvable blocker set. Second failure escalates via §6.3.
5. **Killswitch wins.** `~/.life/system/cron-paused` short-circuits every cron-fired entry point: sweep, supervise, supervise-all. In-flight subprocesses keep running; only NEW work is blocked.
6. **Single-writer per file.** `dag.yaml` written only by the supervisor; per-task `spec.yaml` written by intake + dispatch (atomic) or supervisor (run-bound) + sweep (reap/watchdog) + dispatch-CLI (terminal). No two writers race because each phase is gated by status.

## Task-lifecycle Telegram announces

Operators with a phone but no terminal need to see what the daemon is doing without tailing logs. Five state-transition events fire through the `orchestrator.events` module:

| # | Transition                               | Fired from                              | Message format                                             |
|---|------------------------------------------|-----------------------------------------|------------------------------------------------------------|
| 1 | `task_intake` → `spec_created`           | `intake.intake_from_prose` (state="new")| `📋 Queued: <task_id> → <target_repo or '(project-less)'>` |
| 2 | `task_dispatch` → `dispatched-*`         | `sweep.sweep_once`, `supervisor.tick_run`| `🚀 Dispatched: <task_id> (<runner_kind>)`                  |
| 3 | `task_runner` → `done` WITH `pr_url`     | `cli.cmd_dispatch`                      | `✅ Done: <task_id>\n<pr_url>`                              |
| 4 | `task_runner` → `done` WITHOUT `pr_url`  | `cli.cmd_dispatch`                      | `✅ Done: <task_id>`                                        |
| 5 | `task_runner` → `failed`/`abandoned`     | `cli.cmd_dispatch` (terminal-blocked)   | `❌ <new_state>: <task_id>\n<reason or 'no reason captured'>`|

Each emitted message is capped at 300 chars (truncated with `…`). Each transition fires exactly once because the orchestrator is the single writer of each state and only one site flips each transition. The duplicate-intake path (`state="duplicate"`) intentionally **does not** re-announce.

The emitters reuse PR #21's `AnnounceCallback = Callable[[str, str, str], None]` shape — there is no separate transport. `DaemonConfig.events_announce` is **additive** to `DaemonConfig.announce` (which remains dedicated to the audit-loop). `cli.py daemon` wires both to `_openclaw_announce`, which shells out to `openclaw message send` (`check=False, timeout=15`); subprocess failures log at WARN and never raise.

### Chat-id resolution

The lifecycle events resolve their Telegram chat id through `events.resolve_events_chat()`:

1. **`LIFEKIT_TELEGRAM_EVENTS_CHAT`** — lifecycle-specific override (set this in the docker-compose env to send events to a side channel).
2. **`LIFEKIT_TELEGRAM_CHAT`** — shared fallback (re-used from the existing audit/escalate wiring).
3. **`default`** — caller-supplied last resort.

Out of scope for this port slice: wiring `LIFEKIT_TELEGRAM_EVENTS_CHAT` into `lifekit-stack`'s docker-compose env — a sibling follow-up.
