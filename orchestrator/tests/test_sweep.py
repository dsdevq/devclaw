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
    _popen_dispatch_cli,
    _ready_to_dispatch,
    detect_cycle,
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
    """Create a minimal ~/.life/ shape for tests.

    Note: flat-bucket `tasks/` is now resolved via `state_tasks_dir()` (see
    `paths.state_dir`), which the autouse fixture in conftest.py points at
    `tmp_path` — so `state_tasks_dir() == tmp_path/"tasks"`. We deliberately
    don't pre-create that here; helpers create it as needed.
    """
    life = tmp_path / "life"
    (life / "projects").mkdir(parents=True)
    (life / "system").mkdir(parents=True)
    return life


def write_atomic_spec(life: Path, spec: TaskSpec) -> Path:
    from orchestrator.paths import state_tasks_dir
    task_dir = state_tasks_dir() / spec.task_id
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


def write_project_bound_atomic_spec(life: Path, project: str, spec: TaskSpec) -> Path:
    """Spec at projects/<project>/tasks/<task_id>/spec.yaml — what intake produces
    when the NL intent names a project but doesn't bind to a run."""
    task_dir = life / "projects" / project / "tasks" / spec.task_id
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


def test_find_dispatched_specs_finds_project_bound_atomic(tmp_path: Path):
    """Specs at projects/<project>/tasks/<id>/spec.yaml — what `intake` produces
    when the NL intent names a project but isn't run-bound. Previously sweep
    missed these because the glob only covered run-bound spec paths."""
    life = setup_life_root(tmp_path)
    write_project_bound_atomic_spec(life, "lifekit-stack", make_spec("project-atomic-1"))
    found = find_dispatched_specs(life)
    assert len(found) == 1
    assert found[0].parent.name == "project-atomic-1"


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
    """N+1 ready atomic specs → only DISPATCH_CAP_PER_TICK get dispatched.

    Passes max_concurrent_claudes high so this test isolates the per-tick
    cap; the concurrent-claudes cap is exercised by its own tests below.
    """
    _reset_noop()
    life = setup_life_root(tmp_path)
    for i in range(DISPATCH_CAP_PER_TICK + 2):
        spec = make_spec(f"ready-{i}", status=TaskStatus.ready, dispatched_at=None)
        write_atomic_spec(life, spec)

    result = sweep_once(
        life, dispatcher=_noop_dispatcher, max_concurrent_claudes=100
    )
    assert len(result.dispatched) == DISPATCH_CAP_PER_TICK
    assert len(_noop_dispatcher.calls) == DISPATCH_CAP_PER_TICK


def test_popen_dispatch_cli_creates_dispatch_log(tmp_path: Path, monkeypatch):
    """_popen_dispatch_cli opens dispatch.log beside the spec and hands it to Popen as stdout/stderr."""
    import subprocess as _subprocess

    spec_dir = tmp_path / "tasks" / "ready-1"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "spec.yaml"
    spec_path.write_text("placeholder")

    captured: dict = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, stdout=None, stderr=None, close_fds=True):
        captured["cmd"] = cmd
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        return _FakeProc()

    monkeypatch.setattr(_subprocess, "Popen", _fake_popen)

    result = _popen_dispatch_cli(spec_path)

    log_path = spec_dir / "dispatch.log"
    assert log_path.exists()
    assert captured["stdout"] is captured["stderr"]
    assert captured["stdout"].name == str(log_path)
    assert captured["stdout"].mode == "ab"
    assert result == "pid:4242"


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


# ─── depends_on / DAG-aware dispatch ─────────────────────────────────────────


def test_sweep_dispatches_spec_with_no_deps(tmp_path: Path):
    """(a) A spec whose depends_on is empty dispatches exactly as before."""
    _reset_noop()
    life = setup_life_root(tmp_path)
    spec = make_spec(
        "no-deps", status=TaskStatus.ready, dispatched_at=None, depends_on=[]
    )
    write_atomic_spec(life, spec)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "no-deps" in result.dispatched
    assert len(_noop_dispatcher.calls) == 1


