"""Runtime path resolution for devclaw.

`life_root` is the knowledge vault (`~/.life/`, `/srv/life/`). `state_dir` is the
runtime-state location for things the orchestrator owns but that don't belong in
the knowledge layer: `orchestrator.sqlite`, `intake_index.json`, the flat-bucket
`tasks/<id>/` directory, and (via skill prompts) `queue.jsonl`, `.curator-proposed/`,
`.last_consolidation`.

See `system/proposals.md → 2026-05-27-runtime-knowledge-split` for the split rationale.
"""

from __future__ import annotations

import os
from pathlib import Path


def state_dir() -> Path:
    """Resolve the devclaw/lifekit runtime-state dir.

    Order: LIFEKIT_STATE_DIR env, then $XDG_STATE_HOME/lifekit, then
    ~/.local/state/lifekit. Mirrors lifekit.core.paths.state_dir().
    """
    override = os.environ.get("LIFEKIT_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser().resolve() / "lifekit"
    return Path.home() / ".local" / "state" / "lifekit"


def state_tasks_dir() -> Path:
    """The flat-bucket task-spec dir (per-project specs stay under life_root/projects/)."""
    return state_dir() / "tasks"
