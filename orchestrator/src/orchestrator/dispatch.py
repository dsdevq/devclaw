"""Deterministic dispatch, reap, and watchdog passes.

This is the Python port of the markdown `task_dispatch` skill's three passes. Pure mechanism — zero LLM calls. Each function is unit-testable in isolation.

The three passes (in order):

  1. Dispatch: spec.status == ready  →  spec.status == dispatched-* + watchdog_deadline written
  2. Reap:     spec.status == dispatched-* AND completion artifact on disk  →  spec.status == done
  3. Watchdog: spec.status == dispatched-* AND now > watchdog_deadline AND no artifact  →  spec.status == blocked

In the LangGraph world, the dispatch pass is what kicks off a new graph run for a spec. The reap and watchdog passes run on a separate cron-fired graph that sweeps over in-flight specs.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from orchestrator.state.models import (
    Result,
    TaskSpec,
    TaskStatus,
)

WATCHDOG_GRACE_SECONDS = 300


def now_utc() -> datetime:
    """Single source of timestamps — pinch-point for test injection."""
    return datetime.now(timezone.utc)


# ─── Dispatch ────────────────────────────────────────────────────────────────


def compute_watchdog_deadline(dispatched_at: datetime, budget_seconds: int) -> datetime:
    """Deadline = dispatched_at + budget + grace. Watchdog fires when now > deadline."""
    return dispatched_at + timedelta(seconds=budget_seconds + WATCHDOG_GRACE_SECONDS)


def mark_dispatched(
    spec: TaskSpec,
    dispatch_run_id: str,
    dispatch_target: str = "subagent",
    dispatched_at: datetime | None = None,
) -> TaskSpec:
    """Pure function: produce a new TaskSpec with dispatch fields populated.

    Caller is responsible for persisting the new spec (yaml file write + checkpointer commit).
    """
    ts = dispatched_at or now_utc()
    return spec.model_copy(
        update={
            "status": TaskStatus(f"dispatched-{dispatch_target}"),
            "dispatch_target": dispatch_target,
            "dispatch_run_id": dispatch_run_id,
            "dispatched_at": ts,
            "watchdog_deadline": compute_watchdog_deadline(ts, spec.budget.max_runtime_seconds),
        }
    )


# ─── Reap ────────────────────────────────────────────────────────────────────


def find_completion_artifact(task_dir: Path, kind: str) -> Path | None:
    """Look for evidence a runner completed despite not flipping the spec.

    Search order:
      1. result.json next to spec.yaml (code-task contract)
      2. findings.md next to spec.yaml (research / draft contract)
      3. run.log.jsonl with a `subagent_complete` event (legacy / fallback)
    """
    result_json = task_dir / "result.json"
    if result_json.is_file():
        return result_json

    if kind in ("research", "draft"):
        findings = task_dir / "findings.md"
        if findings.is_file():
            return findings

    run_log = task_dir / "run.log.jsonl"
    if run_log.is_file():
        try:
            for line in run_log.read_text().splitlines():
                if not line.strip():
                    continue
                event = json.loads(line)
                if event.get("event") == "subagent_complete":
                    return run_log
        except (json.JSONDecodeError, OSError):
            return None

    return None


def reap_spec(spec: TaskSpec, artifact: Path) -> TaskSpec:
    """Flip a stale dispatched-* spec to done using the artifact's data.

    Pure function. Caller persists.
    """
    completed_at = now_utc()
    summary = f"reaped from {artifact.name} — runner finished but did not flip spec"

    if artifact.name == "result.json":
        try:
            data = json.loads(artifact.read_text())
            parsed = Result.model_validate(data)
            return spec.model_copy(
                update={
                    "status": TaskStatus.done
                    if parsed.status == "done"
                    else TaskStatus.blocked,
                    "completed_at": parsed.completed_at,
                    "result_summary": parsed.notes
                    or f"PR: {parsed.pr_url}"
                    if parsed.pr_url
                    else summary,
                }
            )
        except (json.JSONDecodeError, OSError, ValueError):
            # malformed artifact — fall through to generic reap
            pass

    if artifact.name == "findings.md":
        try:
            first_line = artifact.read_text().splitlines()[0].lstrip("# ").strip()
            summary = f"{first_line} (reaped — runner did not flip spec)"
        except (OSError, IndexError):
            pass

    return spec.model_copy(
        update={
            "status": TaskStatus.done,
            "completed_at": completed_at,
            "result_summary": summary,
        }
    )


# ─── Watchdog ────────────────────────────────────────────────────────────────


def is_ghosted(spec: TaskSpec, current_time: datetime | None = None) -> bool:
    """Has this spec passed its watchdog deadline with no completion?"""
    if spec.status not in (
        TaskStatus.dispatched_subagent,
        TaskStatus.dispatched_build,
    ):
        return False
    if spec.completed_at is not None:
        return False
    if spec.watchdog_deadline is None:
        # spec was dispatched before this mechanism existed; we can't watchdog it
        return False
    t = current_time or now_utc()
    return t > spec.watchdog_deadline


def mark_ghosted(spec: TaskSpec) -> TaskSpec:
    """Flip a ghosted spec to blocked. Pure function. Caller persists."""
    return spec.model_copy(
        update={
            "status": TaskStatus.blocked,
            "completed_at": now_utc(),
            "result_summary": (
                "runner_silent_past_deadline — no result.json/findings.md/subagent_complete "
                f"after dispatched_at + budget + {WATCHDOG_GRACE_SECONDS}s grace. "
                "Likely the subagent died before writing anything (image pull, OOM, "
                "transient infra)."
            ),
        }
    )


# ─── YAML persistence (single-writer helpers) ────────────────────────────────


def persist_spec(spec: TaskSpec, spec_path: Path) -> None:
    """Atomic write of TaskSpec to spec.yaml. Single source of yaml-mutation truth."""
    payload = spec.model_dump(mode="json", exclude_none=False)
    tmp = spec_path.with_suffix(spec_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
    tmp.replace(spec_path)


def load_spec(spec_path: Path) -> TaskSpec:
    """Load a TaskSpec from disk. Used at boundary points (cron-fired sweeps)."""
    return TaskSpec.model_validate(yaml.safe_load(spec_path.read_text()))


# ─── merged_at helpers ──────────────────────────────────────────────────────


def find_spec_for_task(life_root: Path, task_id: str) -> Path | None:
    """Locate spec.yaml for `task_id` under either state_dir/tasks/ (flat-bucket)
    or life_root/projects/* (per-project)."""
    from orchestrator.paths import state_tasks_dir

    flat = state_tasks_dir() / task_id / "spec.yaml"
    if flat.is_file():
        return flat
    for glob_pattern in (
        f"projects/*/tasks/{task_id}/spec.yaml",
        f"projects/*/runs/*/tasks/{task_id}/spec.yaml",
    ):
        for hit in life_root.glob(glob_pattern):
            return hit
    return None