def test_sweep_dispatches_spec_with_met_dep(tmp_path: Path):
    """(b) A spec whose only dep is `done` AND merged dispatches this tick.

    The merged_at requirement applies because the dep has target_repo set
    (default in make_spec). Specs whose dep has no target_repo are covered by
    `test_sweep_dispatches_when_non_code_dep_done`.
    """
    _reset_noop()
    life = setup_life_root(tmp_path)
    dep = make_spec(
        "dep-a",
        status=TaskStatus.done,
        dispatched_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
        merged_at=datetime(2026, 5, 18, 11, 30, tzinfo=timezone.utc),
    )
    write_atomic_spec(life, dep)

    spec = make_spec(
        "needs-a",
        status=TaskStatus.ready,
        dispatched_at=None,
        depends_on=["dep-a"],
    )
    write_atomic_spec(life, spec)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "needs-a" in result.dispatched


def test_sweep_skips_spec_with_unmet_dep(tmp_path: Path):
    """(c) A spec whose dep is still in flight (dispatched-subagent) is skipped."""
    _reset_noop()
    life = setup_life_root(tmp_path)
    dep = make_spec("dep-a", status=TaskStatus.dispatched_subagent)
    write_atomic_spec(life, dep)

    spec = make_spec(
        "needs-a",
        status=TaskStatus.ready,
        dispatched_at=None,
        depends_on=["dep-a"],
    )
    spec_path = write_atomic_spec(life, spec)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "needs-a" not in result.dispatched
    assert _noop_dispatcher.calls == []

    from orchestrator.dispatch import load_spec

    reloaded = load_spec(spec_path)
    assert reloaded.status == TaskStatus.ready


def test_sweep_skips_unknown_dep_and_logs_warning_once(tmp_path: Path, caplog):
    """(d) A spec whose dep doesn't exist on disk stays in `ready` with one
    warning log line per sweep tick (not per offending spec)."""
    _reset_noop()
    life = setup_life_root(tmp_path)

    for tid in ("orphan-1", "orphan-2"):
        spec = make_spec(
            tid,
            status=TaskStatus.ready,
            dispatched_at=None,
            depends_on=["does-not-exist"],
        )
        write_atomic_spec(life, spec)

    import logging

    with caplog.at_level(logging.WARNING, logger="orchestrator.sweep"):
        result = sweep_once(life, dispatcher=_noop_dispatcher)

    assert result.dispatched == []
    assert _noop_dispatcher.calls == []
    assert result.errors == []

    unknown_warnings = [
        rec for rec in caplog.records if "unknown" in rec.getMessage().lower()
    ]
    assert len(unknown_warnings) == 1, (
        f"expected exactly one unknown-dep warning per tick, got {len(unknown_warnings)}"
    )

    from orchestrator.dispatch import load_spec
    from orchestrator.paths import state_tasks_dir

    for tid in ("orphan-1", "orphan-2"):
        reloaded = load_spec(state_tasks_dir() / tid / "spec.yaml")
        assert reloaded.status == TaskStatus.ready


def test_sweep_same_tick_race_a_dispatches_b_skipped(tmp_path: Path):
    """(e) Same-tick race: A and B both `ready`, B depends on A. A dispatches
    this tick (flipping to dispatched-subagent), B is skipped (dep not yet
    done) and remains in `ready` for the next tick."""
    _reset_noop()
    life = setup_life_root(tmp_path)

    a = make_spec("task-a", status=TaskStatus.ready, dispatched_at=None)
    b = make_spec(
        "task-b",
        status=TaskStatus.ready,
        dispatched_at=None,
        depends_on=["task-a"],
    )
    a_path = write_atomic_spec(life, a)
    b_path = write_atomic_spec(life, b)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "task-a" in result.dispatched
    assert "task-b" not in result.dispatched

    from orchestrator.dispatch import load_spec

    a_reloaded = load_spec(a_path)
    b_reloaded = load_spec(b_path)
    assert a_reloaded.status == TaskStatus.dispatched_subagent
    assert b_reloaded.status == TaskStatus.ready


