"""Task-status lookup — read spec.yaml + result.json from disk and report state.

The current observable source-of-truth for task state in devclaw is the pair
`~/.life/tasks/<id>/spec.yaml` + `~/.life/tasks/<id>/result.json` (mirrored
under `~/.life/projects/*/tasks/<id>/` for project-bound work). The
`orchestrator.sqlite` checkpointer is a LangGraph internal — it does not hold
a stable spec-level view, so we read the on-disk artifacts directly.

Returns a dict with this shape:
  {
    "task_id": str,
    "state": "ready" | "dispatched-*" | "done" | "blocked" | "unknown",
    "last_action": str | None,        # e.g. "ready", "dispatched-subagent", "done"
    "pr_url": str | None,             # populated from result.json when present
    "completed_at": str | None,       # ISO-8601 string, populated from result.json
    "spec_path": str | None,
    "result_path": str | None,
    "blocker": str | None,
  }

`state="unknown"` is returned (NOT raised) when the task_id has no spec on
disk, so callers can treat unknowns as a normal outcome.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from orchestrator.dispatch import load_spec
from orchestrator.paths import state_tasks_dir

logger = logging.getLogger(__name__)


def _find_task_dir(life_root: Path, task_id: str) -> Path | None:
    flat = state_tasks_dir() / task_id
    if flat.is_dir():
        return flat
    for candidate in life_root.glob(f"projects/*/tasks/{task_id}"):
        if candidate.is_dir():
            return candidate
    return None


def lookup_task_status(task_id: str, *, life_root: Path | None = None) -> dict:
    """Look up a task's current state from the on-disk spec + result files."""
    life_root = life_root or Path("~/.life").expanduser()
    task_dir = _find_task_dir(life_root, task_id)
    if task_dir is None:
        return {
            "task_id": task_id,
            "state": "unknown",
            "last_action": None,
            "pr_url": None,
            "completed_at": None,
            "spec_path": None,
            "result_path": None,
            "blocker": None,
        }

    spec_path = task_dir / "spec.yaml"
    result_path = task_dir / "result.json"

    state = "unknown"
    last_action: str | None = None
    if spec_path.is_file():
        try:
            spec = load_spec(spec_path)
            state = spec.status.value
            last_action = spec.status.value
        except Exception as exc:  # noqa: BLE001
            logger.warning("status: failed to load spec %s: %s", spec_path, exc)

    pr_url: str | None = None
    completed_at: str | None = None
    blocker: str | None = None
    if result_path.is_file():
        try:
            result = json.loads(result_path.read_text())
            pr_url = result.get("pr_url")
            completed_at = result.get("completed_at")
            blocker = result.get("blocker")
            # result.json is the authoritative terminal signal once written.
            terminal = result.get("status")
            if terminal in ("done", "blocked"):
                state = terminal
                last_action = terminal
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("status: failed to read %s: %s", result_path, exc)

    return {
        "task_id": task_id,
        "state": state,
        "last_action": last_action,
        "pr_url": pr_url,
        "completed_at": completed_at,
        "spec_path": str(spec_path) if spec_path.is_file() else None,
        "result_path": str(result_path) if result_path.is_file() else None,
        "blocker": blocker,
    }
