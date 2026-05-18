"""Tests for the cron-fired sweep — reap + watchdog over all in-flight specs.

Uses tmp_path to set up a fake ~/.life/ tree; no real disk paths touched.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orchestrator.dispatch import (
    WATCHDOG_GRACE_SECONDS,
    persist_spec,
)
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)
from orchestrator.sweep import (
    DISPATCH_CAP_PER_TICK,
    REAP_CAP_PER_TICK,
    WATCHDOG_CAP_PER_TICK,
    find_all_specs,
    find_dispatched_specs,
    is_killswitch_set,
    sweep_once,
)


def _noop_dispatcher(spec_path):
    """Test dispatcher that records the call but does NOT subprocess.Popen anything."""
    _noop_dispatcher.calls.append(spec_path)  # type: ignore[attr-defined]
    return f"pid:0"


_noop_dispatcher.calls = []  # type: ignore[attr-defined]


def _reset_noop():
    _noop_dispatcher.calls = []  # type: ignore[attr-defined]


def make_spec(task_id: str, **overrides) -> TaskSpec:
    base = dict(
        task_id=task_id,
        created_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="tests"),
        verbatim_intent="test sweep",
        kind=TaskKind.code,
        target_repo="dsdevq/lifekit-stack",
        acceptance_criteria=["x"],
        budget=Budget(max_runtime_seconds=1800),
        status=TaskStatus.dispatched_subagent,
        dispatched_at=datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return TaskSpec(**base)


def setup_life_root(tmp_path: Path) -> Path:
    """Create a minimal ~/.life/ shape for tests."""
    life = tmp_path / "life"
    (life / "tasks").mkdir(parents=True)
    (life / "system").mkdir(parents=True)
    return life


def write_atomic_spec(life: Path, spec: TaskSpec) -> Path:
    task_dir = life / "tasks" / spec.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    spec_path = task_dir / "spec.yaml"
    persist_spec(spec, spec_path)
    return spec_path


def write_run_bound_spec(life: Path, project: str, run: str, spec: TaskSpec) -> Path:
    task_dir = life / "projects" / project / "runs" / run / "tasks" / spec.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    spec_path = task_dir / "spec.yaml"
    persist_spec(spec, spec_path)
    return spec_path


# ─── find_dispatched_specs ───────────────────────────────────────────────────


def test_find_dispatched_specs_finds_atomic(tmp_path: Path):
    life = setup_life_root(tmp_path)
    write_atomic_spec(life, make_spec("atomic-1"))
    found = find_dispatched_specs(life)
    assert len(found) == 1
    assert found[0].parent.name == "atomic-1"


def test_find_dispatched_specs_finds_run_bound(tmp_path: Path):
    life = setup_life_root(tmp_path)
    write_run_bound_spec(life, "lifekit-stack", "run-1", make_spec("run-bound-1"))
    found = find_dispatched_specs(life)
    assert len(found) == 1
    assert found[0].parent.name == "run-bound-1"


def test_find_dispatched_specs_finds_both(tmp_path: Path):
    life = setup_life_root(tmp_path)
    write_atomic_spec(life, make_spec("atomic-1"))
    write_run_bound_spec(life, "lifekit-stack", "run-1", make_spec("run-bound-1"))
    found = find_dispatched_specs(life)
    assert len(found) == 2


def test_find_dispatched_specs_empty_when_no_tasks(tmp_path: Path):
    life = setup_life_root(tmp_path)
    assert find_dispatched_specs(life) == []


# ─── killswitch ──────────────────────────────────────────────────────────────


def test_killswitch_detected_when_file_exists(tmp_path: Path):
    life = setup_life_root(tmp_path)
    (life / "system" / "cron-paused").touch()
    assert is_killswitch_set(life) is True


def test_killswitch_not_detected_normally(tmp_path: Path):
    life = setup_life_root(tmp_path)
    assert is_killswitch_set(life) is False


def test_sweep_does_nothing_when_killswitch_set(tmp_path: Path):
    life = setup_life_root(tmp_path)
    (life / "system" / "cron-paused").touch()

    spec = make_spec(
        "ghost-1",
        dispatched_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        watchdog_deadline=datetime(2026, 5, 18, 10, 35, tzinfo=timezone.utc),
    )
    spec_path = write_atomic_spec(life, spec)

    result = sweep_once(life)
    assert result.skipped_killswitch is True
    assert result.reaped == []
    assert result.ghosted == []

    # spec unchanged on disk
    from orchestrator.dispatch import load_spec

    reloaded = load_spec(spec_path)
    assert reloaded.status == TaskStatus.dispatched_subagent


# ─── reap pass ───────────────────────────────────────────────────────────────


def test_sweep_reaps_when_result_json_present(tmp_path: Path):
    life = setup_life_root(tmp_path)
    spec = make_spec("reap-me")
    spec_path = write_atomic_spec(life, spec)

    # runner finished but didn't flip the spec
    (spec_path.parent / "result.json").write_text(
        json.dumps(
            {
                "task_id": "reap-me",
                "status": "done",
                "completed_at": "2026-05-18T12:25:00+00:00",
                "pr_url": "https://example.test/pull/1",
                "branch": "kit/reap-me",
                "notes": "done within budget",
            }
        )
    )

    result = sweep_once(life)
    assert "reap-me" in result.reaped
    assert result.ghosted == []

    from orchestrator.dispatch import load_spec

    reloaded = load_spec(spec_path)
    assert reloaded.status == TaskStatus.done


def test_sweep_reaps_findings_md_for_research(tmp_path: Path):
    life = setup_life_root(tmp_path)
    spec = make_spec("research-me", kind=TaskKind.research)
    spec_path = write_atomic_spec(life, spec)

    (spec_path.parent / "findings.md").write_text("# Findings on X\n\ndetails")

    result = sweep_once(life)
    assert "research-me" in result.reaped

    from orchestrator.dispatch import load_spec

    reloaded = load_spec(spec_path)
    assert reloaded.status == TaskStatus.done


# ─── watchdog pass ───────────────────────────────────────────────────────────


def test_sweep_watchdogs_ghosted_spec(tmp_path: Path):
    life = setup_life_root(tmp_path)
    # dispatched 2 hours ago, budget 1800s, deadline already past
    long_ago = datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc)
    deadline = long_ago + timedelta(seconds=1800 + WATCHDOG_GRACE_SECONDS)
    spec = make_spec("ghost-1", dispatched_at=long_ago, watchdog_deadline=deadline)
    spec_path = write_atomic_spec(life, spec)

    # No artifact on disk

    # Need to make is_ghosted return True — set deadline well in past via fixed deadline above + rely on real now()
    # (the test runs after 2026-05-18T10:35Z; this is fine since dispatch test fixtures use that month)
    # Actually the real now() is whatever today is when the test runs; if test runs in May 2026 the spec's deadline (2026-05-18T10:35Z) is in the past.
    # For safety, push the deadline to a guaranteed past value.
    spec_with_past_deadline = spec.model_copy(
        update={"watchdog_deadline": datetime(2020, 1, 1, tzinfo=timezone.utc)}
    )
    persist_spec(spec_with_past_deadline, spec_path)

    result = sweep_once(life)
    assert "ghost-1" in result.ghosted

    from orchestrator.dispatch import load_spec

    reloaded = load_spec(spec_path)
    assert reloaded.status == TaskStatus.blocked
    assert "runner_silent_past_deadline" in (reloaded.result_summary or "")


def test_sweep_does_not_watchdog_spec_with_result_json(tmp_path: Path):
    """Reap pass runs before watchdog — a spec with result.json should reap, not ghost."""
    life = setup_life_root(tmp_path)
    spec = make_spec(
        "late-completer",
        watchdog_deadline=datetime(2020, 1, 1, tzinfo=timezone.utc),  # past
    )
    spec_path = write_atomic_spec(life, spec)
    (spec_path.parent / "result.json").write_text(
        json.dumps(
            {
                "task_id": "late-completer",
                "status": "done",
                "completed_at": "2026-05-18T12:25:00+00:00",
                "pr_url": "https://example.test/pull/2",
                "branch": "kit/late-completer",
                "notes": "finished, just late",
            }
        )
    )

    result = sweep_once(life)
    assert "late-completer" in result.reaped
    assert "late-completer" not in result.ghosted


def test_sweep_caps_are_enforced(tmp_path: Path):
    """If more than CAP specs need reaping, only CAP get reaped this tick."""
    life = setup_life_root(tmp_path)
    for i in range(REAP_CAP_PER_TICK + 3):
        spec = make_spec(f"reap-{i}")
        spec_path = write_atomic_spec(life, spec)
        (spec_path.parent / "result.json").write_text(
            json.dumps(
                {
                    "task_id": f"reap-{i}",
                    "status": "done",
                    "completed_at": "2026-05-18T12:25:00+00:00",
                    "notes": "done",
                }
            )
        )

    result = sweep_once(life)
    assert len(result.reaped) == REAP_CAP_PER_TICK


def test_sweep_empty_life_returns_clean(tmp_path: Path):
    life = setup_life_root(tmp_path)
    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert result.scanned == 0
    assert result.reaped == []
    assert result.ghosted == []
    assert result.dispatched == []
    assert result.errors == []
    assert result.skipped_killswitch is False


# ─── dispatch pass ───────────────────────────────────────────────────────────


def test_sweep_dispatches_ready_atomic_spec(tmp_path: Path):
    """A spec at status: ready (atomic, no run binding) gets Popen'd by the sweep."""
    _reset_noop()
    life = setup_life_root(tmp_path)
    spec = make_spec("ready-1", status=TaskStatus.ready, dispatched_at=None)
    spec_path = write_atomic_spec(life, spec)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "ready-1" in result.dispatched
    assert len(_noop_dispatcher.calls) == 1
    assert _noop_dispatcher.calls[0] == spec_path

    # spec.yaml on disk is now status: dispatched-subagent with a watchdog_deadline
    from orchestrator.dispatch import load_spec

    reloaded = load_spec(spec_path)
    assert reloaded.status == TaskStatus.dispatched_subagent
    assert reloaded.watchdog_deadline is not None
    assert reloaded.dispatched_at is not None


