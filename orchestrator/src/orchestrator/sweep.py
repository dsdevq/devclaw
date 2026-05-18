"""Periodic sweep: reap + watchdog passes over all in-flight specs on disk.

Intended to be cron-fired every 15 minutes (the same cadence as the markdown `task_dispatch_15m`). Pure mechanism — no LLM calls, no LangGraph involvement.

Honors the killswitch at `~/.life/system/cron-paused`.

Caps per invocation (architecture §6.2 spirit):
  - reap at most 5 specs
  - watchdog at most 5 specs
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.dispatch import (
    find_completion_artifact,
    is_ghosted,
    load_spec,
    mark_ghosted,
    persist_spec,
    reap_spec,
)
from orchestrator.state.models import TaskStatus

REAP_CAP_PER_TICK = 5
WATCHDOG_CAP_PER_TICK = 5

DISPATCHED_STATUSES = {
    TaskStatus.dispatched_subagent,
    TaskStatus.dispatched_build,
}

logger = logging.getLogger(__name__)


@dataclass
class SweepResult:
    """What a single sweep tick did. Useful for logs + tests."""

    scanned: int = 0
    reaped: list[str] = field(default_factory=list)
    ghosted: list[str] = field(default_factory=list)
    skipped_killswitch: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_killswitch:
            return "sweep_paused: killswitch present"
        return (
            f"scanned={self.scanned} reaped={len(self.reaped)} "
            f"ghosted={len(self.ghosted)} errors={len(self.errors)}"
        )


def find_dispatched_specs(life_root: Path) -> list[Path]:
    """Locate every spec.yaml under ~/.life/ whose status looks dispatched-*.

    Scans BOTH atomic (`tasks/*/spec.yaml`) and run-bound (`projects/*/runs/*/tasks/*/spec.yaml`) paths.
    """
    candidates: list[Path] = []
    for glob in ("tasks/*/spec.yaml", "projects/*/runs/*/tasks/*/spec.yaml"):
        candidates.extend(life_root.glob(glob))
    return candidates


def is_killswitch_set(life_root: Path) -> bool:
    return (life_root / "system" / "cron-paused").exists()


def sweep_once(life_root: Path) -> SweepResult:
    """Run one sweep tick. Returns what happened.

    Order: reap pass first (a runner that finished within deadline keeps credit), then watchdog (kills anything past deadline with no artifact).
    """
    result = SweepResult()

    if is_killswitch_set(life_root):
        result.skipped_killswitch = True
        return result

    candidates = find_dispatched_specs(life_root)

    # Pass 1: reap completed-but-unflipped specs.
    for spec_path in candidates:
        if len(result.reaped) >= REAP_CAP_PER_TICK:
            break
        try:
            spec = load_spec(spec_path)
        except Exception as exc:  # noqa: BLE001 — broad on purpose; one bad spec shouldn't kill the tick
            result.errors.append(f"load failed: {spec_path} — {exc}")
            continue

        result.scanned += 1
        if spec.status not in DISPATCHED_STATUSES or spec.completed_at is not None:
            continue

        artifact = find_completion_artifact(spec_path.parent, spec.kind.value)
        if artifact is None:
            continue

        try:
            reaped = reap_spec(spec, artifact)
            persist_spec(reaped, spec_path)
            result.reaped.append(spec.task_id)
            logger.info("reaped %s via %s", spec.task_id, artifact.name)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"reap failed: {spec.task_id} — {exc}")

    # Pass 2: watchdog ghosted specs.
    # Re-load — pass 1 may have flipped some specs to done.
    for spec_path in candidates:
        if len(result.ghosted) >= WATCHDOG_CAP_PER_TICK:
            break
        try:
            spec = load_spec(spec_path)
        except Exception as exc:  # noqa: BLE001
            # already logged in pass 1 if it failed there
            continue

        if not is_ghosted(spec):
            continue

        try:
            blocked = mark_ghosted(spec)
            persist_spec(blocked, spec_path)
            result.ghosted.append(spec.task_id)
            logger.info("watchdog ghosted %s", spec.task_id)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"watchdog failed: {spec.task_id} — {exc}")

    return result
