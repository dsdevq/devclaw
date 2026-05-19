"""Wiring tests for task-lifecycle announces.

Covers intake, sweep dispatch, sweep reap, sweep watchdog, and cmd_dispatch.
Each test injects a MagicMock events_announce and asserts exact call args.
We do not exercise the real `openclaw message send` subprocess — that's
covered separately in `test_cli.py`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

from orchestrator import cli
from orchestrator.dispatch import WATCHDOG_GRACE_SECONDS, persist_spec
from orchestrator.events import EVENTS_CHANNEL
from orchestrator.intake import intake
from orchestrator.runners._subprocess import SubprocessResult
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    Result,
    TaskKind,
    TaskSpec,
    TaskStatus,
)
from orchestrator.sweep import sweep_once

# ─── shared helpers ──────────────────────────────────────────────────────────


def _mock_claude_json(payload: dict) -> SubprocessResult:
    return SubprocessResult(
        status="done",
        parsed_json=payload,
        raw_stdout=str(payload),
        raw_stderr="",
        returncode=0,
    )


def _noop_dispatcher(spec_path):
    return "pid:0"


def _spec(task_id: str, **overrides) -> TaskSpec:
    base = dict(
        task_id=task_id,
        created_at=datetime(2026, 5, 19, 10, 0, tzinfo=UTC),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="tests"),
        verbatim_intent="lifecycle announce test",
        kind=TaskKind.code,
        target_repo="dsdevq/devclaw",
        acceptance_criteria=["x"],
        budget=Budget(max_runtime_seconds=1800),
        status=TaskStatus.ready,
    )
    base.update(overrides)
    return TaskSpec(**base)


def _write_atomic_spec(life: Path, spec: TaskSpec) -> Path:
    d = life / "tasks" / spec.task_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / "spec.yaml"
    persist_spec(spec, p)
    return p


def _setup_life(tmp_path: Path) -> Path:
    life = tmp_path / "life"
    (life / "tasks").mkdir(parents=True)
    (life / "system").mkdir(parents=True)
    return life


# ─── event 1: task_intake → spec_created ─────────────────────────────────────


def test_intake_fires_spec_created_event(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    announce = MagicMock()
    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json(
            {
                "kind": "code",
                "target_repo": "dsdevq/devclaw",
                "acceptance_criteria": ["x"],
                "budget_seconds": 900,
            }
        ),
    ):
        spec = intake(
            "tweak something in devclaw",
            requester_route=RequesterRoute(channel="telegram", to="123"),
            life_root=life,
            events_announce=announce,
            events_chat="ECHAT",
        )

    assert spec is not None
    announce.assert_called_once()
    channel, target, message = announce.call_args.args
    assert channel == EVENTS_CHANNEL
    assert target == "ECHAT"
    assert message == f"📋 Queued: {spec.task_id} → dsdevq/devclaw"


def test_intake_fires_project_less_label_when_no_target_repo(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    announce = MagicMock()
    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json(
            {
                "kind": "research",
                "target_repo": None,
                "acceptance_criteria": ["findings"],
            }
        ),
    ):
        spec = intake(
            "research something",
            requester_route=RequesterRoute(channel="telegram", to="123"),
            life_root=life,
            events_announce=announce,
            events_chat="ECHAT",
        )

    assert spec is not None
    message = announce.call_args.args[2]
    assert message == f"📋 Queued: {spec.task_id} → (project-less)"


def test_intake_does_not_fire_when_validation_fails(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()

    announce = MagicMock()
    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json({"kind": "invalid-kind"}),
    ):
        spec = intake(
            "x",
            requester_route=RequesterRoute(channel="test", to="t"),
            life_root=life,
            events_announce=announce,
            events_chat="ECHAT",
        )

    assert spec is None
    announce.assert_not_called()


# ─── event 2: task_dispatch → dispatched-* ───────────────────────────────────


def test_sweep_fires_dispatched_event_on_ready_spec(tmp_path: Path):
    life = _setup_life(tmp_path)
    spec = _spec("disp-1", status=TaskStatus.ready)
    _write_atomic_spec(life, spec)

    announce = MagicMock()
    sweep_once(
        life,
        dispatcher=_noop_dispatcher,
        events_announce=announce,
        events_chat="ECHAT",
    )

    # exactly one dispatched event for this spec; no other events fire (no reap/watchdog work)
    dispatched_calls = [
        c for c in announce.call_args_list if "🚀 Dispatched" in c.args[2]
    ]
    assert len(dispatched_calls) == 1
    channel, target, message = dispatched_calls[0].args
    assert channel == EVENTS_CHANNEL
    assert target == "ECHAT"
    assert message == "🚀 Dispatched: disp-1 (subagent)"


# ─── events 3/4 via cmd_dispatch (runner subprocess path) ────────────────────


def _spec_path_for_cli(tmp_path: Path) -> Path:
    """A real-on-disk spec.yaml path that satisfies cli.cmd_dispatch's is_file() guard."""
    p = tmp_path / "spec.yaml"
    p.write_text("placeholder\n")
    return p