# ─── _ready_to_dispatch helper unit ──────────────────────────────────────────


def test_ready_to_dispatch_returns_tuple_shape():
    """The helper returns (bool, reason: str | None) per the contract."""
    spec = make_spec("solo", status=TaskStatus.ready, depends_on=[])
    ok, reason = _ready_to_dispatch(spec, {"solo": spec})
    assert ok is True
    assert reason is None

    waiting = make_spec("waiting", status=TaskStatus.ready, depends_on=["missing"])
    ok, reason = _ready_to_dispatch(waiting, {"waiting": waiting})
    assert ok is False
    assert reason == "unknown_dep"


# ─── cycle detection ─────────────────────────────────────────────────────────


def test_detect_cycle_flags_self_dependency():
    spec = make_spec("loopy", status=TaskStatus.ready, depends_on=["loopy"])
    cycle = detect_cycle(spec, {})
    assert cycle is not None
    assert cycle[0] == "loopy" and cycle[-1] == "loopy"


def test_detect_cycle_flags_indirect_cycle():
    a = make_spec("a", status=TaskStatus.ready, depends_on=["b"])
    b = make_spec("b", status=TaskStatus.ready, depends_on=["a"])
    # Pretend `a` is already on disk; inserting `b` closes the loop.
    cycle = detect_cycle(b, {"a": a})
    assert cycle is not None
    assert set(["a", "b"]).issubset(set(cycle))


def test_detect_cycle_allows_acyclic_chain():
    a = make_spec("a", status=TaskStatus.done, depends_on=[])
    b = make_spec("b", status=TaskStatus.ready, depends_on=["a"])
    c = make_spec("c", status=TaskStatus.ready, depends_on=["b"])
    assert detect_cycle(c, {"a": a, "b": b}) is None


def test_detect_cycle_tolerates_unknown_dep():
    """An unknown dep is not a cycle — sweep handles the unknown-dep case."""
    spec = make_spec("x", status=TaskStatus.ready, depends_on=["missing"])
    assert detect_cycle(spec, {}) is None


# ─── merged_at-aware DAG gate (bug fix from 2026-05-19) ──────────────────────


def test_sweep_dispatches_when_code_dep_done_and_merged(tmp_path: Path):
    """A child whose code-bearing parent is `done` AND `merged_at` set dispatches.

    Reproduces the happy path of the merged_at gate added 2026-05-19.
    """
    _reset_noop()
    life = setup_life_root(tmp_path)
    dep = make_spec(
        "code-dep",
        status=TaskStatus.done,
        completed_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
        target_repo="dsdevq/lifekit-stack",
        merged_at=datetime(2026, 5, 18, 11, 30, tzinfo=timezone.utc),
    )
    write_atomic_spec(life, dep)

    child = make_spec(
        "needs-code",
        status=TaskStatus.ready,
        dispatched_at=None,
        depends_on=["code-dep"],
    )
    write_atomic_spec(life, child)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "needs-code" in result.dispatched


def test_sweep_skips_when_code_dep_done_but_not_merged(tmp_path: Path):
    """A child whose code-bearing parent is `done` but `merged_at` is null is skipped.

    This is the bug from 2026-05-19: parent's runner finished and opened a PR,
    but the PR isn't on `main`, so the child's runner can't reference the
    parent's code. Gate must keep the child waiting.
    """
    _reset_noop()
    life = setup_life_root(tmp_path)
    dep = make_spec(
        "code-dep",
        status=TaskStatus.done,
        completed_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
        target_repo="dsdevq/lifekit-stack",
        merged_at=None,
    )
    write_atomic_spec(life, dep)

    child = make_spec(
        "needs-code",
        status=TaskStatus.ready,
        dispatched_at=None,
        depends_on=["code-dep"],
    )
    spec_path = write_atomic_spec(life, child)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "needs-code" not in result.dispatched
    assert _noop_dispatcher.calls == []

    from orchestrator.dispatch import load_spec

    assert load_spec(spec_path).status == TaskStatus.ready

    ok, reason = _ready_to_dispatch(
        load_spec(spec_path),
        {"code-dep": dep, "needs-code": load_spec(spec_path)},
    )
    assert ok is False
    assert reason == "dep_not_merged"


