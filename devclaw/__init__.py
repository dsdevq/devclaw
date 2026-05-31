"""DevClaw — a thin orchestration layer in front of OpenHands.

TypeScript is gone; this is the all-Python host runtime. The MCP server,
planner, SQLite state store, task queue, and docker-sandbox runner all live
here. The only other code is ``openhands-runner/runner.py``, which runs the
OpenHands SDK *inside* the per-task sandbox container.
"""

__all__ = ["__version__"]

try:  # populated from package metadata once installed
    from importlib.metadata import version as _version

    __version__ = _version("devclaw")
except Exception:  # running from a source tree that isn't installed
    __version__ = "0.0.1"
