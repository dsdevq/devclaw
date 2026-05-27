"""Integration tests for the five task-lifecycle announce sites.

These tests assert that the right event fires at the right state-transition
point — without relying on the real `openclaw message send` subprocess.

Sites under test:
  1. intake_from_prose       → emit_queued
  2. sweep_once dispatch     → emit_dispatched
  3. supervisor._dispatch_*  → emit_dispatched (run-bound)
  4. cli.cmd_dispatch done   → emit_done (with + without pr_url)
  5. cli.cmd_dispatch blocked→ emit_terminal_failure
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from orchestrator.dispatch import persist_spec
from orchestrator.intake import intake_from_prose
from orchestrator.runners._subprocess import SubprocessResult
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)
from orchestrator.sweep import sweep_once


def _claude_intake_payload() -> SubprocessResult:
    return SubprocessResult(
        status="done",
        parsed_json={
            "kind": "code",
            "target_repo": "dsdevq/devclaw",
            "target_branch": "main",
            "project": None,
            "acceptance_criteria": ["x"],
            "budget_seconds": 1200,
            "notes": "noop",
        },
        raw_stdout="",
        raw_stderr="",
        returncode=0,
    )


def _make_ready_spec(task_id: str, target_repo: str | None = "dsdevq/devclaw") -> TaskSpec:
    return TaskSpec(
        task_id=task_id,
        created_at=datetime(2026, 5, 20, 10, 0, tzinfo=UTC),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="tests"),
        verbatim_intent="lifecycle announce coverage",
        kind=TaskKind.code,
        target_repo=target_repo,
        acceptance_criteria=["noop"],
        budget=Budget(max_runtime_seconds=120),
        status=TaskStatus.ready,
    )


def _life_root(tmp_path: Path) -> Path:
    """Build life_root for tests. Note: flat-bucket `tasks/` now lives under
    state_tasks_dir() (conftest points LIFEKIT_STATE_DIR at tmp_path), so any
    helper writing flat-bucket specs should use state_tasks_dir() rather than
    `life / "tasks"`."""
    life = tmp_path / "life"
    (life / "system").mkdir(parents=True)
    return life


def _flat_task_dir(task_id: str) -> Path:
    from orchestrator.paths import state_tasks_dir
    return state_tasks_dir() / task_id


# ─── 1. intake_from_prose → emit_queued ──────────────────────────────────────


def test_intake_from_prose_fires_emit_queued_on_new(tmp_path: Path):
    life = _life_root(tmp_path)
    announce = MagicMock()

    with mock.patch(
        "orchestrator.intake.run_claude", return_value=_claude_intake_payload()
    ):
        result = intake_from_prose(
            "build the thing",
            from_surface="pc-kit",
            life_root=life,
            events_announce=announce,
            events_chat_id="EVENTS",
        )

    assert result is not None and result.state == "new"
    announce.assert_called_once()
    channel, target, msg = announce.call_args.args
    assert channel == "telegram"
    assert target == "EVENTS"
    assert msg.startswith("📋 Queued: ")
    assert "dsdevq/devclaw" in msg
    assert result.task_id in msg


def test_intake_from_prose_does_not_fire_on_duplicate(tmp_path: Path):
    life = _life_root(tmp_path)
    announce = MagicMock()

    with mock.patch(
        "orchestrator.intake.run_claude", return_value=_claude_intake_payload()
    ):
        intake_from_prose(
            "dedupe me",
            from_surface="pc-kit",
            life_root=life,
            events_announce=announce,
            events_chat_id="EVENTS",
        )
        intake_from_prose(
            "dedupe me",
            from_surface="pc-kit",
            life_root=life,
            events_announce=announce,
            events_chat_id="EVENTS",
        )

    assert announce.call_count == 1, (
        "duplicate intake must NOT re-announce; "
        f"got {announce.call_args_list!r}"
    )


def test_intake_from_prose_project_less_label(tmp_path: Path):
    life = _life_root(tmp_path)
    announce = MagicMock()

    payload = SubprocessResult(
        status="done",
        parsed_json={
            "kind": "research",
            "target_repo": None,
            "target_branch": "main",
            "project": None,
            "acceptance_criteria": ["x"],
            "budget_seconds": 600,
            "notes": "noop",
        },
        raw_stdout="",
        raw_stderr="",
        returncode=0,
    )
    with mock.patch("orchestrator.intake.run_claude", return_value=payload):
        intake_from_prose(
            "research something",
            from_surface="cli",
            life_root=life,
            events_announce=announce,
            events_chat_id="9",
        )

    assert announce.called
    msg = announce.call_args.args[2]
    assert "(project-less)" in msg


# ─── 2. sweep_once dispatch → emit_dispatched ────────────────────────────────


def test_sweep_once_fires_emit_dispatched_on_dispatch(tmp_path: Path):
    life = _life_root(tmp_path)
    spec = _make_ready_spec("2026-05-20-foo")
    task_dir = _flat_task_dir(spec.task_id)
    task_dir.mkdir(parents=True)
    persist_spec(spec, task_dir / "spec.yaml")

    events_announce = MagicMock()

    def _noop_dispatcher(_spec_path: Path) -> str:
        return "pid:0"

    sweep_once(
        life,
        dispatcher=_noop_dispatcher,
        events_announce=events_announce,
        events_chat_id="EVENTS",
    )

    # exactly one dispatched-event for the single ready spec
    dispatch_calls = [
        c for c in events_announce.call_args_list if "🚀 Dispatched:" in c.args[2]
    ]
    assert len(dispatch_calls) == 1
    channel, target, msg = dispatch_calls[0].args
    assert target == "EVENTS"
    assert spec.task_id in msg
    assert "(subagent)" in msg


def test_sweep_once_default_events_announce_is_noop(tmp_path: Path):
    """Backwards-compatible: callers that omit events_announce see no crash."""
    life = _life_root(tmp_path)
    spec = _make_ready_spec("2026-05-20-bar")
    task_dir = _flat_task_dir(spec.task_id)
    task_dir.mkdir(parents=True)
    persist_spec(spec, task_dir / "spec.yaml")

    def _noop_dispatcher(_spec_path: Path) -> str:
        return "pid:0"

    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert result.dispatched == [spec.task_id]


# ─── 3. cli.cmd_dispatch → emit_done / emit_terminal_failure ─────────────────


def _build_final_state(
    spec: TaskSpec,
    *,
    status: TaskStatus,
    pr_url: str | None,
    blocker: str | None,
):
    from orchestrator.state.models import Result

    result = Result(
        task_id=spec.task_id,
        status="done" if status == TaskStatus.done else "blocked",
        completed_at=datetime(2026, 5, 20, 12, 0, tzinfo=UTC),
        pr_url=pr_url,
        branch="kit/x",
        files_changed=[],
        tests_passed=True if status == TaskStatus.done else None,
        notes="ok" if status == TaskStatus.done else None,
        blocker=blocker,
    )
    final_spec = spec.model_copy(
        update={
            "status": status,
            "completed_at": result.completed_at,
            "result_summary": result.notes or "blocked",
        }
    )
    err = blocker if status == TaskStatus.blocked else None
    return {"spec": final_spec, "result": result, "error": err}


def test_cmd_dispatch_fires_emit_done_with_pr(tmp_path: Path):
    from orchestrator import cli

    life = _life_root(tmp_path)
    spec = _make_ready_spec("2026-05-20-done-pr")
    task_dir = _flat_task_dir(spec.task_id)
    task_dir.mkdir(parents=True)
    spec_path = task_dir / "spec.yaml"
    persist_spec(spec, spec_path)

    final = _build_final_state(
        spec,
        status=TaskStatus.done,
        pr_url="https://github.com/dsdevq/devclaw/pull/42",
        blocker=None,
    )

    fake_graph = MagicMock()
    fake_graph.invoke.return_value = final

    captured: list = []

    def fake_announce(channel: str, target: str, message: str) -> None:
        captured.append((channel, target, message))

    with mock.patch("orchestrator.cli.build_task_graph", return_value=fake_graph), mock.patch(
        "orchestrator.cli.sqlite_checkpointer", return_value=None
    ), mock.patch("orchestrator.cli.notify_telegram"), mock.patch(
        "orchestrator.cli._openclaw_announce", side_effect=fake_announce
    ):
        from types import SimpleNamespace
        args = SimpleNamespace(
            spec=str(spec_path),
            db=str(tmp_path / "db.sqlite"),
            thread_id=None,
        )
        rc = cli.cmd_dispatch(args)

    assert rc == 0
    done_msgs = [m for _, _, m in captured if m.startswith("✅ Done:")]
    assert len(done_msgs) == 1
    assert "https://github.com/dsdevq/devclaw/pull/42" in done_msgs[0]
    assert spec.task_id in done_msgs[0]


def test_cmd_dispatch_fires_emit_done_without_pr(tmp_path: Path):
    from orchestrator import cli

    life = _life_root(tmp_path)
    spec = _make_ready_spec("2026-05-20-done-nopr", target_repo=None)
    task_dir = _flat_task_dir(spec.task_id)
    task_dir.mkdir(parents=True)
    spec_path = task_dir / "spec.yaml"
    persist_spec(spec, spec_path)

    final = _build_final_state(spec, status=TaskStatus.done, pr_url=None, blocker=None)

    fake_graph = MagicMock()
    fake_graph.invoke.return_value = final

    captured: list = []
    with mock.patch("orchestrator.cli.build_task_graph", return_value=fake_graph), mock.patch(
        "orchestrator.cli.sqlite_checkpointer", return_value=None
    ), mock.patch("orchestrator.cli.notify_telegram"), mock.patch(
        "orchestrator.cli._openclaw_announce",
        side_effect=lambda c, t, m: captured.append((c, t, m)),
    ):
        from types import SimpleNamespace
        args = SimpleNamespace(
            spec=str(spec_path),
            db=str(tmp_path / "db.sqlite"),
            thread_id=None,
        )
        rc = cli.cmd_dispatch(args)

    assert rc == 0
    done_msgs = [m for _, _, m in captured if m.startswith("✅ Done:")]
    assert len(done_msgs) == 1
    assert "\n" not in done_msgs[0], f"expected no PR-URL line, got {done_msgs[0]!r}"


def test_cmd_dispatch_fires_emit_terminal_failure_on_blocked(tmp_path: Path):
    from orchestrator import cli

    life = _life_root(tmp_path)
    spec = _make_ready_spec("2026-05-20-blk")
    task_dir = _flat_task_dir(spec.task_id)
    task_dir.mkdir(parents=True)
    spec_path = task_dir / "spec.yaml"
    persist_spec(spec, spec_path)

    final = _build_final_state(
        spec,
        status=TaskStatus.blocked,
        pr_url=None,
        blocker="tests_failed",
    )

    fake_graph = MagicMock()
    fake_graph.invoke.return_value = final

    captured: list = []
    with mock.patch("orchestrator.cli.build_task_graph", return_value=fake_graph), mock.patch(
        "orchestrator.cli.sqlite_checkpointer", return_value=None
    ), mock.patch("orchestrator.cli.notify_telegram"), mock.patch(
        "orchestrator.cli._openclaw_announce",
        side_effect=lambda c, t, m: captured.append((c, t, m)),
    ):
        from types import SimpleNamespace
        args = SimpleNamespace(
            spec=str(spec_path),
            db=str(tmp_path / "db.sqlite"),
            thread_id=None,
        )
        rc = cli.cmd_dispatch(args)

    # blocked: error=blocker is set, so cmd_dispatch returns 1
    assert rc == 1
    failure_msgs = [m for _, _, m in captured if m.startswith("❌")]
    assert len(failure_msgs) == 1
    assert "blocked" in failure_msgs[0]
    assert spec.task_id in failure_msgs[0]
    assert "tests_failed" in failure_msgs[0]


# ─── 4. supervisor → emit_dispatched ─────────────────────────────────────────


def test_supervisor_tick_run_fires_emit_dispatched_on_node_dispatch(tmp_path: Path):
    import yaml

    from orchestrator import supervisor
    from orchestrator.state.models import DagNode, Run, RunStatus

    life = _life_root(tmp_path)
    project = "demo"
    run_id = "2026-05-20-r1"
    run_dir = life / "projects" / project / "runs" / run_id
    run_dir.mkdir(parents=True)
    dag_path = run_dir / "dag.yaml"

    run = Run(
        run_id=run_id,
        project=project,
        created_at=datetime(2026, 5, 20, 9, 0, tzinfo=UTC),
        status=RunStatus.in_progress,
        tasks=[
            DagNode(
                id="n1",
                title="first node",
                kind=TaskKind.code,
                budget_seconds=600,
                target_repo="dsdevq/devclaw",
                acceptance_criteria=["x"],
            )
        ],
    )
    dag_path.write_text(yaml.safe_dump(run.model_dump(mode="json"), sort_keys=False))

    events_announce = MagicMock()

    def _noop_dispatcher(_p: Path) -> str:
        return "pid:0"

    result = supervisor.tick_run(
        dag_path,
        life_root=life,
        requester_route=RequesterRoute(channel="telegram", to="x"),
        dispatcher=_noop_dispatcher,
        events_announce=events_announce,
        events_chat_id="EVENTS",
    )

    assert result.dispatched == ["n1"]
    dispatch_msgs = [
        c.args[2]
        for c in events_announce.call_args_list
        if "🚀 Dispatched:" in c.args[2]
    ]
    assert len(dispatch_msgs) == 1
    assert "(subagent)" in dispatch_msgs[0]
    assert events_announce.call_args.args[1] == "EVENTS"


# ─── 5. PR #21 audit-announce regression ─────────────────────────────────────


def test_daemon_audit_loop_announce_path_still_uses_audit_announce(tmp_path: Path):
    """Adding `events_announce` to DaemonConfig must not touch the audit-loop
    path — audit drift announces must keep going through `config.announce`,
    not through the new `events_announce` field."""
    import datetime as dt
    import threading
    import time

    from orchestrator.audits.state_currency import AuditReport, RetiredHit
    from orchestrator.daemon import DaemonConfig, run_daemon

    life = tmp_path / "life"
    (life / "system").mkdir(parents=True)
    (life / "audits").mkdir(parents=True)

    drift = AuditReport(
        generated_at="2026-05-20T00:00:00+00:00",
        retired_hits=[
            RetiredHit(
                term="x",
                file="f.md",
                line_no=1,
                line="...",
                replacement="y",
                retired_on="2026-01-01",
                reason="r",
            )
        ],
    )
    report_path = life / "audits" / f"{dt.date.today().isoformat()}-state-currency.md"

    def fake_audit(p: Path):
        report_path.write_text("stub")
        return drift, report_path

    audit_announce = MagicMock()
    events_announce = MagicMock()

    config = DaemonConfig(
        life_root=life,
        sweep_interval_s=60.0,
        supervise_interval_s=60.0,
        supervise_offset_s=60.0,
        audit_interval_s=0.05,
        audit_offset_s=0.0,
        telegram_chat="audit-chat",
        telegram_events_chat="events-chat",
        announce=audit_announce,
        events_announce=events_announce,
    )

    shutdown = threading.Event()
    t = threading.Thread(
        target=run_daemon,
        kwargs={
            "config": config,
            "shutdown": shutdown,
            "sweep_fn": lambda p: "noop",
            "supervise_fn": lambda p, r: [],
            "audit_fn": fake_audit,
        },
    )
    t.start()
    time.sleep(0.2)
    shutdown.set()
    t.join(timeout=2.0)

    # PR #21 path: exactly one audit-loop announce went through `announce`
    assert audit_announce.call_count == 1
    channel, target, msg = audit_announce.call_args.args
    assert target == "audit-chat"
    assert "drift" in msg.lower() or "retired" in msg.lower()

    # The new events_announce path is NOT used by the audit loop
    assert events_announce.call_count == 0
