"""LangGraph wiring — composes deterministic + cognition nodes into the task pipeline.

This is the v0.0.1 slice: one TaskSpec in, one Result out, via:

  START → code_task (cognition) → verify (deterministic) → {complete | retry → code_task | escalate}

The wrapping orchestration layer (project_curator-over-multiple-runs) is a separate supervisor graph composed on top — out of scope for the first commit.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

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
from orchestrator.state.models import GraphState


def build_task_graph(
    *,
    code_runner: Callable[[GraphState], dict] | None = None,
    checkpointer=None,
):
    """Compile the single-task pipeline.

    Args:
        code_runner: override for code_task_node — pass `code_task_node_stub` in tests so we don't burn real Claude tokens.
        checkpointer: a LangGraph checkpointer; default `None` (in-memory, ephemeral).

    Returns:
        A compiled LangGraph runnable. Call `.invoke({"spec": <TaskSpec>}, config={"configurable": {"thread_id": ...}})`.
    """
    runner = code_runner or code_task_node

    builder = StateGraph(GraphState)

    builder.add_node("code_task", runner)
    builder.add_node("verify", verify_node)
    builder.add_node("retry", increment_retry)
    builder.add_node("complete", complete_node)
    builder.add_node("escalate", escalate_node)

    builder.add_edge(START, "code_task")
    builder.add_edge("code_task", "verify")
    builder.add_conditional_edges(
        "verify",
        route_after_verify,
        {
            "complete": "complete",
            "retry": "retry",
            "escalate": "escalate",
        },
    )
    builder.add_edge("retry", "code_task")  # one-shot retry — route_after_verify guards via retry_count
    builder.add_edge("complete", END)
    builder.add_edge("escalate", END)

    return builder.compile(checkpointer=checkpointer)


def sqlite_checkpointer(db_path: Path) -> SqliteSaver:
    """Open a SQLite checkpointer at the given path. Caller owns lifecycle."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    return SqliteSaver(conn)
