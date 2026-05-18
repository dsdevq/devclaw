"""LangGraph wiring — composes deterministic + cognition nodes into the task pipeline.

The graph routes by task kind:

  START → route_by_kind → {code_task | research_task | propose_change}
                             ↓
                          verify
                             ↓
                       {complete | retry → route_by_kind | escalate}

Wraps the Run-level orchestration (project_curator equivalent — multiple Runs, dependency graph) as a separate supervisor graph built on top of this per-task pipeline.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable, Literal

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph

from orchestrator.nodes.verify import (
    complete_node,
    escalate_node,
    increment_retry,
    route_after_verify,
    verify_node,
)
from orchestrator.runners.code_task import code_task_node
from orchestrator.runners.propose_change import propose_change_node
from orchestrator.runners.research_task import research_task_node
from orchestrator.state.models import GraphState, TaskKind


def route_by_kind(
    state: GraphState,
) -> Literal["code_task", "research_task", "propose_change"]:
    """Pick the runner for this spec.

    Kinds:
      - code                  → code_task
      - research, chore       → research_task
      - draft, with project   → propose_change (project-bound draft = RFC)
      - draft, without project → research_task (raw draft)
      - decision              → not handled by this graph (atomic intake handles it)
    """
    kind = state.spec.kind
    if kind == TaskKind.code:
        return "code_task"
    if kind == TaskKind.draft and state.spec.project:
        return "propose_change"
    return "research_task"


def build_task_graph(
    *,
    code_runner: Callable[[GraphState], dict] | None = None,
    research_runner: Callable[[GraphState], dict] | None = None,
    propose_runner: Callable[[GraphState], dict] | None = None,
    checkpointer=None,
):
    """Compile the single-task pipeline with kind-based runner routing.

    Args:
        code_runner / research_runner / propose_runner: override the production runners with stubs in tests.
        checkpointer: a LangGraph checkpointer; default `None` (in-memory).

    Returns:
        A compiled LangGraph runnable. Call `.invoke({"spec": <TaskSpec>}, config={"configurable": {"thread_id": ...}})`.
    """
    code_fn = code_runner or code_task_node
    research_fn = research_runner or research_task_node
    propose_fn = propose_runner or propose_change_node

    builder = StateGraph(GraphState)

    builder.add_node("code_task", code_fn)
    builder.add_node("research_task", research_fn)
    builder.add_node("propose_change", propose_fn)
    builder.add_node("verify", verify_node)
    builder.add_node("retry", increment_retry)
    builder.add_node("complete", complete_node)
    builder.add_node("escalate", escalate_node)

    # START → route_by_kind → one of the runners
    builder.add_conditional_edges(
        START,
        route_by_kind,
        {
            "code_task": "code_task",
            "research_task": "research_task",
            "propose_change": "propose_change",
        },
    )

    # All runners → verify
    for runner in ("code_task", "research_task", "propose_change"):
        builder.add_edge(runner, "verify")

    # verify → complete | retry | escalate
    builder.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "complete": "complete",
            "retry": "retry",
            "escalate": "escalate",
        },
    )

    # retry → re-dispatch via the same kind-routing
    builder.add_conditional_edges(
        "retry",
        route_by_kind,
        {
            "code_task": "code_task",
            "research_task": "research_task",
            "propose_change": "propose_change",
        },
    )

    builder.add_edge("complete", END)
    builder.add_edge("escalate", END)

    return builder.compile(checkpointer=checkpointer)


def sqlite_checkpointer(db_path: Path) -> SqliteSaver:
    """Open a SQLite checkpointer at the given path. Caller owns lifecycle."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)
