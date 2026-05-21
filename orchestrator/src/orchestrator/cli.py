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
import subprocess
import sys
from pathlib import Path

from orchestrator.daemon import DaemonConfig, install_signal_handlers, run_daemon
from orchestrator.dispatch import load_spec, persist_spec, record_manual_merge
from orchestrator.events import (
    emit_done,
    emit_terminal_failure,
    resolve_events_chat,
)
from orchestrator.graph import build_task_graph, postgres_checkpointer, sqlite_checkpointer
from orchestrator.intake import intake_from_prose
from orchestrator.notify import notify_telegram
from orchestrator.state.models import GraphState, RequesterRoute, TaskStatus
from orchestrator.status import lookup_task_status
from orchestrator.supervisor import tick_run
from orchestrator.sweep import DEFAULT_MAX_CONCURRENT_CLAUDES, sweep_once


def cmd_dispatch(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec).expanduser().resolve()
    if not spec_path.is_file():
        print(f"error: spec not found: {spec_path}", file=sys.stderr)
        return 2

    spec = load_spec(spec_path)

    if args.db.startswith(("postgres://", "postgresql://")):
        checkpointer = postgres_checkpointer(args.db)
    else:
        checkpointer = sqlite_checkpointer(Path(args.db).expanduser())
    graph = build_task_graph(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": args.thread_id or spec.task_id}}
    final = graph.invoke(GraphState(spec=spec), config=config)

    # Persist the final spec back to disk so the world can see status: done|blocked.
    # Without this, the spec stays at status: dispatched-* on disk and the sweep
    # watchdog would later misidentify it as a ghost.
    final_spec = final.get("spec")
    if final_spec is not None:
        persist_spec(final_spec, spec_path)
        chat_id = final_spec.requester_route.to
        events_chat = resolve_events_chat(chat_id)
        if final_spec.status == TaskStatus.done:
            result_obj = final.get("result")
            pr_url = result_obj.pr_url if result_obj is not None else None
            notify_telegram(
                chat_id,
                f"✅ done {final_spec.task_id} — {pr_url or 'no PR'}",
            )
            emit_done(
                task_id=final_spec.task_id,
                pr_url=pr_url,
                chat_id=events_chat,
                announce=_openclaw_announce,
            )
        elif final_spec.status == TaskStatus.blocked:
            result_obj = final.get("result")
            blocker = result_obj.blocker if result_obj is not None else None
            notify_telegram(
                chat_id,
                f"🚫 blocked {final_spec.task_id} — {blocker or 'unknown'}",
            )
            emit_terminal_failure(
                task_id=final_spec.task_id,
                new_state="blocked",
                reason=blocker or final_spec.result_summary,
                chat_id=events_chat,
                announce=_openclaw_announce,
            )

    # Also drop a result.json next to the spec so reaps in mixed-cron environments
    # (markdown skill + Python orchestrator coexisting during cutover) can recover it.
    result = final.get("result")
    if result is not None:
        result_json_path = spec_path.parent / "result.json"
        result_json_path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, default=str)
        )
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

    result = sweep_once(life_root, max_concurrent_claudes=args.max_concurrent_claudes)
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