def test_sweep_dispatches_when_non_code_dep_done(tmp_path: Path):
    """A child whose research/chore parent (no target_repo) is `done` dispatches
    without a merged_at requirement — those parents produce only artifacts."""
    _reset_noop()
    life = setup_life_root(tmp_path)
    dep = make_spec(
        "research-dep",
        kind=TaskKind.research,
        status=TaskStatus.done,
        completed_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
        target_repo=None,
        merged_at=None,
    )
    write_atomic_spec(life, dep)

    child = make_spec(
        "needs-research",
        status=TaskStatus.ready,
        dispatched_at=None,
        depends_on=["research-dep"],
    )
    write_atomic_spec(life, child)

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "needs-research" in result.dispatched


def test_record_manual_merge_stamps_merged_at(tmp_path: Path):
    """`record_manual_merge(task_id)` finds the spec by id and stamps merged_at."""
    from orchestrator.dispatch import load_spec, record_manual_merge

    life = setup_life_root(tmp_path)
    spec = make_spec(
        "manually-merged",
        status=TaskStatus.done,
        completed_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
        target_repo="dsdevq/devclaw",
        merged_at=None,
    )
    spec_path = write_atomic_spec(life, spec)

    when = datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc)
    returned = record_manual_merge("manually-merged", life_root=life, when=when)

    assert returned == spec_path
    reloaded = load_spec(spec_path)
    assert reloaded.merged_at == when
    assert "manual-merged" in (reloaded.result_summary or "")


def test_sweep_reconciles_merge_via_gh(tmp_path: Path):
    """Reconciliation pass: `gh pr view` says the PR is merged → spec gets stamped."""
    import json as _json
    import subprocess

    _reset_noop()
    life = setup_life_root(tmp_path)
    spec = make_spec(
        "reconcile-me",
        status=TaskStatus.done,
        completed_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
        target_repo="dsdevq/devclaw",
        merged_at=None,
        result_summary="PR: https://github.com/dsdevq/devclaw/pull/99",
    )
    spec_path = write_atomic_spec(life, spec)

    gh_calls: list[list[str]] = []

    def fake_gh(args: list[str]) -> subprocess.CompletedProcess:
        gh_calls.append(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=_json.dumps({"mergedAt": "2026-05-19T10:00:00Z"}),
            stderr="",
        )

    result = sweep_once(life, dispatcher=_noop_dispatcher, gh=fake_gh)

    assert "reconcile-me" in result.reconciled_merges
    assert any("pr" in c and "view" in c and "99" in c for c in gh_calls)

    from orchestrator.dispatch import load_spec

    reloaded = load_spec(spec_path)
    assert reloaded.merged_at is not None
    assert reloaded.merged_at == datetime(2026, 5, 19, 10, 0, tzinfo=timezone.utc)
    assert "reconcile-merged" in (reloaded.result_summary or "")


# ─── max_concurrent_claudes global cap ───────────────────────────────────────


