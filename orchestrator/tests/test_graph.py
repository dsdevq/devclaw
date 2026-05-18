"""End-to-end graph wiring tests using the deterministic stub runner.

Validates that the LangGraph wiring (code_task → verify → complete | retry | escalate) routes correctly without invoking the real Claude CLI.
"""

from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.graph import build_task_graph
from orchestrator.runners.code_task import code_task_node_stub
from orchestrator.state.models import (
    Budget,
    GraphState,
    RequesterRoute,
    Result,
    TaskKind,
    TaskSpec,
    TaskStatus,
)


def make_spec(**overrides) -> TaskSpec:
    base = dict(
        task_id="2026-05-18-graph-test-aaaa",
        created_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="tests"),
        verbatim_intent="test the graph wiring",
        kind=TaskKind.code,
        target_repo="dsdevq/lifekit-stack",
        acceptance_criteria=[],
        budget=Budget(max_runtime_seconds=600),
        status=TaskStatus.ready,
    )
    base.update(overrides)
    return TaskSpec(**base)


def test_happy_path_completes_with_stub_runner():
    graph = build_task_graph(code_runner=code_task_node_stub)
    initial = GraphState(spec=make_spec())
    final = graph.invoke(initial)
    # Compiled-graph .invoke returns the AddableValuesDict shape; unchanged-default keys may be absent.
    assert final["result"] is not None
    assert final["result"].status == "done"
    assert final["spec"].status == TaskStatus.done
    assert final.get("error") is None


def test_blocked_runner_routes_to_escalate_after_retry():
    """A runner that blocks with non-resolvable reason → escalate (no retry)."""

    def blocking_runner(state: GraphState) -> dict:
        return {
            "result": Result(
                task_id=state.spec.task_id,
                status="blocked",
                completed_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
                blocker="auth_failed",  # NOT in internally-resolvable list
                notes="cannot auth to GitHub",
            )
        }

    graph = build_task_graph(code_runner=blocking_runner)
    initial = GraphState(spec=make_spec())
    final = graph.invoke(initial)
    assert final["result"].status == "blocked"
    assert final["spec"].status == TaskStatus.blocked
    assert final["error"] == "auth_failed"


def test_resolvable_failure_triggers_one_retry_then_succeeds():
    """First call fails with tests_failed; retry succeeds → complete."""
    call_count = {"n": 0}

    def flaky_runner(state: GraphState) -> dict:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return {
                "result": Result(
                    task_id=state.spec.task_id,
                    status="blocked",
                    completed_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
                    blocker="tests_failed",
                    notes="3 tests failed on first attempt",
                )
            }
        return {
            "result": Result(
                task_id=state.spec.task_id,
                status="done",
                completed_at=datetime(2026, 5, 18, 11, 30, tzinfo=timezone.utc),
                pr_url=f"https://example.test/{state.spec.target_repo}/pull/1",
                notes="green on retry",
            )
        }

    graph = build_task_graph(code_runner=flaky_runner)
    initial = GraphState(spec=make_spec())
    final = graph.invoke(initial)
    assert call_count["n"] == 2
    assert final["result"].status == "done"
    assert final["spec"].status == TaskStatus.done


def test_resolvable_failure_twice_escalates():
    """Two tests_failed in a row → escalate (one retry max)."""

    def persistently_failing_runner(state: GraphState) -> dict:
        return {
            "result": Result(
                task_id=state.spec.task_id,
                status="blocked",
                completed_at=datetime(2026, 5, 18, 11, 0, tzinfo=timezone.utc),
                blocker="tests_failed",
                notes="still red",
            )
        }

    graph = build_task_graph(code_runner=persistently_failing_runner)
    initial = GraphState(spec=make_spec())
    final = graph.invoke(initial)
    assert final["spec"].status == TaskStatus.blocked
    assert final["error"] == "tests_failed"