def _stub_graph_returning(final_state: dict):
    g = MagicMock()
    g.invoke.return_value = final_state
    return g


def test_cmd_dispatch_fires_done_with_pr_url(tmp_path: Path):
    spec_path = _spec_path_for_cli(tmp_path)
    args = mock.MagicMock(spec=["spec", "db", "thread_id"])
    args.spec = str(spec_path)
    args.db = str(tmp_path / "orch.sqlite")
    args.thread_id = None

    final_spec = _spec("runner-done-pr", status=TaskStatus.done)
    final_result = Result(
        task_id=final_spec.task_id,
        status="done",
        completed_at=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
        pr_url="https://github.com/dsdevq/devclaw/pull/42",
    )

    fake_announce = MagicMock()
    with mock.patch.object(cli, "load_spec", return_value=final_spec), \
         mock.patch.object(cli, "sqlite_checkpointer"), \
         mock.patch.object(
             cli,
             "build_task_graph",
             return_value=_stub_graph_returning(
                 {"spec": final_spec, "result": final_result, "error": None}
             ),
         ), \
         mock.patch.object(cli, "persist_spec"), \
         mock.patch.object(cli, "notify_telegram"), \
         mock.patch.object(
             cli, "_resolve_events_announce", return_value=(fake_announce, "ECHAT")
         ):
        rc = cli.cmd_dispatch(args)

    assert rc == 0
    done_calls = [c for c in fake_announce.call_args_list if "✅ Done" in c.args[2]]
    assert len(done_calls) == 1
    _, target, message = done_calls[0].args
    assert target == "ECHAT"
    assert message == (
        "✅ Done: runner-done-pr\nhttps://github.com/dsdevq/devclaw/pull/42"
    )


def test_cmd_dispatch_fires_done_without_pr_url(tmp_path: Path):
    spec_path = _spec_path_for_cli(tmp_path)
    args = mock.MagicMock(spec=["spec", "db", "thread_id"])
    args.spec = str(spec_path)
    args.db = str(tmp_path / "orch.sqlite")
    args.thread_id = None

    final_spec = _spec("runner-done-nopr", status=TaskStatus.done)
    final_result = Result(
        task_id=final_spec.task_id,
        status="done",
        completed_at=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
        pr_url=None,
    )

    fake_announce = MagicMock()
    with mock.patch.object(cli, "load_spec", return_value=final_spec), \
         mock.patch.object(cli, "sqlite_checkpointer"), \
         mock.patch.object(
             cli,
             "build_task_graph",
             return_value=_stub_graph_returning(
                 {"spec": final_spec, "result": final_result, "error": None}
             ),
         ), \
         mock.patch.object(cli, "persist_spec"), \
         mock.patch.object(cli, "notify_telegram"), \
         mock.patch.object(
             cli, "_resolve_events_announce", return_value=(fake_announce, "ECHAT")
         ):
        cli.cmd_dispatch(args)

    done_calls = [c for c in fake_announce.call_args_list if "✅ Done" in c.args[2]]
    assert len(done_calls) == 1
    assert done_calls[0].args[2] == "✅ Done: runner-done-nopr"


# ─── event 5: failed (cmd_dispatch) / abandoned (sweep watchdog) ─────────────


def test_cmd_dispatch_fires_failed_event_on_blocked_spec(tmp_path: Path):
    spec_path = _spec_path_for_cli(tmp_path)
    args = mock.MagicMock(spec=["spec", "db", "thread_id"])
    args.spec = str(spec_path)
    args.db = str(tmp_path / "orch.sqlite")
    args.thread_id = None

    final_spec = _spec(
        "runner-failed",
        status=TaskStatus.blocked,
        result_summary="escalated: verification_failed",
    )
    final_result = Result(
        task_id=final_spec.task_id,
        status="blocked",
        completed_at=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
        blocker="verification_failed",
        notes="three ACs failed",
    )

    fake_announce = MagicMock()
    with mock.patch.object(cli, "load_spec", return_value=final_spec), \
         mock.patch.object(cli, "sqlite_checkpointer"), \
         mock.patch.object(
             cli,
             "build_task_graph",
             return_value=_stub_graph_returning(
                 {"spec": final_spec, "result": final_result, "error": "verification_failed"}
             ),
         ), \
         mock.patch.object(cli, "persist_spec"), \
         mock.patch.object(cli, "notify_telegram"), \
         mock.patch.object(
             cli, "_resolve_events_announce", return_value=(fake_announce, "ECHAT")
         ):
        rc = cli.cmd_dispatch(args)

    assert rc == 1
    failed_calls = [c for c in fake_announce.call_args_list if "❌ failed" in c.args[2]]
    assert len(failed_calls) == 1
    msg = failed_calls[0].args[2]
    assert msg.startswith("❌ failed: runner-failed\n")
    assert "verification_failed" in msg


