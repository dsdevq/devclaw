"""devclaw-orchestrator CLI — entry points for dispatching specs and running the periodic sweep.

Usage:
    devclaw-orchestrator dispatch <spec.yaml> [--db ~/.life/orchestrator.sqlite] [--thread-id <id>]
    devclaw-orchestrator sweep [--life ~/.life] [--quiet]

The `sweep` subcommand is intended to be cron-fired every 15 minutes (the same cadence as the markdown `task_dispatch_15m`).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from orchestrator.dispatch import load_spec
from orchestrator.graph import build_task_graph, sqlite_checkpointer
from orchestrator.state.models import GraphState
from orchestrator.supervisor import tick_run
from orchestrator.sweep import sweep_once


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


def cmd_sweep(args: argparse.Namespace) -> int:
    """Run one reap + watchdog tick over all in-flight specs under ~/.life/."""
    life_root = Path(args.life).expanduser().resolve()
    if not life_root.is_dir():
        print(f"error: --life root not found: {life_root}", file=sys.stderr)
        return 2

    if not args.quiet:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s sweep %(message)s",
            datefmt="%H:%M:%S",
        )

    result = sweep_once(life_root)
    print(result.summary())
    if result.reaped:
        print(f"  reaped: {', '.join(result.reaped)}")
    if result.ghosted:
        print(f"  ghosted: {', '.join(result.ghosted)}")
    if result.errors:
        print(f"  errors:", file=sys.stderr)
        for e in result.errors:
            print(f"    - {e}", file=sys.stderr)
        return 1
    return 0


def cmd_supervise(args: argparse.Namespace) -> int:
    """Run one supervisor heartbeat for the given dag.yaml."""
    from orchestrator.state.models import RequesterRoute

    dag_path = Path(args.dag).expanduser().resolve()
    if not dag_path.is_file():
        print(f"error: dag.yaml not found: {dag_path}", file=sys.stderr)
        return 2

    route = RequesterRoute(channel="telegram", to=args.telegram_chat)
    result = tick_run(
        dag_path,
        life_root=Path(args.life).expanduser(),
        requester_route=route,
    )
    print(result.summary())
    return 0


def cmd_supervise_all(args: argparse.Namespace) -> int:
    """Sweep every active Run under ~/.life/projects/*/runs/*/dag.yaml."""
    from orchestrator.state.models import RequesterRoute

    life_root = Path(args.life).expanduser().resolve()
    if not life_root.is_dir():
        print(f"error: --life root not found: {life_root}", file=sys.stderr)
        return 2

    dags = list(life_root.glob("projects/*/runs/*/dag.yaml"))
    if not dags:
        print("supervise-all: no active runs")
        return 0

    route = RequesterRoute(channel="telegram", to=args.telegram_chat)
    any_errors = False
    for dag_path in dags:
        try:
            result = tick_run(dag_path, life_root=life_root, requester_route=route)
            print(result.summary())
        except Exception as exc:  # noqa: BLE001
            any_errors = True
            print(f"error in {dag_path}: {exc}", file=sys.stderr)
    return 1 if any_errors else 0


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

    p_sweep = sub.add_parser(
        "sweep",
        help="run one reap + watchdog tick over all in-flight specs (intended for cron)",
    )
    p_sweep.add_argument(
        "--life",
        default="~/.life",
        help="root of the ~/.life store (default ~/.life)",
    )
    p_sweep.add_argument("--quiet", action="store_true", help="suppress per-item logging")
    p_sweep.set_defaults(func=cmd_sweep)

    p_sup = sub.add_parser(
        "supervise",
        help="run one supervisor heartbeat for a Run (dispatch ready nodes, reconcile completed)",
    )
    p_sup.add_argument("dag", help="path to a runs/<run>/dag.yaml")
    p_sup.add_argument(
        "--life",
        default="~/.life",
        help="root of the ~/.life store (default ~/.life)",
    )
    p_sup.add_argument(
        "--telegram-chat",
        default="default",
        help="Telegram chat id for escalations + run-complete announce",
    )
    p_sup.set_defaults(func=cmd_supervise)

    p_sup_all = sub.add_parser(
        "supervise-all",
        help="run a supervisor heartbeat for every active Run under ~/.life/projects/*/runs/*",
    )
    p_sup_all.add_argument(
        "--life",
        default="~/.life",
        help="root of the ~/.life store (default ~/.life)",
    )
    p_sup_all.add_argument("--telegram-chat", default="default")
    p_sup_all.set_defaults(func=cmd_supervise_all)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
