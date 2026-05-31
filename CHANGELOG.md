# Changelog

## Unreleased

### Added
- **Elicitation grill — build a project from scratch (step 4).** Four new MCP tools: `build_project(idea, workspace_dir)` starts a project and DevClaw **grills you one question at a time** (each with a recommended answer) to a shared spec; `answer_question(project_id, answer)` walks the interview; `get_project(project_id)` shows state across phases; `approve_spec(project_id)` plans the spec into a milestone DAG and starts the build. The grill methodology adapts [Matt Pocock's MIT `grill-me`](https://github.com/mattpocock/skills); cognition runs via `claude` (mechanism/cognition split), capped at `DEVCLAW_MAX_GRILL_QUESTIONS` (default 20). Project idea/transcript/spec persist to `$DEVCLAW_STATE/projects/<id>/` (human-readable). New `devclaw/{elicitation,project_store,project_service}.py`; approval hands an already-planned DAG to the executor via `TaskQueue.start_planned_program` (no plan-time recovery edge case).
- **Plan-from-spec (build-from-scratch step 3).** New `planner.plan_spec(spec, workspace_dir)` decomposes an *approved spec* (the shared understanding from the elicitation grill) into a **milestone-ordered DAG** — grounded in the spec's milestones, acceptance criteria, scope, and constraints, distinct from the bare-goal `plan_goal`. Tasks now carry an optional `milestone` (new SQLite column + forward-compat ALTER, surfaced in `get_program`/`list_tasks`), persisted by the queue. The `build_project` tool that produces the spec lands in step 4.
- **Durability + crash recovery (build-from-scratch step 2).** Scheduling is now reconciled from DB state by a single `_pump`, so concurrency is crash-consistent (derived from `running` rows, not an in-memory counter). On startup `recover()` resets tasks orphaned in `running` by a dead process back to `pending` (logged as a `reaped` event); a **heartbeat tick** (`DEVCLAW_TICK_SECONDS`, default 10s) advances DAGs and resumes recovered work from disk, so a multi-day build survives restarts. A **cheap-idle guard** makes an idle tick ~free (one COUNT). New **global concurrency cap** `DEVCLAW_MAX_CONCURRENT` (default 4) with backpressure, alongside the per-program cap.

### Changed
- **Rewrote the host orchestration from TypeScript to all-Python.** The MCP server is now [FastMCP](https://github.com/jlowin/fastmcp) (`devclaw/server.py`); the planner, SQLite state store, async task queue, and docker-sandbox runner are Python modules under `devclaw/`. The TypeScript (`src/`, `test/`, `package.json`, `tsconfig.json`) is gone. Behaviour, MCP tool surface, wire shapes, transports (stdio + streamable-HTTP), the `/dashboard` + SSE feed, and the `DEVCLAW_TOKEN` bearer auth are all preserved. Rationale: OpenHands has a Python-only SDK, so Python was already mandatory inside the sandbox — going all-Python collapses the two-language split into one toolchain. Run it with `devclaw-mcp` (or `python -m devclaw.server`); test with `pytest`. The in-sandbox `openhands-runner/runner.py` is unchanged.

### Removed
- **v1 retired.** Deleted the LangGraph orchestrator (`orchestrator/`), the markdown dev-workflow skills (`skills/`), their v1 design docs (`docs/architecture-{tasks,curator,orchestrator-port}.md`), and v1 helper scripts/examples. The OpenClaw → DevClaw v2 cutover is complete; v1 lives on in git history as prior art.

### Added
- Optional bearer-token auth for the HTTP transport via `DEVCLAW_TOKEN`. When set, every route except `/health` requires the token (`Authorization: Bearer …` header or `?token=` query param); unset keeps local dev open.

### Changed
- Flattened the project from `v2/` to the repo root and renamed the package `@devclaw/v2` → `devclaw`.
- The MCP server version now derives from `package.json` (was a hardcoded literal that had drifted to `0.0.5`).
- Renamed `python-runner/` → `openhands-runner/` (name by role, not language); the install script is now `npm run openhands:install` (was `python:install`).
- README rewritten around the live v2 system (MCP server + OpenHands execution engine + per-task Docker sandbox); removed references to the personal `~/.life` memory layout and private infra.