def test_sweep_max_concurrent_claudes_two_dispatches_two_queues_one(tmp_path: Path):
    """cap=2 + three back-to-back ready specs → 2 dispatched, 1 left in ready.

    The deferred spec stays at status=ready and is NOT marked blocked, so the
    next sweep tick can pick it up once capacity frees.
    """
    _reset_noop()
    life = setup_life_root(tmp_path)
    spec_paths: list[Path] = []
    for i in range(3):
        spec = make_spec(f"ready-{i}", status=TaskStatus.ready, dispatched_at=None)
        spec_paths.append(write_atomic_spec(life, spec))

    result = sweep_once(
        life, dispatcher=_noop_dispatcher, max_concurrent_claudes=2
    )
    assert len(result.dispatched) == 2
    assert len(_noop_dispatcher.calls) == 2

    from orchestrator.dispatch import load_spec

    statuses = [load_spec(p).status for p in spec_paths]
    assert statuses.count(TaskStatus.dispatched_subagent) == 2
    assert statuses.count(TaskStatus.ready) == 1
    # The deferred one is NOT blocked — it must remain dispatchable next tick.
    assert TaskStatus.blocked not in statuses


def test_sweep_max_concurrent_claudes_default_is_one(tmp_path: Path):
    """Default cap=1 → only one ready spec dispatches per tick even when many wait."""
    _reset_noop()
    life = setup_life_root(tmp_path)
    spec_paths: list[Path] = []
    for i in range(3):
        spec = make_spec(f"ready-{i}", status=TaskStatus.ready, dispatched_at=None)
        spec_paths.append(write_atomic_spec(life, spec))

    # No max_concurrent_claudes kwarg → default applies. Asserts the default
    # value is 1, not just that cap-of-1 behaves correctly.
    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert len(result.dispatched) == 1
    assert len(_noop_dispatcher.calls) == 1

    from orchestrator.dispatch import load_spec

    statuses = [load_spec(p).status for p in spec_paths]
    assert statuses.count(TaskStatus.dispatched_subagent) == 1
    assert statuses.count(TaskStatus.ready) == 2
    assert TaskStatus.blocked not in statuses


def test_sweep_queued_spec_dispatches_after_in_flight_completes(tmp_path: Path):
    """Queued spec dispatches on the next sweep tick once capacity frees.

    Models the realistic flow: tick 1 dispatches A (cap=1) and leaves B queued.
    Between ticks, A's runner writes result.json. Tick 2 reaps A (freeing the
    slot) and then dispatches B in the same tick (reap pass runs before
    dispatch pass).
    """
    _reset_noop()
    life = setup_life_root(tmp_path)
    a = make_spec("task-a", status=TaskStatus.ready, dispatched_at=None)
    b = make_spec("task-b", status=TaskStatus.ready, dispatched_at=None)
    a_path = write_atomic_spec(life, a)
    b_path = write_atomic_spec(life, b)
    paths_by_id = {"task-a": a_path, "task-b": b_path}

    # Tick 1: cap=1 — exactly one dispatches; the other stays in ready.
    # find_all_specs uses glob order which is filesystem-dependent, so we
    # accept whichever spec wins this tick and follow up on the loser.
    result1 = sweep_once(life, dispatcher=_noop_dispatcher, max_concurrent_claudes=1)
    assert len(result1.dispatched) == 1
    assert len(_noop_dispatcher.calls) == 1

    first = result1.dispatched[0]
    second = "task-b" if first == "task-a" else "task-a"
    first_path = paths_by_id[first]
    second_path = paths_by_id[second]

    from orchestrator.dispatch import load_spec

    assert load_spec(first_path).status == TaskStatus.dispatched_subagent
    assert load_spec(second_path).status == TaskStatus.ready

    # Between ticks: the in-flight one's runner writes result.json.
    (first_path.parent / "result.json").write_text(
        json.dumps(
            {
                "task_id": first,
                "status": "done",
                "completed_at": "2026-05-18T12:25:00+00:00",
                "notes": "done",
            }
        )
    )

    # Tick 2: reap the completed one (frees the slot), then dispatch the queued one.
    result2 = sweep_once(life, dispatcher=_noop_dispatcher, max_concurrent_claudes=1)
    assert first in result2.reaped
    assert result2.dispatched == [second]
    assert len(_noop_dispatcher.calls) == 2

    assert load_spec(first_path).status == TaskStatus.done
    assert load_spec(second_path).status == TaskStatus.dispatched_subagent
