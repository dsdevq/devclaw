"""Cycle-window close report (ADR 0006, continuous-eval PR2).

When the per-cycle run window (22:00–05:00 Europe/London) closes, the goal
heartbeat assembles the cycle's slice from rows devclaw already writes
(``eval_outcomes`` + the ``problems`` catalog) and pushes a report through the
existing notifier. It fires EXACTLY once per cycle (cycle_key PK), is CLEAN iff
zero mechanism-wedges fired, and costs ZERO cognition calls. All stubbed — no
docker, no claude.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from devclaw.goal.cycle_report import (
    assemble_cycle_report,
    most_recent_closed_window,
)
from devclaw.goal.service import GoalConfig, GoalService
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue
from tests.goal_fakes import FakeClaude, RecordingNotifier

_LON = ZoneInfo("Europe/London")
_UTC = ZoneInfo("UTC")


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


# ---- window math (pure) ----------------------------------------------------


def test_most_recent_closed_window_after_close_reports_last_cycle():
    # 10:00 London on the 22nd: the window that opened 22:00 on the 21st and
    # closed 05:00 on the 22nd is the most-recent CLOSED one → cycle_key=21st.
    now = _ms(datetime(2026, 7, 22, 10, 0, tzinfo=_UTC))
    nd, start_ms, end_ms = most_recent_closed_window(now)
    assert nd == "2026-07-21"
    assert start_ms < end_ms <= now
    # 02:00 London on the 22nd is INSIDE the reported window.
    assert start_ms <= _ms(datetime(2026, 7, 22, 2, 0, tzinfo=_LON)) < end_ms
    # 21:00 London on the 21st (before the 22:00 open) is NOT inside.
    assert _ms(datetime(2026, 7, 21, 21, 0, tzinfo=_LON)) < start_ms


def test_most_recent_closed_window_inside_current_window_reports_prior_cycle():
    # 03:00 London on the 22nd: still INSIDE the window that opened 22:00 on the
    # 21st (not yet closed), so the most-recent CLOSED window is cycle_key=20th.
    now = _ms(datetime(2026, 7, 22, 3, 0, tzinfo=_LON))
    nd, _s, _e = most_recent_closed_window(now)
    assert nd == "2026-07-20"


def test_most_recent_closed_window_unknown_tz_fails_safe():
    now = _ms(datetime(2026, 7, 22, 10, 0, tzinfo=_UTC))
    assert most_recent_closed_window(now, tz="Not/AZone") is None


# ---- assembly / clean-cycle boundary (pure, over a fake store) -------------


class _FakeStore:
    """Read double returning canned eval_outcomes / problems rows."""

    def __init__(self, outcomes=None, problems=None):
        self._o = outcomes or []
        self._p = problems or []

    def list_eval_outcomes(self, *, source=None, limit=100):
        return list(self._o)

    def list_problems(self, *, category=None, limit=100):
        return list(self._p)


def _window():
    start = _ms(datetime(2026, 7, 21, 22, 0, tzinfo=_LON))
    end = _ms(datetime(2026, 7, 22, 5, 0, tzinfo=_LON))
    return "2026-07-21", start, end, (start + end) // 2


def test_cycle_report_is_clean_when_no_wedges():
    nd, s, e, mid = _window()
    store = _FakeStore(outcomes=[
        {"status": "done", "settled_at": mid, "failure_class": None, "task_id": "t1"},
        {"status": "done", "settled_at": mid, "failure_class": None, "task_id": "t2"},
    ])
    r = assemble_cycle_report(store, nd, s, e)
    assert r.clean is True
    assert r.wedges == []
    assert r.settled == 2 and r.done == 2 and r.failed == 0
    assert "CLEAN" in r.summary


def test_cycle_report_classifies_selfhealed_pause_as_clean_not_wedge():
    # A self-healed quota/auth pause (problems category='limit', recovered) is the
    # pause machinery working unattended — reported in `pauses`, NEVER a wedge.
    nd, s, e, mid = _window()
    store = _FakeStore(
        outcomes=[{"status": "done", "settled_at": mid, "task_id": "t1"}],
        problems=[{
            "category": "limit", "kind": "rate_limit",
            "sample_message": "usage-limit pauses until 05:00",
            "last_seen_ms": mid, "last_goal_id": "g1", "fingerprint": "limit|rate_limit|x",
        }],
    )
    r = assemble_cycle_report(store, nd, s, e)
    assert r.clean is True
    assert r.wedges == []
    assert len(r.pauses) == 1 and r.pauses[0]["class"] == "rate_limit"
    assert "self-healed pauses" in r.summary


def test_cycle_report_mechanical_block_is_a_wedge_not_clean():
    nd, s, e, mid = _window()
    store = _FakeStore(problems=[{
        "category": "block", "kind": "mechanical:corrupt_doc",
        "sample_message": "firmed-draft.yaml torn",
        "last_seen_ms": mid, "last_goal_id": "g1", "fingerprint": "block|mech|x",
    }])
    r = assemble_cycle_report(store, nd, s, e)
    assert r.clean is False
    assert len(r.wedges) == 1 and r.wedges[0]["class"] == "mechanical:corrupt_doc"


def test_cycle_report_engine_and_timeout_classes_are_wedges():
    nd, s, e, mid = _window()
    store = _FakeStore(outcomes=[
        {"status": "failed", "settled_at": mid, "failure_class": "timeout",
         "error": "wall-clock timeout", "task_id": "t1"},
        {"status": "failed", "settled_at": mid, "failure_class": "review_crash",
         "error": "review gate crashed", "task_id": "t2"},
    ])
    r = assemble_cycle_report(store, nd, s, e)
    assert r.clean is False
    assert {w["class"] for w in r.wedges} == {"timeout", "review_crash"}
    assert r.failed == 2


def test_cycle_report_gate_rejection_is_a_quality_outcome_not_a_wedge():
    # review_rejected / verify_failed are the gate DOING ITS JOB — a genuine
    # quality verdict, not a mechanism wedge. The cycle stays clean.
    nd, s, e, mid = _window()
    store = _FakeStore(outcomes=[
        {"status": "failed", "settled_at": mid, "failure_class": "review_rejected",
         "error": "code review requested changes", "task_id": "t1"},
        {"status": "failed", "settled_at": mid, "failure_class": "verify_failed",
         "error": "verify gate failed", "task_id": "t2"},
    ])
    r = assemble_cycle_report(store, nd, s, e)
    assert r.clean is True
    assert r.wedges == []
    assert r.failed == 2


def test_cycle_report_genuine_needs_answer_is_clean_but_surfaced():
    nd, s, e, mid = _window()
    store = _FakeStore(problems=[{
        "category": "block", "kind": "needs_answer",
        "sample_message": "which auth provider?",
        "last_seen_ms": mid, "last_goal_id": "g1", "fingerprint": "block|na|x",
    }])
    r = assemble_cycle_report(store, nd, s, e)
    assert r.clean is True
    assert r.wedges == []
    assert len(r.needs_operator) == 1 and r.needs_operator[0]["class"] == "needs_answer"
    assert "needs operator" in r.summary


def test_cycle_report_ignores_rows_outside_the_window():
    nd, s, e, _mid = _window()
    before = s - 60_000
    after = e + 60_000
    store = _FakeStore(
        outcomes=[
            {"status": "failed", "settled_at": before, "failure_class": "engine_error",
             "error": "boom", "task_id": "t-old"},
            {"status": "failed", "settled_at": after, "failure_class": "engine_error",
             "error": "boom", "task_id": "t-new"},
        ],
        problems=[{
            "category": "cognition", "kind": "planner", "sample_message": "err",
            "last_seen_ms": before, "fingerprint": "cog|x",
        }],
    )
    r = assemble_cycle_report(store, nd, s, e)
    assert r.clean is True
    assert r.settled == 0 and r.wedges == []


# ---- the heartbeat edge (integration over a real store) --------------------


@pytest.fixture()
def db(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _svc(tmp_path, db, notifier):
    goals_dir = tmp_path / "goals"
    cfg = GoalConfig(
        goals_dir=goals_dir, notify_url="", tick_seconds=900,
        eval_every=3, verify_done=False,
    )
    queue = TaskQueue(db)
    planner = FakeClaude(role="planner")
    evaluator = FakeClaude(role="evaluator")
    svc = GoalService(
        queue, db, config=cfg, notifier=notifier,
        planner_caller=planner, evaluator_caller=evaluator,
    )
    return svc, planner, evaluator


def _seed_live_outcome(db, *, task_id, status, settled_at, failure_class=None, error=None):
    db._db.execute(
        "INSERT INTO eval_outcomes (source, task_id, status, failure_class, error, settled_at) "
        "VALUES ('live', ?, ?, ?, ?, ?)",
        (task_id, status, failure_class, error, settled_at),
    )
    db._commit()


def _seed_problem(db, *, category, kind, message, last_seen_ms, goal_id=""):
    db._db.execute(
        "INSERT INTO problems (fingerprint, category, kind, summary, sample_message, "
        " count, recovered_count, terminal_count, first_seen_ms, last_seen_ms, "
        " last_goal_id, last_task_id) "
        "VALUES (?, ?, ?, ?, ?, 1, 1, 0, ?, ?, ?, '')",
        (f"{category}|{kind}|{message}", category, kind, message, message,
         last_seen_ms, last_seen_ms, goal_id),
    )
    db._commit()


@pytest.mark.asyncio
async def test_cycle_report_fires_once_at_window_close_and_is_clean_when_no_wedges(
    tmp_path, db, monkeypatch
):
    fixed_now = _ms(datetime(2026, 7, 22, 10, 0, tzinfo=_UTC))
    monkeypatch.setattr("devclaw.goal.service._now_ms", lambda: fixed_now)
    nd, start_ms, end_ms = most_recent_closed_window(fixed_now)
    mid = (start_ms + end_ms) // 2
    _seed_live_outcome(db, task_id="t1", status="done", settled_at=mid)

    notifier = RecordingNotifier()
    svc, planner, evaluator = _svc(tmp_path, db, notifier)

    emitted = await svc._maybe_emit_cycle_report()
    assert emitted == nd
    # zero-token guard: the cycle-report edge makes NO cognition call.
    assert planner.calls == 0 and evaluator.calls == 0
    # pushed once through the notifier, and persisted CLEAN with sent_at set.
    assert len(notifier.sent) == 1 and "CLEAN" in notifier.sent[0]
    (row,) = db.list_cycle_reports()
    assert row["cycle_key"] == nd
    assert row["clean"] == 1
    assert row["wedges_json"] == "[]"
    assert row["sent_at"] is not None

    # A second wakeup the same day is an idempotent no-op — no second push/row.
    again = await svc._maybe_emit_cycle_report()
    assert again is None
    assert len(notifier.sent) == 1
    assert len(db.list_cycle_reports()) == 1


@pytest.mark.asyncio
async def test_self_issue_filing_fires_once_per_cycle_past_the_idempotency_gate(
    tmp_path, db, monkeypatch
):
    """Placement guard: self-issue filing rides the SAME cycle-close edge — it
    must fire ONCE per cycle and only PAST the ``cycle_report_exists``
    short-circuit, never per tick (the zero-token guard). Pins the call site so a
    future refactor can't hoist it above the idempotency gate unnoticed."""
    from devclaw.goal import self_issue as _si

    fixed_now = _ms(datetime(2026, 7, 22, 10, 0, tzinfo=_UTC))
    monkeypatch.setattr("devclaw.goal.service._now_ms", lambda: fixed_now)

    calls: list[str] = []

    async def _spy(store, **kw):
        calls.append(kw["cycle_key"])
        return _si.SelfIssueResult()

    monkeypatch.setattr("devclaw.goal.self_issue.run_self_issue_filing", _spy)

    svc, planner, evaluator = _svc(tmp_path, db, RecordingNotifier())

    await svc._maybe_emit_cycle_report()
    assert len(calls) == 1                              # fired once, at the close edge
    assert planner.calls == 0 and evaluator.calls == 0  # zero-token edge

    # Second wakeup same cycle: cycle_report_exists short-circuits BEFORE filing.
    await svc._maybe_emit_cycle_report()
    assert len(calls) == 1                              # not re-run — past the gate


