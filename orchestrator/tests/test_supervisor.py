"""Tests for the supervisor — multi-task DAG orchestration.

Uses synthetic dag.yaml on tmp_path. Dispatch is mocked (no real subprocesses, no Claude calls). Announce captured to a list.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.dispatch import persist_spec
from orchestrator.state.models import (
    Budget,
    DagNode,
    Evidence,
    RequesterRoute,
    Result,
    Run,
    RunnerStatus,
    RunStatus,
    TaskKind,
    TaskSpec,
    TaskStatus,
    VerifierStatus,
)
from orchestrator.supervisor import (
    DISPATCH_CAP_PER_TICK,
    all_verified_done,
    deps_satisfied,
    find_dispatched_nodes,
    find_ready_nodes,
    find_verification_failed_nodes,
    is_killswitch_set,
    load_run,
    persist_run,
    tick_run,
)


# ─── fixtures ────────────────────────────────────────────────────────────────


def make_node(node_id: str, **overrides) -> DagNode:
    base = dict(
        id=node_id,
        title=f"node {node_id}",
        kind=TaskKind.code,
        depends_on=[],
        budget_seconds=600,
        target_repo="dsdevq/lifekit-stack",
        target_branch="main",
        acceptance_criteria=[],
        runner_status=RunnerStatus.pending,
        verifier_status=VerifierStatus.pending,
    )
    base.update(overrides)
    return DagNode(**base)


def make_run(tasks: list[DagNode], **overrides) -> Run:
    base = dict(
        run_id="2026-05-18-test-run",
        project="lifekit-stack",
        proposal=None,
        created_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        status=RunStatus.in_progress,
        tasks=tasks,
    )
    base.update(overrides)
    return Run(**base)


def setup_run_on_disk(tmp_path: Path, run: Run) -> tuple[Path, Path]:
    """Write a dag.yaml under a synthetic ~/.life/projects/<p>/runs/<r>/."""
    life = tmp_path / "life"
    (life / "system").mkdir(parents=True)
    run_dir = life / "projects" / run.project / "runs" / run.run_id
    run_dir.mkdir(parents=True)
    dag_path = run_dir / "dag.yaml"
    persist_run(run, dag_path)
    return life, dag_path


# ─── pure helpers ────────────────────────────────────────────────────────────


def test_deps_satisfied_empty_deps():
    n = make_node("a", depends_on=[])
    assert deps_satisfied(n, {}) is True


def test_deps_satisfied_when_all_verified_done():
    a = make_node("a", runner_status=RunnerStatus.verified_done)
    b = make_node("b", depends_on=["a"])
    assert deps_satisfied(b, {"a": a, "b": b}) is True


def test_deps_satisfied_false_when_dep_not_done():
    a = make_node("a", runner_status=RunnerStatus.dispatched)
    b = make_node("b", depends_on=["a"])
    assert deps_satisfied(b, {"a": a, "b": b}) is False


def test_find_ready_nodes_returns_only_pending_with_satisfied_deps():
    a = make_node("a", runner_status=RunnerStatus.verified_done)
    b = make_node("b", depends_on=["a"], runner_status=RunnerStatus.pending)
    c = make_node("c", depends_on=["b"], runner_status=RunnerStatus.pending)
    run = make_run([a, b, c])
    ready = find_ready_nodes(run)
    assert [n.id for n in ready] == ["b"]


def test_all_verified_done():
    a = make_node("a", runner_status=RunnerStatus.verified_done)
    b = make_node("b", runner_status=RunnerStatus.verified_done)
    assert all_verified_done(make_run([a, b])) is True

    c = make_node("c", runner_status=RunnerStatus.pending)
    assert all_verified_done(make_run([a, c])) is False


# ─── tick: dispatch pass ─────────────────────────────────────────────────────


def test_tick_dispatches_ready_root_nodes(tmp_path: Path):
    a = make_node("a", depends_on=[])
    b = make_node("b", depends_on=["a"])
    run = make_run([a, b])
    life, dag_path = setup_run_on_disk(tmp_path, run)

    dispatched_specs: list[Path] = []

    def mock_dispatcher(spec_path: Path) -> str:
        dispatched_specs.append(spec_path)
        return f"pid:{len(dispatched_specs) * 100}"

    result = tick_run(
        dag_path,
        life_root=life,
        dispatcher=mock_dispatcher,
    )

    assert result.dispatched == ["a"]
    assert len(dispatched_specs) == 1
    # dag.yaml flipped on disk
    reloaded = load_run(dag_path)
    assert reloaded.tasks[0].runner_status == RunnerStatus.dispatched
    assert reloaded.tasks[0].spec_path is not None
    assert reloaded.tasks[1].runner_status == RunnerStatus.pending


def test_tick_caps_dispatches_at_per_tick_limit(tmp_path: Path):
    """5 root nodes, only DISPATCH_CAP_PER_TICK should fire in one tick."""
    nodes = [make_node(f"n{i}", depends_on=[]) for i in range(5)]
    run = make_run(nodes)
    life, dag_path = setup_run_on_disk(tmp_path, run)

    def mock_dispatcher(spec_path: Path) -> str:
        return "pid:1"

    result = tick_run(dag_path, life_root=life, dispatcher=mock_dispatcher)
    assert len(result.dispatched) == DISPATCH_CAP_PER_TICK


def test_tick_does_nothing_when_killswitch_set(tmp_path: Path):
    a = make_node("a", depends_on=[])
    run = make_run([a])
    life, dag_path = setup_run_on_disk(tmp_path, run)
    (life / "system" / "cron-paused").touch()

    def mock_dispatcher(spec_path: Path) -> str:
        return "pid:1"

    result = tick_run(dag_path, life_root=life, dispatcher=mock_dispatcher)
    assert result.skipped_killswitch is True
    assert result.dispatched == []


# ─── tick: reconcile pass ────────────────────────────────────────────────────


def test_tick_reconciles_done_spec_to_verified_done(tmp_path: Path):
    """After dispatch, when the spec.yaml shows status=done, dag flips to verified_done."""
    a = make_node("a", runner_status=RunnerStatus.dispatched)
    run = make_run([a])
    life, dag_path = setup_run_on_disk(tmp_path, run)

    # Simulate a completed spec on disk
    spec_dir = dag_path.parent / "tasks" / "spec-for-a"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "spec.yaml"
    spec = TaskSpec(
        task_id="spec-for-a",
        created_at=datetime(2026, 5, 18, 10, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="t"),
        verbatim_intent="x",
        kind=TaskKind.code,
        target_repo="dsdevq/x",
        budget=Budget(max_runtime_seconds=600),
        status=TaskStatus.done,
        completed_at=datetime(2026, 5, 18, 11, tzinfo=timezone.utc),
        result_summary="PR: https://github.com/x/x/pull/1",
    )
    persist_spec(spec, spec_path)

    # Wire spec_path into the dag node
    a.spec_path = spec_path
    run.tasks[0] = a
    persist_run(run, dag_path)

    def mock_dispatcher(spec_path: Path) -> str:
        return "pid:1"

    result = tick_run(dag_path, life_root=life, dispatcher=mock_dispatcher)
    assert "a" in result.reconciled

    reloaded = load_run(dag_path)
    assert reloaded.tasks[0].runner_status == RunnerStatus.verified_done


def test_tick_reconciles_blocked_spec_to_verification_failed(tmp_path: Path):
    a = make_node("a", runner_status=RunnerStatus.dispatched)
    run = make_run([a])
    life, dag_path = setup_run_on_disk(tmp_path, run)

    spec_dir = dag_path.parent / "tasks" / "spec-for-a"
    spec_dir.mkdir(parents=True)
    spec_path = spec_dir / "spec.yaml"
    spec = TaskSpec(
        task_id="spec-for-a",
        created_at=datetime(2026, 5, 18, 10, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="t"),
        verbatim_intent="x",
        kind=TaskKind.code,
        target_repo="dsdevq/x",
        budget=Budget(max_runtime_seconds=600),
        status=TaskStatus.blocked,
        completed_at=datetime(2026, 5, 18, 11, tzinfo=timezone.utc),
        result_summary="tests_failed: 3 unit tests red",
    )
    persist_spec(spec, spec_path)
    a.spec_path = spec_path
    run.tasks[0] = a
    persist_run(run, dag_path)

    def mock_dispatcher(spec_path: Path) -> str:
        return "pid:1"

    announces: list[tuple[str, str]] = []

    result = tick_run(
        dag_path,
        life_root=life,
        dispatcher=mock_dispatcher,
        announce=lambda c, m: announces.append((c, m)),
    )

    # First fail → retry (no escalation yet) + immediate re-dispatch in the same tick
    assert "a" in result.failed
    assert "a" in result.retried
    assert "a" in result.dispatched  # same tick picks the now-pending node back up
    assert result.escalated == []
    reloaded = load_run(dag_path)
    assert reloaded.tasks[0].runner_status == RunnerStatus.dispatched
    assert reloaded.tasks[0].retried is True


# ─── tick: retry-or-escalate ─────────────────────────────────────────────────


def test_second_failure_after_retry_escalates(tmp_path: Path):
    """If a node has retried=True and verification_failed again, escalate."""
    a = make_node(
        "a",
        runner_status=RunnerStatus.verification_failed,
        retried=True,
        evidence=Evidence(verification_failure_reason="tests_failed: still red"),
    )
    run = make_run([a])
    life, dag_path = setup_run_on_disk(tmp_path, run)

    announces: list[tuple[str, str]] = []

    def mock_dispatcher(spec_path: Path) -> str:
        return "pid:1"

    result = tick_run(
        dag_path,
        life_root=life,
        dispatcher=mock_dispatcher,
        announce=lambda c, m: announces.append((c, m)),
    )

    assert "a" in result.escalated
    reloaded = load_run(dag_path)
    assert reloaded.status == RunStatus.blocked
    assert len(announces) == 1
    assert "Run blocked" in announces[0][1]


def test_non_resolvable_failure_escalates_immediately(tmp_path: Path):
    """Failure reason not on the resolvable list → escalate on first failure."""
    a = make_node(
        "a",
        runner_status=RunnerStatus.verification_failed,
        retried=False,
        evidence=Evidence(verification_failure_reason="auth_failed: gh token expired"),
    )
    run = make_run([a])
    life, dag_path = setup_run_on_disk(tmp_path, run)

    announces: list[tuple[str, str]] = []

    def mock_dispatcher(spec_path: Path) -> str:
        return "pid:1"

    result = tick_run(
        dag_path,
        life_root=life,
        dispatcher=mock_dispatcher,
        announce=lambda c, m: announces.append((c, m)),
    )

    assert "a" in result.escalated
    reloaded = load_run(dag_path)
    assert reloaded.status == RunStatus.blocked


# ─── tick: run-complete ──────────────────────────────────────────────────────


def test_tick_marks_run_complete_when_all_verified_done(tmp_path: Path):
    a = make_node("a", runner_status=RunnerStatus.verified_done)
    b = make_node("b", depends_on=["a"], runner_status=RunnerStatus.verified_done)
    run = make_run([a, b])
    life, dag_path = setup_run_on_disk(tmp_path, run)

    announces: list[tuple[str, str]] = []

    def mock_dispatcher(spec_path: Path) -> str:
        return "pid:1"

    result = tick_run(
        dag_path,
        life_root=life,
        dispatcher=mock_dispatcher,
        announce=lambda c, m: announces.append((c, m)),
    )

    assert result.completed is True
    reloaded = load_run(dag_path)
    assert reloaded.status == RunStatus.completed
    assert any("Run complete" in m for _, m in announces)


# ─── tick: end-to-end across multiple heartbeats ─────────────────────────────


def test_run_progresses_through_multiple_ticks(tmp_path: Path):
    """Simulate two ticks: first dispatches root, second reconciles, third dispatches dep."""
    a = make_node("a", depends_on=[])
    b = make_node("b", depends_on=["a"])
    run = make_run([a, b])
    life, dag_path = setup_run_on_disk(tmp_path, run)

    def mock_dispatcher(spec_path: Path) -> str:
        return "pid:1"

    # Tick 1: a dispatched, b still pending
    r1 = tick_run(dag_path, life_root=life, dispatcher=mock_dispatcher)
    assert "a" in r1.dispatched

    # Simulate a completing successfully — write a done spec at the node's spec_path
    reloaded = load_run(dag_path)
    a_spec_path = Path(str(reloaded.tasks[0].spec_path)).expanduser()
    spec = TaskSpec(
        task_id=a_spec_path.parent.name,
        created_at=datetime(2026, 5, 18, 10, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="t"),
        verbatim_intent="x",
        kind=TaskKind.code,
        target_repo="dsdevq/x",
        budget=Budget(max_runtime_seconds=600),
        status=TaskStatus.done,
        completed_at=datetime(2026, 5, 18, 11, tzinfo=timezone.utc),
        result_summary="done",
    )
    persist_spec(spec, a_spec_path)

    # Tick 2: a should reconcile to verified_done, b should dispatch
    r2 = tick_run(dag_path, life_root=life, dispatcher=mock_dispatcher)
    assert "a" in r2.reconciled
    assert "b" in r2.dispatched

    reloaded = load_run(dag_path)
    assert reloaded.tasks[0].runner_status == RunnerStatus.verified_done
    assert reloaded.tasks[1].runner_status == RunnerStatus.dispatched