def test_sweep_watchdog_fires_abandoned_event(tmp_path: Path):
    life = _setup_life(tmp_path)
    # A dispatched spec well past its watchdog deadline.
    dispatched_at = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    watchdog_deadline = dispatched_at + timedelta(seconds=1800 + WATCHDOG_GRACE_SECONDS)
    spec = _spec(
        "ghosted-1",
        status=TaskStatus.dispatched_subagent,
        dispatched_at=dispatched_at,
        watchdog_deadline=watchdog_deadline,
    )
    _write_atomic_spec(life, spec)

    announce = MagicMock()
    # is_ghosted uses now_utc() — well past watchdog_deadline by 2026-05-19 default.
    sweep_once(
        life,
        dispatcher=_noop_dispatcher,
        events_announce=announce,
        events_chat="ECHAT",
    )

    abandoned_calls = [
        c for c in announce.call_args_list if "❌ abandoned" in c.args[2]
    ]
    assert len(abandoned_calls) == 1
    _, target, message = abandoned_calls[0].args
    assert target == "ECHAT"
    assert message.startswith("❌ abandoned: ghosted-1\n")
    assert "runner_silent_past_deadline" in message


def test_sweep_reap_done_with_pr_url_fires_done_event(tmp_path: Path):
    life = _setup_life(tmp_path)
    spec = _spec(
        "reap-pr",
        status=TaskStatus.dispatched_subagent,
        dispatched_at=datetime(2026, 5, 19, 10, 0, tzinfo=UTC),
    )
    spec_path = _write_atomic_spec(life, spec)
    (spec_path.parent / "result.json").write_text(
        json.dumps(
            {
                "task_id": "reap-pr",
                "status": "done",
                "completed_at": "2026-05-19T11:00:00+00:00",
                "pr_url": "https://github.com/dsdevq/devclaw/pull/99",
            }
        )
    )

    announce = MagicMock()
    sweep_once(
        life,
        dispatcher=_noop_dispatcher,
        events_announce=announce,
        events_chat="ECHAT",
    )

    done_calls = [c for c in announce.call_args_list if "✅ Done" in c.args[2]]
    assert len(done_calls) == 1
    msg = done_calls[0].args[2]
    assert msg == "✅ Done: reap-pr\nhttps://github.com/dsdevq/devclaw/pull/99"


def test_sweep_reap_blocked_fires_failed_event(tmp_path: Path):
    life = _setup_life(tmp_path)
    spec = _spec(
        "reap-blocked",
        status=TaskStatus.dispatched_subagent,
        dispatched_at=datetime(2026, 5, 19, 10, 0, tzinfo=UTC),
    )
    spec_path = _write_atomic_spec(life, spec)
    (spec_path.parent / "result.json").write_text(
        json.dumps(
            {
                "task_id": "reap-blocked",
                "status": "blocked",
                "completed_at": "2026-05-19T11:00:00+00:00",
                "blocker": "merge_conflict",
            }
        )
    )

    announce = MagicMock()
    sweep_once(
        life,
        dispatcher=_noop_dispatcher,
        events_announce=announce,
        events_chat="ECHAT",
    )

    failed_calls = [c for c in announce.call_args_list if "❌ failed" in c.args[2]]
    assert len(failed_calls) == 1


# ─── regression: events_announce default no-op doesn't break existing flows ──


def test_sweep_works_without_events_announce(tmp_path: Path):
    """Caller may omit events_announce entirely; sweep must not fire any callback."""
    life = _setup_life(tmp_path)
    spec = _spec("no-events", status=TaskStatus.ready)
    _write_atomic_spec(life, spec)

    # No events_announce arg → no event firing, no error.
    result = sweep_once(life, dispatcher=_noop_dispatcher)
    assert "no-events" in result.dispatched


def test_intake_works_without_events_announce(tmp_path: Path):
    life = tmp_path / "life"
    life.mkdir()
    with patch(
        "orchestrator.intake.run_claude",
        return_value=_mock_claude_json({"kind": "research"}),
    ):
        spec = intake(
            "x",
            requester_route=RequesterRoute(channel="test", to="t"),
            life_root=life,
        )
    assert spec is not None


# ─── ≤300 char guarantee on a real sweep watchdog message ────────────────────


def test_watchdog_event_respects_300_char_cap(tmp_path: Path):
    life = _setup_life(tmp_path)
    dispatched_at = datetime(2026, 5, 18, 12, 0, tzinfo=UTC)
    spec = _spec(
        "ghosted-long",
        status=TaskStatus.dispatched_subagent,
        dispatched_at=dispatched_at,
        watchdog_deadline=dispatched_at + timedelta(seconds=900),
    )
    _write_atomic_spec(life, spec)

    announce = MagicMock()
    sweep_once(
        life,
        dispatcher=_noop_dispatcher,
        events_announce=announce,
        events_chat="ECHAT",
    )

    for call in announce.call_args_list:
        assert len(call.args[2]) <= 300
