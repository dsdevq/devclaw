# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project orientation

DevClaw is a **FastMCP server that orchestrates autonomous coding work**. It is "the chef": an OpenClaw waiter agent translates Denys's chat into MCP tool calls; devclaw plans, dispatches, evaluates, and ships. Cognition is always `claude` over a Pro/Max OAuth session — `ANTHROPIC_API_KEY` is **actively refused** at both host and sandbox layers (don't add code that reintroduces it).

Read `README.md` and `docs/architecture-layers.md` before non-trivial work — the 5-layer model there is locked and architectural changes are judged against it.

## Common commands

```bash
# Setup
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the MCP server
DEVCLAW_TRANSPORT=stdio devclaw-mcp                          # local dev
DEVCLAW_TRANSPORT=http DEVCLAW_PORT=8000 devclaw-mcp         # long-running; dashboard at /goals /projects /dashboard
python -m devclaw.server                                     # equivalent

# Control-plane CLI (talks to the same stores; no server needed)
devclaw projects list
python -m devclaw.cli projects show <slug>

# Tests — all unit, all stubbed (no docker, no claude). Fast.
pytest
pytest tests/test_goal_tick.py                               # single file
pytest tests/test_goal_tick.py::test_specific_case           # single test
pytest -k firming                                            # by keyword

# Real-pipeline validation (requires docker + logged-in claude): docs/live-shakedown.md
```

`pytest` collection is **pinned to `tests/`** in `pyproject.toml` because the stub engine writes throwaway `test_*.py` into `evals/runs/`. Don't add tests outside `tests/`.

## Engine modes (`DEVCLAW_ENGINE`)

| Value | Engine | Use |
|---|---|---|
| *(unset)* | OpenHands in a per-task docker sandbox | production |
| `host` | OpenHands on the host (no isolation) | dev/CI when docker is unavailable |
| `claude_sdk` | `claude --print` inside the same sandbox | spike — see `docs/engine-decision.md` |
| `stub` | deterministic stub | the entire `pytest` suite runs on this |

The full ~60-var env reference lives in `docs/env-vars.md`. `.env` is loaded at startup; shell/systemd env always wins.

## Architecture — the 5 layers (locked)

Strict chain: **1 → 2 → 3** (cognition) or **1 → 2 → 4 → 5** (execution). No cross-layer reach-through.

1. **MCP surface** — `devclaw/server/` (FastMCP tools, HTTP routes, dashboard, bearer-token auth). `tools.py` = every `@mcp.tool`; `http.py` = every `@mcp.custom_route`; `_state.py` = long-lived services + env; `lifecycle.py` = main + serve loops.
2. **Orchestrator** — `devclaw/goal/` (the durable goal layer). `GoalService` is the facade the server wires up. `tick.py` is one heartbeat: check → plan → evaluate → dispatch → done-gate. `store.py` owns disk state (`goal.yaml` · `STATUS.md` · `log.md` · `inbox.md` · `deliveries.md`). Goal lifecycle: `investigating → firming → executing`.
3. **Cognition callers** — `goal/{planner,decomposer,evaluator,firming,summary}.py` + top-level `planner.py`, `elicitation.py`. One-shot `claude --print` with baked prompts (`devclaw/prompts/*.md`, loaded by slug) → parsed YAML. **Stateless**. Conform to the `Cognition` protocol in `devclaw/cognition.py` so they can be stubbed.
4. **TaskQueue + Engine** — `devclaw/task_queue.py` + `devclaw/engine/`. The `Engine` protocol (`engine/__init__.py`) is one async callable `(EngineRequest) → EngineResult`. Implementations: `sandcastle.py` (prod), `claude_sdk.py` (spike), `host.py` (testing only), `stub.py` (tests).
5. **Worker harness** — `openhands-runner/runner.py` runs **inside the sandbox container** (not the devclaw package). Talks to the host via a line-delimited JSON protocol on stdout (`event:` lines + one terminating `result:`). This is the only true agent turn-loop in the stack.

### Cross-cutting

- `devclaw/loom/` — engine-agnostic substrate: `limits.py` (rate-limit classifier), `test_integrity.py` (gate guard against deleted/weakened tests), `trace.py` (run-trace recorder).
- `devclaw/quality/` — `__init__.py` is the pre-PR adversarial diff review; `eval_judge.py` + `evals.py` score eval runs.
- `devclaw/delivery/` — commit → branch → push → PR (`__init__.py`), Tailscale deploy (`deploy.py`), `gh repo create` (`repo.py`).
- `devclaw/state_store.py` — SQLite, append-only event log for programs/tasks. `devclaw/project_registry.py` — control plane joining repos ↔ goals.

## Invariants (do NOT violate)

- **Single source of truth per state.** Goal state in `GoalStore` (disk yaml/markdown). Task state in `StateStore` (SQLite, append-only). No upstream caching.
- **No `ANTHROPIC_API_KEY`.** Both planner and sandbox actively refuse it — autonomous runs must use Pro OAuth via the host's logged-in `claude` CLI.
- **Engines are pure async callables.** No back-channel; everything needed comes in `EngineRequest`.
- **Cognition callers are stateless.** No process-level memory across calls.
- **Goal store is owned by the heartbeat.** Once a goal exists, only the tick loop mutates it.
- **The done-gate is grounded, not vibes-based.** The planner's `done` is a *proposal* — it triggers a read-only `review_repository` against the firmed `done_when` + `stub_acceptable`. A clause shipped as a stub flips to unsatisfied unless the owner authorized that clause in `stub_acceptable`.

### Model-agnostic invariants (worker harness layer)

The skill/hook system in `openhands-runner/` is deliberately neutral about which agent runs in the sandbox. Don't add code that violates these:

- **Skills are plain markdown** in `openhands-runner/skills/` (universal, baked into the image at `/opt/devclaw/skills/`) and `<repo>/.agent/skills/` (per-repo, agent-discovered via `ls` + `cat`). **No frontmatter with model-specific fields. No `Skill(name=…)` tool invocations** — that's Claude's native skill system, not ours.
- **Hooks are `.sh` files** in `openhands-runner/hooks/` (→ `/opt/devclaw/hooks/`) and `<repo>/.agent/hooks/`. `runner.py` invokes them directly. Do **not** move them into a Claude-Code `settings.json` or any other harness-native config.
- **Tools cross via MCP**, not vendor-specific wiring.
- `openhands-runner/runner.py` must not import from the `devclaw` Python package — it runs in a different container.

## Adding new functionality — the question tree

1. Does this fit an existing layer? → put it there. Most things do.
2. Is it a new surface on an existing layer? → extend the layer's protocol, write the conformance test, implement.
3. Is it cross-layer machinery (a new skill/hook)? → worker harness (layer 5), model-agnostic.
4. Is it a new layer? → **stop**. Re-read `docs/architecture-layers.md`. Talk to Denys before proposing.

## Doc map

- `docs/architecture-layers.md` — the locked 5-layer contract + invariants.
- `docs/architecture-v2.md` — the "why OpenHands + sandbox isolation are orthogonal" rationale.
- `docs/engine-decision.md` — decision rule for sandcastle vs. claude_sdk.
- `docs/task-execution-flow.md` — temporal walk of one task across nodes.
- `docs/env-vars.md` — full env-var reference (~60 vars).
- `docs/live-shakedown.md` — layered runbook for validating the real pipeline.
- `docs/vps-waiter-deploy.md` — deploy notes.
