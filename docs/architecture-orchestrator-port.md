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

Priority order: `task_dispatch` first (already done in v0.0.1 spirit via the new Python `dispatch.py`); then `code_task`; then `verify-task`; then `project_curator`; then the runner kinds (research/draft/chore); then `task_intake` and `propose_change` last (the most LLM-shaped, port last).
