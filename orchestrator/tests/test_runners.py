"""Unit tests for runner kind routing and runner-specific behaviour.

Uses stubs throughout — no Claude calls. Live invocation is validated by the smoke test in dsdevq/devclaw#4 (code_task) and follow-up smoke tests for the others.
"""

from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.graph import build_task_graph, route_by_kind
from orchestrator.runners import (
    code_task_node_stub,
    propose_change_node_stub,
    research_task_node_stub,
)
from orchestrator.runners._subprocess import _parse_last_json_line
from orchestrator.state.models import (
    Budget,
    GraphState,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)


def make_spec(**overrides) -> TaskSpec:
    base = dict(
        task_id="2026-05-18-runner-test-aaaa",
        created_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="tests"),
        verbatim_intent="test",
        kind=TaskKind.code,
        target_repo="dsdevq/lifekit-stack",
        acceptance_criteria=[],
        budget=Budget(max_runtime_seconds=600),
        status=TaskStatus.ready,
    )
    base.update(overrides)
    return TaskSpec(**base)


# ─── route_by_kind ───────────────────────────────────────────────────────────


def test_route_by_kind_code():
    state = GraphState(spec=make_spec(kind=TaskKind.code))
    assert route_by_kind(state) == "code_task"


def test_route_by_kind_research():
    state = GraphState(spec=make_spec(kind=TaskKind.research, target_repo=None))
    assert route_by_kind(state) == "research_task"


def test_route_by_kind_chore():
    state = GraphState(spec=make_spec(kind=TaskKind.chore, target_repo=None))
    assert route_by_kind(state) == "research_task"


def test_route_by_kind_draft_with_project_is_propose_change():
    state = GraphState(
        spec=make_spec(kind=TaskKind.draft, project="lifekit-stack", target_repo=None)
    )
    assert route_by_kind(state) == "propose_change"


def test_route_by_kind_draft_without_project_is_research():
    state = GraphState(spec=make_spec(kind=TaskKind.draft, target_repo=None))
    assert route_by_kind(state) == "research_task"


# ─── End-to-end with stubs ───────────────────────────────────────────────────


def test_research_task_via_graph_with_stub():
    graph = build_task_graph(
        code_runner=code_task_node_stub,
        research_runner=research_task_node_stub,
        propose_runner=propose_change_node_stub,
    )
    initial = GraphState(spec=make_spec(kind=TaskKind.research, target_repo=None))
    final = graph.invoke(initial)
    assert final["result"].status == "done"
    assert final["spec"].status == TaskStatus.done
    # research stub fills files_changed with findings.md
    assert any("findings.md" in f for f in final["result"].files_changed)


def test_propose_change_via_graph_with_stub():
    graph = build_task_graph(
        code_runner=code_task_node_stub,
        research_runner=research_task_node_stub,
        propose_runner=propose_change_node_stub,
    )
    initial = GraphState(
        spec=make_spec(kind=TaskKind.draft, project="lifekit-stack", target_repo=None)
    )
    final = graph.invoke(initial)
    assert final["result"].status == "done"
    assert final["spec"].status == TaskStatus.done
    # propose_change stub fills files_changed with a proposals/ path
    assert any("proposals/" in f for f in final["result"].files_changed)


def test_code_task_via_graph_still_works():
    """Sanity: code_task still routes correctly after the refactor."""
    graph = build_task_graph(
        code_runner=code_task_node_stub,
        research_runner=research_task_node_stub,
        propose_runner=propose_change_node_stub,
    )
    initial = GraphState(spec=make_spec(kind=TaskKind.code))
    final = graph.invoke(initial)
    assert final["result"].status == "done"
    assert final["result"].pr_url is not None


# ─── _parse_last_json_line helper ────────────────────────────────────────────


def test_parse_last_json_line_simple():
    out = '{"status": "done", "x": 1}'
    parsed = _parse_last_json_line(out)
    assert parsed == {"status": "done", "x": 1}


def test_parse_last_json_line_finds_json_at_end():
    out = """
Doing some work...
Step 1 done.
Step 2 done.
{"status": "done", "pr_url": "https://x"}
"""
    parsed = _parse_last_json_line(out)
    assert parsed is not None
    assert parsed["status"] == "done"


def test_parse_last_json_line_skips_invalid_json():
    out = """
{this is not json}
{"status": "done"}
"""
    parsed = _parse_last_json_line(out)
    assert parsed is not None
    assert parsed["status"] == "done"


def test_parse_last_json_line_none_when_no_json():
    out = "Just some output\nNothing JSON here\n"
    parsed = _parse_last_json_line(out)
    assert parsed is None


def test_parse_last_json_line_handles_trailing_whitespace():
    out = '{"status": "done"}   \n\n  '
    parsed = _parse_last_json_line(out)
    assert parsed is not None
    assert parsed["status"] == "done"
