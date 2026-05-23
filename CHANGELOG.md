# Changelog

## Unreleased

### Added
- `orchestrator/src/orchestrator/mcp_server.py` — local execution mode (`DEVCLAW_LOCAL=1`) so the MCP server runs as a VPS container without SSH. Three new MCP tools: `devclaw_list` (recent task history), `devclaw_logs` (full task context for debugging), `devclaw_unblock` (provide a decision and re-queue a blocked task). HTTP transport (`--transport http`) for container deployment alongside openclaw-gateway.
- `orchestrator/src/orchestrator/cli.py` — `logs <task_id>` subcommand (enriched status with intent, acceptance criteria, result detail) and `unblock <task_id> --decision <text>` subcommand (writes `decision.yaml` + resets spec to ready).
- `skills/devclaw-proxy/SKILL.md` — OpenClaw skill for conversational devclaw interaction: filing tasks, checking status, debugging failures, resolving blockers.

### Changed
- `orchestrator/pyproject.toml` — added `uvicorn>=0.30.0` dependency for HTTP transport mode.
