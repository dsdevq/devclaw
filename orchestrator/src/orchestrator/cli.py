"""devclaw-orchestrator CLI — manual entry point for dispatching a single TaskSpec.

Usage:
    devclaw-orchestrator dispatch <spec.yaml> [--db ~/.life/orchestrator.sqlite] [--thread-id <id>]

This is the v0.0.1 invocation surface. Cron-fired sweeps (reap + watchdog over all in-flight specs) will be a separate `devclaw-orchestrator sweep` subcommand once the per-task graph is solid.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from orchestrator.dispatch import load_spec
from orchestrator.graph import build_task_graph, sqlite_checkpointer
from orchestrator.state.models import GraphState


def cmd_dispatch(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec).expanduser().resolve()
    if not spec_path.is_file():
        print(f"error: spec not found: {spec_path}", file=sys.stderr)
        return 2

    spec = load_spec(spec_path)

    checkpointer = sqlite_checkpointer(Path(args.db).expanduser())
    graph = build_task_graph(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": args.thread_id or spec.task_id}}
    final = graph.invoke(GraphState(spec=spec), config=config)

    # Pretty-print the result for human eyes; final["result"] is the structured output.
    result = final.get("result")
    if result is not None:
        print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
    if final.get("error"):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="devclaw-orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dispatch = sub.add_parser("dispatch", help="run one TaskSpec end-to-end")
    p_dispatch.add_argument("spec", help="path to a spec.yaml")
    p_dispatch.add_argument(
        "--db",
        default="~/.life/orchestrator.sqlite",
        help="SQLite checkpointer path",
    )
    p_dispatch.add_argument(
        "--thread-id",
        default=None,
        help="LangGraph thread id (defaults to spec.task_id)",
    )
    p_dispatch.set_defaults(func=cmd_dispatch)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
