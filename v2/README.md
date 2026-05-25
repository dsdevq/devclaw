# DevClaw v2 — first slice

> **Status:** slice 1 of N. Stdio MCP server with one tool (`implement_feature`) that hands a natural-language goal to OpenHands via the Python SDK and waits for completion.

This directory is the v2 implementation greenfield. v1 (the markdown skills + LangGraph orchestrator) continues to live at the repo root in `skills/` and `orchestrator/`. v2 retires v1 once it's proven end-to-end and DevClaw v2 has baked.

The architectural rationale is documented at [`../docs/architecture-v2.md`](../docs/architecture-v2.md).

## Shape

```
v2/
├── src/
│   ├── mcp-server.ts          # stdio MCP server, exposes one tool
│   └── openhands-runner.ts    # spawns the Python runner subprocess
├── python-runner/
│   ├── runner.py              # OpenHands ACPAgent invocation
│   └── requirements.txt       # openhands-sdk
├── package.json
├── tsconfig.json
└── README.md
```

TypeScript orchestrates; Python touches OpenHands. Two reasons:

1. The orchestration layer (MCP server, state store, poller, planner) is TypeScript per `[[feedback-typescript-default]]`.
2. OpenHands SDK is Python; wrapping it in a tiny runner subprocess keeps the language boundary clean and isolates crashes in the agent loop from the long-running DevClaw process.

## Auth

DevClaw v2 inherits Claude Code OAuth via the `CLAUDE_CODE_EXECUTABLE` + `CLAUDE_CONFIG_DIR` env vars, the same model the local OpenHands smoke test (2026-05-25) validated. **`ANTHROPIC_API_KEY` is actively refused** at both the Python and TypeScript layers — preserves `[[pro-subscription-is-the-design]]` (no metered billing for autonomous overnight runs).

## Local dev

```bash
# One-time setup
cd v2/
npm install
npm run python:install        # creates python-runner/.venv with openhands-sdk
npm install -g @agentclientprotocol/claude-agent-acp   # ACP adapter (one-time, global)

# Build + start the MCP server on stdio
npm run build
npm start
# (server runs until stdin closes; logs go to stderr)

# Or run it directly in dev mode (no build step)
npm run dev
```

Test it from an MCP client (the SDK ships one). A minimal smoke loop will land in `test/client.ts` in the next slice.

## What's NOT here yet (deliberate)

- **HTTP transport** (for OpenClaw integration) — slice 2. Stdio is enough to verify the chain works.
- **State store** (SQLite for task history + audit) — slice 2.
- **Async + notify_url** — slice 2; today the MCP call blocks until OpenHands finishes.
- **Planner** (Goal → multi-task DAG) — slice 3.
- **`fix_bug`, `review_repository`, `run_tests`** as separate tools — slice 3.
- **OpenClaw `mcp.servers` registration** — happens once the HTTP transport is live.

## Tested end-to-end?

Not yet. Next step: a thin client script that calls the server over stdio with a tiny goal and verifies a real file gets created.
