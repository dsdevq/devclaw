# devclaw

> **DevClaw turns a coding goal into a verified PR — autonomously, with no API key.**

DevClaw is a thin orchestration layer in front of [OpenHands](https://github.com/All-Hands-AI/OpenHands). You hand it a goal over MCP (`implement_feature`, `fix_bug`, `review_repository`); it plans the work, runs OpenHands inside a per-task ephemeral Docker sandbox, streams every event, and reports back when the task is done or blocked. Cognition is always `claude` over a Pro/Max OAuth session — **no `ANTHROPIC_API_KEY`, no metered billing** for autonomous overnight runs.

It is **not** a chatbot and **not** a rebuild of OpenHands. OpenHands owns the hard part (the agent loop, tool use, code edits, git). DevClaw owns everything *around* it: the interface, goal decomposition, task state, isolation, and observability.

```
MCP client (OpenClaw / Claude Code / any MCP host)
  │   implement_feature / fix_bug / review_repository / start_program …
  ▼
DevClaw  (TypeScript)
  ├── MCP server     stdio + streamable-HTTP, 9 tools
  ├── planner        Goal → task DAG (single-task passthrough for small goals)
  ├── state store    SQLite — programs, tasks, append-only events
  └── sandcastle     `docker run --rm` per task — RO ~/.claude mount, destroyed on exit
        │
        ▼
  OpenHands (Python SDK)  ── agent loop, runs `claude` via ACP (Pro OAuth)
```

## The split

| Concern | Owner |
|---|---|
| Agent loop, sandbox coding, git | **OpenHands** |
| Goal → tasks decomposition | DevClaw planner |
| Task/program state | DevClaw state store (SQLite) |
| Per-task isolation | DevClaw sandcastle runner (`docker run`) |
| Progress + notification | DevClaw event stream + `notify_url` callbacks |
| Interface to clients | DevClaw MCP server |

The full rationale — including why OpenHands and sandbox isolation are **orthogonal** layers (the agent vs. the box it runs in), and why this calls `docker run` directly instead of depending on `@ai-hero/sandcastle` — lives in [`docs/architecture-v2.md`](./docs/architecture-v2.md).

## Layout

```
src/
├── mcp-server.ts        # stdio + streamable-HTTP MCP server, 9 tools, dashboard + SSE
├── planner.ts           # Goal → task DAG
├── state-store.ts       # SQLite: programs, tasks, append-only events
├── task-queue.ts        # async task lifecycle + concurrency
└── sandcastle-runner.ts # `docker run --rm` per task; streams events from the runner
openhands-runner/
├── runner.py            # OpenHands SDK invocation; emits event/result lines on stdout
└── requirements.txt     # openhands-sdk
.sandcastle/Dockerfile   # per-task sandbox image
test/                    # unit + runtime harnesses (see package.json scripts)
docs/architecture-v2.md  # the architectural contract — read before touching the runner/store/sandbox
```

TypeScript orchestrates; Python touches OpenHands. The language boundary keeps the OpenHands agent loop isolated from the long-running DevClaw process.

## MCP tools

| Tool | Does |
|---|---|
| `implement_feature(repo, goal, …)` | Run a single feature task |
| `fix_bug(repo, description, …)` | Run a single bug-fix task |
| `review_repository(repo, …)` | Read-only review (no writes — invariant runtime-checked) |
| `start_program(goal, …)` | Decompose a large goal into a task DAG and run it |
| `get_program(id)` / `list_programs()` | Program status + the task DAG |
| `get_status(task_id)` | One task's status / result / PR |
| `list_tasks(...)` | Task history, filterable |
| `get_events(...)` | Replayable event feed (also a live SSE stream over HTTP) |

Async by default: a tool call returns a `task_id` immediately and the work runs in the background. Pass a `notify_url` to get a callback on completion/block instead of polling.

## Auth (the design constraint)

DevClaw inherits a Claude Code OAuth session — it never uses an API key. `ANTHROPIC_API_KEY` is **actively refused** at both the TypeScript and Python layers so a stray key can't silently switch autonomous runs onto metered billing.

```bash
export CLAUDE_CODE_EXECUTABLE=/path/to/claude   # the CLI to drive
export CLAUDE_CONFIG_DIR=$HOME/.claude          # the OAuth session to reuse
```

Inside the per-task sandbox, `~/.claude` is bind-mounted **read-only** (auth works); nothing else from the host is mounted (the container is destroyed on exit, so no persistent state escapes).

## Run it

```bash
npm install
npm run openhands:install     # creates openhands-runner/.venv with openhands-sdk
npm install -g @agentclientprotocol/claude-agent-acp   # ACP adapter (one-time, global)

npm run build
DEVCLAW_TRANSPORT=stdio npm start        # local dev (MCP over stdio)
# or HTTP for a long-running service:
DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000 npm start
#   → MCP at /mcp, live dashboard at /dashboard, SSE at /programs/:id/events
```

### Useful env

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_TRANSPORT` | `stdio` | `stdio` or `http` |
| `DEVCLAW_PORT` | `8000` | HTTP port |
| `DEVCLAW_DB` | (temp) | SQLite path for state |
| `DEVCLAW_SANDBOX_IMAGE` | — | per-task sandbox image (see `.sandcastle/Dockerfile`) |
| `CLAUDE_CODE_EXECUTABLE` / `CLAUDE_CONFIG_DIR` | — | OAuth-session passthrough |

## Tests

```bash
npm run test:unit     # planner + state store — no Docker needed
npm run test:dag      # local DAG stub harness
```

## Status

v2 is the live runtime; the original v1 (a LangGraph orchestrator + markdown skills driven by cron) has been retired and removed — it survives in git history if you need the prior art. Slices 1–5 are shipped on `main`: MCP server → SQLite state → planner DAG → sandcastle isolation → event stream + dashboard.

## What this is NOT

- **Not a chatbot.** It's a backend service other agents call.
- **Not a general assistant.** It executes software-development goals, nothing else.
- **Not a rebuild of OpenHands.** OpenHands is the execution engine; DevClaw is the orchestration above it.

## License

[MIT](./LICENSE). Copyright 2026 Denys Sychov.