def cmd_intake(args: argparse.Namespace) -> int:
    """Convert a natural-language intent into a TaskSpec and write it to disk.

    Reads prose from `--prose` if given, otherwise from stdin. Emits a
    single-line JSON object on stdout; narrates progress on stderr.

    Idempotent: re-running with byte-identical (prose, --from) returns the
    existing task_id with `state="duplicate"` and does NOT create a second
    spec on disk.
    """
    if args.prose is not None:
        prose = args.prose
    else:
        prose = sys.stdin.read()
    prose = prose.strip()
    if not prose:
        print("error: prose is empty (use --prose or pipe into stdin)", file=sys.stderr)
        return 2

    def _say(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    _say(f"devclaw intake: reading {len(prose)} chars from {'--prose' if args.prose else 'stdin'}")

    result = intake_from_prose(
        prose,
        from_surface=args.from_surface,
        life_root=Path(args.life).expanduser(),
        progress=_say,
        events_announce=_openclaw_announce,
        events_chat_id=resolve_events_chat(),
    )
    if result is None:
        print("error: task_intake failed (see logs)", file=sys.stderr)
        return 1

    payload = {
        "task_id": result.task_id,
        "spec_path": str(result.spec_path),
        "budget_min": result.budget_min,
        "target_repo": result.target_repo,
        "state": result.state,
    }
    print(json.dumps(payload))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Look up a task's state by task_id. Reads from spec.yaml + result.json on disk.

    Emits a single-line JSON object on stdout. Exit 0 even if the task is
    unknown (the JSON carries `state="unknown"`) — the caller decides what to
    do with that.
    """
    life_root = Path(args.life).expanduser()
    info = lookup_task_status(args.task_id, life_root=life_root)
    print(json.dumps(info))
    return 0


def cmd_supervise(args: argparse.Namespace) -> int:
    """Run one supervisor heartbeat for the given dag.yaml."""
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


def _openclaw_announce(channel: str, target: str, message: str) -> None:
    """Real announce: shell out to `openclaw message send`. Failures are logged, not raised."""
    log = logging.getLogger("orchestrator.daemon.announce")
    try:
        result = subprocess.run(
            args=[
                "openclaw",
                "message",
                "send",
                "--channel",
                channel,
                "--target",
                target,
                "--message",
                message,
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
        if result.returncode != 0:
            log.warning(
                "openclaw message send rc=%s stderr=%s",
                result.returncode,
                result.stderr.decode("utf-8", "replace").strip()[:200],
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("openclaw message send failed: %s", exc)


def cmd_record_manual_merge(args: argparse.Namespace) -> int:
    """Stamp `merged_at` on a spec.yaml after a manual `gh pr merge`.

    Call this when you've merged a PR by hand (or any path other than
    pr_review_loop). Without it, any child spec whose `depends_on` points at
    this task will stay gated by the DAG-aware sweep — `_ready_to_dispatch`
    requires `merged_at` for parents that produced code, not just `status: done`.
    """
    life_root = Path(args.life).expanduser().resolve()
    if not life_root.is_dir():
        print(f"error: --life root not found: {life_root}", file=sys.stderr)
        return 2
    try:
        spec_path = record_manual_merge(args.task_id, life_root=life_root)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"stamped merged_at on {spec_path}")
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    """Long-running scheduler: interleaves sweep (15 min) + supervise-all (30 min).

    Designed to be the entrypoint of a single long-running container — replaces
    the OpenClaw cron entries `task_dispatch_15m` and `curator_30m` so each tick
    runs at zero LLM tokens.
    """
    import threading

    life_root = Path(args.life).expanduser().resolve()
    if not life_root.is_dir():
        print(f"error: --life root not found: {life_root}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )

    config = DaemonConfig(
        life_root=life_root,
        sweep_interval_s=args.sweep_interval,
        supervise_interval_s=args.supervise_interval,
        supervise_offset_s=args.supervise_offset,
        telegram_chat=args.telegram_chat,
        announce=_openclaw_announce,
        events_announce=_openclaw_announce,
        telegram_events_chat=resolve_events_chat(args.telegram_chat),
        max_concurrent_claudes=args.max_concurrent_claudes,
    )
    shutdown = threading.Event()
    install_signal_handlers(shutdown)

    logger = logging.getLogger("orchestrator.daemon")
    logger.info(
        "daemon start life=%s sweep_interval=%ss supervise_interval=%ss",
        life_root,
        config.sweep_interval_s,
        config.supervise_interval_s,
    )
    run_daemon(config, shutdown=shutdown)
    logger.info("daemon stopped")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="devclaw-orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dispatch = sub.add_parser("dispatch", help="run one TaskSpec end-to-end")
    p_dispatch.add_argument("spec", help="path to a spec.yaml")
    p_dispatch.add_argument(
        "--db",
        default="~/.life/orchestrator.sqlite",
        help=(
            "Checkpointer location: a SQLite file path, or a "
            "postgres://... / postgresql://... connection string for the Postgres backend."
        ),
    )
    p_dispatch.add_argument(
        "--thread-id",
        default=None,
        help="LangGraph thread id (defaults to spec.task_id)",
    )
    p_dispatch.set_defaults(func=cmd_dispatch)

    p_intake = sub.add_parser(
        "intake",
        help="convert a natural-language intent into a TaskSpec on disk",
        description=(
            "Read prose from stdin (or --prose) and run the intake LangGraph node "
            "to write a spec.yaml under ~/.life. Idempotent: byte-identical (prose, "
            "--from) inputs return the same task_id with state=duplicate and do not "
            "create a second spec on disk."
        ),
    )
    p_intake.add_argument(
        "--prose",
        default=None,
        help="prose intent (if omitted, read from stdin)",
    )
    p_intake.add_argument(
        "--from",
        dest="from_surface",
        default="cli",
        help="provenance label (e.g. pc-kit, telegram, cron). Default: cli",
    )
    p_intake.add_argument(
        "--life",
        default="~/.life",
        help="root of the ~/.life store",
    )
    p_intake.set_defaults(func=cmd_intake)

    p_status = sub.add_parser(
        "status",
        help="look up a task's state by task_id",
        description=(
            "Read spec.yaml + result.json under ~/.life/tasks/<id>/ or "
            "~/.life/projects/*/tasks/<id>/ and emit a single-line JSON status."
        ),
    )
    p_status.add_argument("task_id", help="task id to look up")
    p_status.add_argument("--life", default="~/.life", help="root of the ~/.life store")
    p_status.set_defaults(func=cmd_status)

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
    p_sweep.add_argument(
        "--max-concurrent-claudes",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_CLAUDES,
        help=(
            "global cap on in-flight claude subprocesses across the orchestrator "
            "(default %(default)s — chosen for VPSes with ~3.7 GiB RAM where a "
            "single claude --print peaks at 1–1.5 GiB)"
        ),
    )
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

    p_record_merge = sub.add_parser(
        "record-manual-merge",
        help=(
            "stamp merged_at on a spec.yaml after a manual `gh pr merge` — "
            "needed so DAG-gated children unblock"
        ),
    )
    p_record_merge.add_argument(
        "task_id",
        help="task_id of the spec whose PR was merged manually",
    )
    p_record_merge.add_argument(
        "--life",
        default="~/.life",
        help="root of the ~/.life store (default ~/.life)",
    )
    p_record_merge.set_defaults(func=cmd_record_manual_merge)

    p_daemon = sub.add_parser(
        "daemon",
        help="long-running scheduler — sweep every 15 min, supervise every 30 min",
    )
    p_daemon.add_argument("--life", default="~/.life", help="root of the ~/.life store")
    p_daemon.add_argument(
        "--sweep-interval",
        type=float,
        default=15 * 60,
        help="seconds between sweep ticks (default 900)",
    )
    p_daemon.add_argument(
        "--supervise-interval",
        type=float,
        default=30 * 60,
        help="seconds between supervise-all ticks (default 1800)",
    )
    p_daemon.add_argument(
        "--supervise-offset",
        type=float,
        default=60.0,
        help="seconds to delay first supervise tick after start (stagger from sweep)",
    )
    p_daemon.add_argument("--telegram-chat", default="default")
    p_daemon.add_argument(
        "--max-concurrent-claudes",
        type=int,
        default=DEFAULT_MAX_CONCURRENT_CLAUDES,
        help=(
            "global cap on in-flight claude subprocesses (default %(default)s). "
            "Each in-flight claude --print can peak at ~1.5 GiB; raise only "
            "after confirming the orchestrator container has N * 1.5 GiB headroom."
        ),
    )
    p_daemon.set_defaults(func=cmd_daemon)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
