"""DevClaw MCP server — the chef.

Layout:
  - ``_state``   — FastMCP instance + long-lived services (store, queue,
                   goals, registry) + env-driven config. Imported by the rest.
  - ``tools``    — every ``@mcp.tool`` decorator (the chef's menu).
  - ``http``     — every ``@mcp.custom_route`` handler (dashboard + SSE +
                   ``/goals/answer`` telegram-reply hook).
  - ``lifecycle``— ``main()`` entrypoint, the stdio/http serve loops, and the
                   bearer-token auth middleware.

Transport:
  - DEVCLAW_TRANSPORT=stdio (default) — local dev + tests
  - DEVCLAW_TRANSPORT=http            — streamable-http on $DEVCLAW_PORT (default
                                        8000); also serves /dashboard +
                                        /programs/:id/events (SSE)

State: SQLite at $DEVCLAW_DB (default ./devclaw.db).
"""

from ._state import goals, mcp, queue, registry, store  # noqa: F401
from . import tools  # noqa: F401  — registers @mcp.tool decorators
from . import http  # noqa: F401  — registers @mcp.custom_route handlers
from .http import goals_answer  # noqa: F401  — re-exported for tests
from .lifecycle import main  # noqa: F401  — entry point (pyproject script)
