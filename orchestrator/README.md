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
