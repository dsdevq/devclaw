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
