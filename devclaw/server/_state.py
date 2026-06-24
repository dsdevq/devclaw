"""Module-level state for the devclaw MCP server.

Owns the FastMCP instance + the four long-lived services (state store, task
queue, goal service, project registry) + env-driven config. Imported by
`tools`, `http`, and `lifecycle` — those modules attach decorators or call
methods, they don't create state.
"""

from __future__ import annotations

import os
import sys
import urllib.parse

from fastmcp import FastMCP
from pydantic import Field

from .. import __version__
from ..goal_service import GoalService
from ..project_registry import ProjectRegistry
from ..state_store import StateStore
from ..task_queue import TaskQueue

SERVER_NAME = "devclaw"
DB_PATH = os.path.abspath(os.environ.get("DEVCLAW_DB", "devclaw.db"))
HTTP_PORT = int(os.environ.get("DEVCLAW_PORT", "8000"))
# Default 0.0.0.0 so sibling compose containers (e.g. openclaw-gateway) can
# reach the endpoint. Set DEVCLAW_HOST=127.0.0.1 to restrict to loopback.
HTTP_HOST = os.environ.get("DEVCLAW_HOST", "0.0.0.0")
# Optional bearer-token guard for the HTTP transport. When DEVCLAW_TOKEN is set,
# every route except /health requires it — via `Authorization: Bearer <token>`
# (MCP clients) or a `?token=<token>` query param (the browser dashboard +
# EventSource, which can't set headers). Unset -> auth disabled (local dev).
AUTH_TOKEN = os.environ.get("DEVCLAW_TOKEN", "")
TOKEN_QS = f"?token={urllib.parse.quote(AUTH_TOKEN)}" if AUTH_TOKEN else ""

store = StateStore(DB_PATH)
_engine = os.environ.get("DEVCLAW_ENGINE", "")
if _engine == "stub":
    # Harness-validation mode: deterministic stub engine + cognition, no docker,
    # no claude. Proves the plumbing around the agent; never use in production.
    from ..stub_engine import stub_engine, stub_goal_planner

    sys.stderr.write(
        "⚠ DEVCLAW_ENGINE=stub — deterministic stub engine + cognition "
        "(NO OpenHands, NO claude). For harness validation only.\n"
    )
    queue = TaskQueue(store, planner=stub_goal_planner, runner=stub_engine)
elif _engine == "host":
    # Real cognition + real OpenHands, but run on the HOST with NO sandbox.
    from ..host_runner import run_host

    sys.stderr.write(
        "⚠ DEVCLAW_ENGINE=host — OpenHands runs on the HOST with NO sandbox "
        "isolation (agent has full filesystem access). Dev/validation only.\n"
    )
    queue = TaskQueue(store, runner=run_host)
else:
    queue = TaskQueue(store)

# The goal layer (folded-in goalclaw): durable, steerable, evaluated goals driven
# across heartbeats, dispatching into the SAME queue in-process. Owns goals under
# DEVCLAW_GOALS_DIR; the heartbeat + on-settle wake are started in the entrypoint.
goals = GoalService(queue, store)

# The project registry (control plane): the single source of truth for "which
# repos is devclaw working on, and what's the status of each". Thin — it links to
# goals by id and joins their live status on read (project_rollup), never caching
# phase. Shares the SQLite file with the state store.
registry = ProjectRegistry(DB_PATH)


def _goal_get(goal_id: str) -> dict:
    """Read-only goal status getter for the project rollup (raises KeyError)."""
    return goals.get_goal(goal_id)


mcp: FastMCP = FastMCP(SERVER_NAME, version=__version__)

LimitField = Field(ge=1, le=1000)