@pytest.mark.asyncio
async def test_cycle_report_wedge_marks_night_unclean(tmp_path, db, monkeypatch):
    fixed_now = _ms(datetime(2026, 7, 22, 10, 0, tzinfo=_UTC))
    monkeypatch.setattr("devclaw.goal.service._now_ms", lambda: fixed_now)
    nd, start_ms, end_ms = most_recent_closed_window(fixed_now)
    mid = (start_ms + end_ms) // 2
    _seed_live_outcome(
        db, task_id="t1", status="failed", settled_at=mid,
        failure_class="engine_error", error="docker run failed",
    )
    _seed_problem(db, category="block", kind="mechanical:prep",
                  message="workspace prep failed", last_seen_ms=mid, goal_id="g1")

    notifier = RecordingNotifier()
    svc, planner, evaluator = _svc(tmp_path, db, notifier)

    emitted = await svc._maybe_emit_cycle_report()
    assert emitted == nd
    assert planner.calls == 0 and evaluator.calls == 0
    (row,) = db.list_cycle_reports()
    assert row["clean"] == 0
    assert "engine_error" in row["wedges_json"] and "mechanical:prep" in row["wedges_json"]
    assert "⚠️" in notifier.sent[0]


@pytest.mark.asyncio
async def test_cycle_report_selfhealed_pause_keeps_night_clean_over_real_store(
    tmp_path, db, monkeypatch
):
    fixed_now = _ms(datetime(2026, 7, 22, 10, 0, tzinfo=_UTC))
    monkeypatch.setattr("devclaw.goal.service._now_ms", lambda: fixed_now)
    nd, start_ms, end_ms = most_recent_closed_window(fixed_now)
    mid = (start_ms + end_ms) // 2
    _seed_live_outcome(db, task_id="t1", status="done", settled_at=mid)
    _seed_problem(db, category="limit", kind="quota",
                  message="quota: paused until 05:00", last_seen_ms=mid)

    notifier = RecordingNotifier()
    svc, planner, evaluator = _svc(tmp_path, db, notifier)

    await svc._maybe_emit_cycle_report()
    (row,) = db.list_cycle_reports()
    assert row["clean"] == 1
    assert "quota" in row["pauses_json"]
    assert row["wedges_json"] == "[]"


@pytest.mark.asyncio
async def test_cycle_report_log_only_when_notifier_unconfigured_sets_sent_at_null(
    tmp_path, db, monkeypatch
):
    from devclaw.goal.notify import NullNotifier

    fixed_now = _ms(datetime(2026, 7, 22, 10, 0, tzinfo=_UTC))
    monkeypatch.setattr("devclaw.goal.service._now_ms", lambda: fixed_now)
    nd, start_ms, end_ms = most_recent_closed_window(fixed_now)
    mid = (start_ms + end_ms) // 2
    _seed_live_outcome(db, task_id="t1", status="done", settled_at=mid)

    svc, planner, evaluator = _svc(tmp_path, db, NullNotifier())
    emitted = await svc._maybe_emit_cycle_report()
    assert emitted == nd  # log-only is NOT an error — the report still lands.
    (row,) = db.list_cycle_reports()
    assert row["sent_at"] is None
    assert row["clean"] == 1
