# devclaw-orchestrator

LangGraph-based runtime orchestrator for devclaw. Replaces the markdown-skill execution model with **deterministic Python orchestration + Claude/Codex CLI subprocess for cognition**.

## The mechanism / cognition split

The current devclaw v0.1 architecture executes every skill — including pure-mechanism work like dispatching tasks, watchdogging deadlines, reconciling state — as an LLM agent following a markdown contract. That's a category error: LLMs are non-deterministic, slow, and (under metered billing) expensive; yaml string-replacement is not cognition.

This package separates the two layers:

| Layer | Implementation | Examples |
|---|---|---|
| **Mechanism** | Pure Python, runs inside LangGraph nodes, zero LLM calls | dispatch, reap, watchdog, state transitions, command-runner verification, retry policy, escalation routing |
| **Cognition** | LangGraph nodes that shell out to `claude --print` or `codex exec --json` via subprocess | intake (NL → spec), code-task, research synthesis, RFC drafting, fuzzy-AC verification |

## Hard constraints

- **No API keys.** Ever. No `ANTHROPIC_API_KEY`, no `OPENAI_API_KEY`, no `ChatAnthropic` / `ChatOpenAI`. All LLM calls happen via the user's Claude / Codex CLI OAuth session, invoked as subprocesses from inside LangGraph nodes.
- **Durable across restarts.** Every step's state is checkpointed to SQLite (dev) or Postgres (prod). VPS reboot mid-Run → worker resumes from last checkpoint.
- **Idempotent side effects.** All side-effecting work (git operations, PR opens, Telegram sends) is wrapped in LangGraph `@task` so resume-after-pause doesn't double-execute.
- **No new failure modes silently introduced.** Every retry-once and escalation-list rule from the current curator architecture maps onto LangGraph's `RetryPolicy` / `TimeoutPolicy` / conditional edges; the §6.3 contract holds.

## Status

**v0.0.1 — proof-of-port.** First slice: deterministic `task_dispatch` (dispatch + reap + watchdog) in Python, plus one cognition node (`code_task` via `claude --print`), wired into a LangGraph graph with a SQLite checkpointer. End-to-end smoke test produces a real PR.

Markdown skills under `../skills/` remain authoritative until each one is ported. Migration is per-skill; the two coexist until the port is complete.

## Architecture

See `../docs/architecture-orchestrator-port.md` for the full design rationale (mechanism/cognition split, LangGraph port reasoning, gotchas to design around, comparison to alternatives considered).

## PR labeling convention

Every PR opened by the runner gets the `devclaw` label, attached at PR-creation time. The label makes runner-opened PRs trivially filterable in the GitHub UI and on the CLI (`gh pr list --label devclaw`), and visually distinguishes them from human commits, fleet siblings, and other automation.

- The label is ensured idempotently on the target repo before each PR is opened (`gh label create devclaw --force`), so adding a new `target_repo` requires no manual repo setup.
- Color `#1f6feb` (devclaw blue); description references the `kit/<task_id>-*` branch convention and the spec path.
- A one-shot backfill script for existing PRs lives at `../scripts/backfill_devclaw_label.sh`.

## Concurrency cap (`max_concurrent_claudes`)

The sweep dispatcher enforces a global ceiling on the number of in-flight `claude --print` subprocesses across all task kinds (`code`, `research`, `propose_change`, `intake` — every kind spawns claude). When the cap is reached, the sweep's dispatch pass **leaves** the next-eligible `ready` TaskSpec on disk (no status flip, no `blocked`) and re-evaluates it on the next sweep tick once an in-flight runner finishes.

| Surface | Where it lives | Default |
|---|---|---|
| `DaemonConfig.max_concurrent_claudes` | `orchestrator.daemon` | `1` |
| `sweep_once(..., max_concurrent_claudes=N)` | `orchestrator.sweep` | `1` |
| `devclaw-orchestrator sweep --max-concurrent-claudes N` | CLI | `1` |
| `devclaw-orchestrator daemon --max-concurrent-claudes N` | CLI | `1` |

### Why default 1

The VPS has ~3.7 GiB physical RAM + 2 GiB swap. A single `claude --print` with the 1M-context flag peaks at ~1–1.5 GiB resident; two or three in parallel exceed host capacity even with swap, and the orchestrator container's memory cap (bumped to 2 GiB on 2026-05-21) is sized for one. The default is set to `1` so a future config slip cannot bring back the freeze risk.

Raising this above `1` requires the container to have headroom for `N × ~1.5 GiB` peak. It is *not* a global lock across containers — each orchestrator instance enforces its own cap.

In-flight is counted from spec state on disk (`status: dispatched-subagent` or `dispatched-build`), so the cap survives orchestrator restarts: after a restart, the sweep counts pre-existing dispatched-* specs against the cap until they reap or watchdog out.

Note that `cmd_dispatch` (the per-spec CLI runner that the sweep `Popen`s) does not independently re-check the cap — it is a worker that executes whatever the sweep has already gated. Direct manual invocations of `devclaw-orchestrator dispatch <spec>` bypass the cap by design, since they are operator-driven one-offs.
