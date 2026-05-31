# devclaw

> **DevClaw turns a coding goal into a verified PR — autonomously, with no API key.**

DevClaw is a thin orchestration layer in front of [OpenHands](https://github.com/All-Hands-AI/OpenHands). You hand it a goal over MCP (`implement_feature`, `fix_bug`, `review_repository`); it plans the work, runs OpenHands inside a per-task ephemeral Docker sandbox, streams every event, and reports back when the task is done or blocked. Cognition is always `claude` over a Pro/Max OAuth session — **no `ANTHROPIC_API_KEY`, no metered billing** for autonomous overnight runs.

It is **not** a chatbot and **not** a rebuild of OpenHands. OpenHands owns the hard part (the agent loop, tool use, code edits, git). DevClaw owns everything *around* it: the interface, goal decomposition, task state, isolation, and observability.

```
MCP client (OpenClaw / Claude Code / any MCP host)
  │   implement_feature / fix_bug / review_repository / start_program …
  ▼
DevClaw  (Python)
  ├── MCP server     FastMCP — stdio + streamable-HTTP, 9 tools, dashboard + SSE
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
devclaw/
├── server.py            # FastMCP server — 9 tools, dashboard + SSE, bearer-auth middleware
├── planner.py           # Goal → task DAG (shells out to `claude --print`)
├── state_store.py       # SQLite: programs, tasks, append-only events
├── task_queue.py        # async task lifecycle (asyncio) + concurrency
└── sandcastle_runner.py # `docker run --rm` per task; streams events from the runner
openhands-runner/
├── runner.py            # OpenHands SDK invocation; emits event/result lines on stdout
└── requirements.txt     # openhands-sdk
.sandcastle/Dockerfile   # per-task sandbox image
tests/                   # pytest — planner, state store, queue/DAG (stubbed; no docker)
docs/architecture-v2.md  # the architectural contract — read before touching the runner/store/sandbox
```

DevClaw is all Python. The only language boundary left is the process boundary: `openhands-runner/runner.py` runs the (Python-only) OpenHands SDK *inside* the sandbox container, isolated from the long-running host process — it talks to the host over a line-delimited JSON protocol on stdout.

## MCP tools

| Tool | Does |
|---|---|
| `implement_feature(workspace_dir, goal, …)` | Run a single feature task |
| `fix_bug(workspace_dir, description, …)` | Run a single bug-fix task |
| `review_repository(workspace_dir, …)` | Read-only review (no writes — invariant runtime-checked) |
| `start_program(workspace_dir, goal, …)` | Decompose a large goal into a task DAG and run it |
| `get_program(program_id)` / `list_programs()` | Program status + the task DAG |
| `get_status(task_id)` | One task's status / result / PR |
| `list_tasks(...)` | Task history, filterable |
| `get_events(...)` | Replayable event feed (also a live SSE stream over HTTP) |

Async by default: a tool call returns a `task_id` immediately and the work runs in the background. Pass a `notify_url` to get a callback on completion/block instead of polling.

## Auth (the design constraint)

DevClaw inherits a `claude` OAuth session — it never uses an API key. `ANTHROPIC_API_KEY` is **actively refused** at both the host (planner) and sandbox layers so a stray key can't silently switch autonomous runs onto metered billing. All you need is a logged-in `claude` CLI: the planner shells out to it, and the per-task sandbox bind-mounts `~/.claude` **read-only** (auth works; nothing else from the host is mounted, and the container is destroyed on exit so no state escapes).

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .                       # the host orchestrator (FastMCP + httpx)
pip install -r openhands-runner/requirements.txt   # only needed inside the sandbox image
npm install -g @agentclientprotocol/claude-agent-acp   # ACP adapter (one-time, global)

DEVCLAW_TRANSPORT=stdio devclaw-mcp        # local dev (MCP over stdio)
# or HTTP for a long-running service:
DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000 devclaw-mcp
#   → MCP at /mcp, live dashboard at /dashboard, SSE at /programs/:id/events
```

(`devclaw-mcp` is the console script; `python -m devclaw.server` works too.)

### Useful env

| Var | Default | Purpose |
|---|---|---|
| `DEVCLAW_TRANSPORT` | `stdio` | `stdio` or `http` |
| `DEVCLAW_PORT` | `8000` | HTTP port |
| `DEVCLAW_HOST` | `0.0.0.0` | HTTP bind address (set `127.0.0.1` to restrict to loopback) |
| `DEVCLAW_TOKEN` | — | When set, the HTTP transport requires it on every route except `/health` — via `Authorization: Bearer <token>` or a `?token=` query param. Unset = no auth (local dev). |
| `DEVCLAW_DB` | `./devclaw.db` | SQLite path for state |
| `DEVCLAW_MAX_CONCURRENT` | `4` | global cap on concurrently-running tasks (backpressure) |
| `DEVCLAW_MAX_CONCURRENT_PER_PROGRAM` | `2` | per-program concurrency cap |
| `DEVCLAW_TICK_SECONDS` | `10` | heartbeat interval — advances DAGs and resumes recovered work from DB state |
| `DEVCLAW_SANDBOX_IMAGE` | `devclaw-sandbox:latest` | per-task sandbox image (see `.sandcastle/Dockerfile`) |
| `DEVCLAW_CLAUDE_BIN` | `claude` | the `claude` binary the planner drives |
| `DEVCLAW_HOST_CLAUDE_DIR` | `~/.claude` | host path bind-mounted read-only into each sandbox |

## Tests

```bash
pip install -e ".[dev]"
pytest          # planner + state store + queue/DAG, all stubbed — no docker, no claude
```

## Status

DevClaw is the live runtime. It was rewritten from TypeScript to all-Python (FastMCP) — the host orchestration now matches the language of the OpenHands SDK it drives, so there's a single toolchain. The original v1 (a LangGraph orchestrator + markdown skills driven by cron) was retired earlier and lives only in git history.

## What this is NOT

- **Not a chatbot.** It's a backend service other agents call.
- **Not a general assistant.** It executes software-development goals, nothing else.
- **Not a rebuild of OpenHands.** OpenHands is the execution engine; DevClaw is the orchestration above it.

## License

[MIT](./LICENSE). Copyright 2026 Denys Sychov.
