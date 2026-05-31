# Changelog

## Unreleased

### Removed
- **v1 retired.** Deleted the LangGraph orchestrator (`orchestrator/`), the markdown dev-workflow skills (`skills/`), their v1 design docs (`docs/architecture-{tasks,curator,orchestrator-port}.md`), and v1 helper scripts/examples. The OpenClaw → DevClaw v2 cutover is complete; v1 lives on in git history as prior art.

### Changed
- Flattened the project from `v2/` to the repo root and renamed the package `@devclaw/v2` → `devclaw`.
- Renamed `python-runner/` → `openhands-runner/` (name by role, not language); the install script is now `npm run openhands:install` (was `python:install`).
- README rewritten around the live v2 system (MCP server + OpenHands execution engine + per-task Docker sandbox); removed references to the personal `~/.life` memory layout and private infra.
