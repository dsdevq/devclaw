"""Periodic sweep: dispatch + reap + watchdog passes over all in-flight specs on disk.

Intended to be cron-fired every 15 minutes (the same cadence as the markdown `task_dispatch_15m`). Pure mechanism — no LLM calls, no LangGraph invocation in the sweep itself; the dispatch pass fires the per-task graph as a subprocess.

Honors the killswitch at `~/.life/system/cron-paused`.

Caps per invocation (architecture §6.2 spirit):
  - dispatch at most 3 ready atomic specs
  - reap at most 5 dispatched-with-artifact specs
  - watchdog at most 5 ghosted specs

Order: reap first (give credit to late-but-complete runners) → watchdog (kill ghosts) → dispatch (fire new ready specs). This ensures a tick that picks up a freshly-dispatched ghost-then-reaper-then-redispatch state behaves correctly.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from orchestrator.dispatch import (
    compute_watchdog_deadline,
    find_completion_artifact,
    is_ghosted,
    load_spec,
    mark_ghosted,
    now_utc,
    persist_spec,
    reap_spec,
)
from orchestrator.events import (
    AnnounceCallback,
    emit_dispatched,
    emit_done,
    emit_terminal_failure,
)
from orchestrator.notify import notify_telegram
from orchestrator.state.models import TaskSpec, TaskStatus

DISPATCH_CAP_PER_TICK = 3
REAP_CAP_PER_TICK = 5
WATCHDOG_CAP_PER_TICK = 5

DISPATCHED_STATUSES = {
    TaskStatus.dispatched_subagent,
    TaskStatus.dispatched_build,
}


# Dispatch abstraction so tests don't fork real processes.
SpecDispatcher = Callable[[Path], Optional[str]]


def _popen_dispatch_cli(spec_path: Path) -> str:
    """Production dispatcher: Popen the `devclaw-orchestrator dispatch` CLI.

    Per-task output (claude --print stdout/stderr) is appended to a `dispatch.log` next to the spec, so post-mortem debugging can recover what the runner actually said. The fd is intentionally not closed here — Popen retains it for the lifetime of the child.
    """
    log_path = spec_path.parent / "dispatch.log"
    log_fh = open(log_path, "ab")
    proc = subprocess.Popen(
        ["devclaw-orchestrator", "dispatch", str(spec_path)],
        stdout=log_fh,
        stderr=log_fh,
        close_fds=True,
    )
    return f"pid:{proc.pid}"

logger = logging.getLogger(__name__)


@dataclass
class SweepResult:
    """What a single sweep tick did. Useful for logs + tests."""

    scanned: int = 0
    dispatched: list[str] = field(default_factory=list)
    reaped: list[str] = field(default_factory=list)
    ghosted: list[str] = field(default_factory=list)
    skipped_killswitch: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_killswitch:
            return "sweep_paused: killswitch present"
        return (
            f"scanned={self.scanned} dispatched={len(self.dispatched)} "
            f"reaped={len(self.reaped)} ghosted={len(self.ghosted)} "
            f"errors={len(self.errors)}"
        )


def find_dispatched_specs(life_root: Path) -> list[Path]:
    """Locate every spec.yaml under ~/.life/ whose status looks dispatched-*.

    Scans BOTH atomic (`tasks/*/spec.yaml`) and run-bound (`projects/*/runs/*/tasks/*/spec.yaml`) paths.
    """
    candidates: list[Path] = []
    for glob in (
        "tasks/*/spec.yaml",
        "projects/*/tasks/*/spec.yaml",
        "projects/*/runs/*/tasks/*/spec.yaml",
    ):
        candidates.extend(life_root.glob(glob))
    return candidates


def find_all_specs(life_root: Path) -> list[Path]:
    """Locate every spec.yaml under ~/.life/ (any status).

    Used by the dispatch pass to find `status: ready` specs that need a runner.
    """
    candidates: list[Path] = []
    for glob in (
        "tasks/*/spec.yaml",
        "projects/*/tasks/*/spec.yaml",
        "projects/*/runs/*/tasks/*/spec.yaml",
    ):
        candidates.extend(life_root.glob(glob))
    return candidates


def is_killswitch_set(life_root: Path) -> bool:
    return (life_root / "system" / "cron-paused").exists()


def sweep_once(
    life_root: Path,
    *,
    dispatcher: SpecDispatcher = _popen_dispatch_cli,
    events_announce: AnnounceCallback | None = None,
    events_chat: str | None = None,
) -> SweepResult:
    """Run one sweep tick: reap → watchdog → dispatch.

    Order rationale:
      - Reap first: a late-but-complete runner gets credit before its deadline trips.
      - Watchdog second: kill anything past deadline with no artifact.
      - Dispatch last: fire new `ready` specs; if a spec just-reaped is `ready` again (it isn't, but logically) it could be redispatched in the same tick.
    """
    result = SweepResult()

    if is_killswitch_set(life_root):
        result.skipped_killswitch = True
        return result

    all_specs = find_all_specs(life_root)

    # Pass 1: reap completed-but-unflipped specs.
    for spec_path in all_specs:
        if len(result.reaped) >= REAP_CAP_PER_TICK:
            break
        try:
            spec = load_spec(spec_path)
        except Exception as exc:  # noqa: BLE001
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
            # Event 3/4/5: reap is the recovery path where the runner subprocess
            # died before persisting its terminal state. We owe the event the
            # subprocess would have fired.
            if events_announce is not None:
                target = events_chat or "default"
                if reaped.status == TaskStatus.done:
                    pr_url = _extract_pr_url(spec_path.parent, reaped)
                    emit_done(
                        events_announce,
                        target,
                        task_id=reaped.task_id,
                        pr_url=pr_url,
                    )
                else:
                    emit_terminal_failure(
                        events_announce,
                        target,
                        task_id=reaped.task_id,
                        new_state="failed",
                        reason=reaped.result_summary,
                    )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"reap failed: {spec.task_id} — {exc}")

    # Pass 2: watchdog ghosted specs.
    for spec_path in all_specs:
        if len(result.ghosted) >= WATCHDOG_CAP_PER_TICK:
            break
        try:
            spec = load_spec(spec_path)
        except Exception:  # noqa: BLE001
            continue

        if not is_ghosted(spec):
            continue

        try:
            blocked = mark_ghosted(spec)
            persist_spec(blocked, spec_path)
            result.ghosted.append(spec.task_id)
            logger.info("watchdog ghosted %s", spec.task_id)
            # Event 5: watchdog flipped a silent runner to blocked — surface
            # it as "abandoned" so operators can tell it apart from a runner-
            # reported failure.
            if events_announce is not None:
                emit_terminal_failure(
                    events_announce,
                    events_chat or "default",
                    task_id=blocked.task_id,
                    new_state="abandoned",
                    reason=blocked.result_summary,
                )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"watchdog failed: {spec.task_id} — {exc}")

    # Pass 3: dispatch ready atomic specs.
    # Run-bound specs are dispatched by the supervisor, not here, so this pass only fires for specs whose `run` field is null.
    # Build an id→spec map once so the readiness gate can look up dep status without re-reading disk per spec.
    specs_by_id: dict[str, TaskSpec] = {}
    for spec_path in all_specs:
        try:
            s = load_spec(spec_path)
        except Exception:  # noqa: BLE001
            continue
        specs_by_id[s.task_id] = s

    unknown_dep_warning_emitted = False
    for spec_path in all_specs:
        if len(result.dispatched) >= DISPATCH_CAP_PER_TICK:
            break
        try:
            spec = load_spec(spec_path)
        except Exception:  # noqa: BLE001
            continue

        if spec.status != TaskStatus.ready or spec.run is not None:
            continue

        ready, reason = _ready_to_dispatch(spec, specs_by_id)
        if not ready:
            if reason == "unknown_dep" and not unknown_dep_warning_emitted:
                logger.warning(
                    "sweep: one or more ready specs reference unknown deps; leaving in ready"
                )
                unknown_dep_warning_emitted = True
            continue

        try:
            dispatched = _mark_atomic_dispatched(spec)
            persist_spec(dispatched, spec_path)
            specs_by_id[spec.task_id] = dispatched  # keep map current within the tick
            run_id_str = dispatcher(spec_path) or ""
            result.dispatched.append(spec.task_id)
            logger.info(
                "dispatched ready atomic spec %s (popen=%s)",
                spec.task_id,
                run_id_str,
            )
            notify_telegram(
                spec.requester_route.to,
                f"🚀 dispatched {spec.task_id} (kind={spec.kind.value})",
            )
            # Event 2: ready → dispatched-* transition.
            if events_announce is not None:
                emit_dispatched(
                    events_announce,
                    events_chat or "default",
                    task_id=dispatched.task_id,
                    runner_kind=dispatched.dispatch_target or "subagent",
                )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"dispatch failed: {spec.task_id} — {exc}")

    return result


def _ready_to_dispatch(
    spec: TaskSpec, all_specs_by_id: dict[str, TaskSpec]
) -> tuple[bool, str | None]:
    """Decide whether a `ready` spec is dispatch-eligible given current dep state.

    Returns (True, None) iff every `depends_on` entry resolves to a known spec
    whose status is `done`. Otherwise returns (False, reason) where reason is
    one of: "unknown_dep" (a listed dep id has no matching spec on disk),
    "dep_not_done" (a listed dep exists but hasn't reached `done` yet).
    Empty depends_on trivially passes — backward-compatible with pre-DAG specs.
    """
    for dep_id in spec.depends_on:
        dep = all_specs_by_id.get(dep_id)
        if dep is None:
            return (False, "unknown_dep")
        if dep.status != TaskStatus.done:
            return (False, "dep_not_done")
    return (True, None)


def detect_cycle(
    spec: TaskSpec, all_specs_by_id: dict[str, TaskSpec]
) -> list[str] | None:
    """If adding `spec` to the DAG introduces a cycle, return the cycle path; else None.

    The returned list reads as the offending chain (e.g. ["a", "b", "a"]). Used by
    `task_intake` (and any other writer) to reject a spec before it lands on disk.
    """
    graph = dict(all_specs_by_id)
    graph[spec.task_id] = spec

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in graph}

    def visit(node: str, stack: list[str]) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for dep in graph[node].depends_on:
            if dep not in graph:
                continue  # unknown deps aren't a cycle; sweep handles them
            if color[dep] == GRAY:
                idx = stack.index(dep)
                return stack[idx:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep, stack)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    # Only need to search from the newly-introduced node — the existing graph
    # is assumed acyclic (each prior insertion went through this check).
    return visit(spec.task_id, [])


def _mark_atomic_dispatched(spec: TaskSpec) -> TaskSpec:
    """Flip a ready atomic spec to dispatched-subagent with a watchdog deadline.

    Pure function; caller persists. We do this BEFORE Popening the dispatch CLI so a race with another concurrent sweep can't double-dispatch (next tick will see status=dispatched-* and skip).
    """
    dispatched_at = now_utc()
    return spec.model_copy(
        update={
            "status": TaskStatus.dispatched_subagent,
            "dispatch_target": "subagent",
            "dispatched_at": dispatched_at,
            "watchdog_deadline": compute_watchdog_deadline(
                dispatched_at, spec.budget.max_runtime_seconds
            ),
        }
    )


def _extract_pr_url(task_dir: Path, reaped_spec: TaskSpec) -> str | None:
    """Find a pr_url for a reaped spec without forcing the caller to re-parse
    result.json.

    Search order: `result_summary` of the reaped spec (reap_spec embeds
    `PR: <url>` when result.json had one) → `task_dir/result.json` → None.
    """
    summary = reaped_spec.result_summary or ""
    if summary.startswith("PR: "):
        candidate = summary[len("PR: ") :].strip()
        if candidate:
            return candidate

    result_json = task_dir / "result.json"
    if result_json.is_file():
        try:
            import json

            data = json.loads(result_json.read_text())
            url = data.get("pr_url")
            if isinstance(url, str) and url.strip():
                return url.strip()
        except (OSError, ValueError):
            return None
    return None


# Backwards-compatible alias — older callers use `find_dispatched_specs`.
find_dispatched_specs = find_all_specs