def stamp_merged_at(
    spec_path: Path,
    *,
    when: datetime | None = None,
    source: str = "manual",
) -> TaskSpec:
    """Stamp `merged_at` on the spec at `spec_path` and extend `result_summary`.

    Source values in use: "manual" (operator ran `gh pr merge`), "auto" (the
    pr_review_loop merged), "reconcile" (the sweep noticed an already-merged
    PR on GitHub whose spec was unstamped).
    """
    spec = load_spec(spec_path)
    when = when or now_utc()
    suffix = f"{source}-merged at {when.isoformat(timespec='seconds')}"
    summary = spec.result_summary or ""
    new_summary = f"{summary} | {suffix}".lstrip(" |") if summary else suffix
    updated = spec.model_copy(update={"merged_at": when, "result_summary": new_summary})
    persist_spec(updated, spec_path)
    return updated


def record_manual_merge(
    task_id: str,
    *,
    life_root: Path | None = None,
    when: datetime | None = None,
) -> Path:
    """Stamp `merged_at` on the spec for `task_id` (manual-merge path).

    Call this after `gh pr merge` on a code-bearing task whose `depends_on` will
    later release children — without it, the DAG gate stays closed because
    `_ready_to_dispatch` requires `merged_at` for parents that produced code.
    Returns the path to the spec that was updated.
    """
    root = life_root if life_root is not None else Path("~/.life").expanduser()
    spec_path = find_spec_for_task(root, task_id)
    if spec_path is None:
        raise FileNotFoundError(
            f"spec.yaml for task_id={task_id!r} not found under {root}"
        )
    stamp_merged_at(spec_path, when=when, source="manual")
    return spec_path
