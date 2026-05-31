# Changelog

## Unreleased

### Removed
- **v1 retired.** Deleted the LangGraph orchestrator (`orchestrator/`), the markdown dev-workflow skills (`skills/`), their v1 design docs (`docs/architecture-{tasks,curator,orchestrator-port}.md`), and v1 helper scripts/examples. The OpenClaw → DevClaw v2 cutover is complete; v1 lives on in git history as prior art.

### Added
- Optional bearer-token auth for the HTTP transport via `DEVCLAW_TOKEN`. When set, every route except `/health` requires the token (`Authorization: Bearer …` header or `?token=` query param); unset keeps local dev open.

### Changed
- Flattened the project from `v2/` to the repo root and renamed the package `@devclaw/v2` → `devclaw`.
- The MCP server version now derives from `package.json` (was a hardcoded literal that had drifted to `0.0.5`).
- Renamed `python-runner/` → `openhands-runner/` (name by role, not language); the install script is now `npm run openhands:install` (was `python:install`).
- README rewritten around the live v2 system (MCP server + OpenHands execution engine + per-task Docker sandbox); removed references to the personal `~/.life` memory layout and private infra.