def test_sweep_does_not_dispatch_run_bound_specs(tmp_path: Path):
    """Run-bound specs (run field set) are dispatched by the supervisor, NOT the sweep."""
    _reset_noop()
    life = setup_life_root(tmp_path)
    spec = make_spec(
        "run-bound-1",
        status=TaskStatus.ready,
        run="some-run-id",
        run_node="node-x",
    )
    write_run_bound_spec(life, "lifekit-stack", "some-run-id", spec)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "run-bound-1" not in result.dispatched
    assert _noop_dispatcher.calls == []


def test_sweep_dispatch_caps_at_per_tick_limit(tmp_path: Path):
    """N+1 ready atomic specs → only DISPATCH_CAP_PER_TICK get dispatched."""
    _reset_noop()
    life = setup_life_root(tmp_path)
    for i in range(DISPATCH_CAP_PER_TICK + 2):
        spec = make_spec(f"ready-{i}", status=TaskStatus.ready, dispatched_at=None)
        write_atomic_spec(life, spec)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert len(result.dispatched) == DISPATCH_CAP_PER_TICK
    assert len(_noop_dispatcher.calls) == DISPATCH_CAP_PER_TICK


def test_sweep_dispatch_skipped_when_killswitch_set(tmp_path: Path):
    _reset_noop()
    life = setup_life_root(tmp_path)
    (life / "system" / "cron-paused").touch()
    spec = make_spec("ready-1", status=TaskStatus.ready, dispatched_at=None)
    write_atomic_spec(life, spec)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert result.skipped_killswitch is True
    assert result.dispatched == []
    assert _noop_dispatcher.calls == []
