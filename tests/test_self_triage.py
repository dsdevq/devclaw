"""Self-triage — the propose-only interceptor on the owner-ping path (slice 1).

Two layers of assertions:
- The layer-3 caller (:mod:`devclaw.goal.triage`) — pure parse/validate/render,
  and the never-raises fail-toward-owner contract.
- The tick wiring (:func:`_maybe_alert_db_size` via ``tick_all``) — the DB-size
  alarm routes through triage when a caller is wired, falls back to the RAW ping
  on any triage failure, and — load-bearing — NEVER runs triage on an idle tick.
"""

from __future__ import annotations

import json

import pytest

from devclaw.goal import triage as _triage
from devclaw.goal.models import GoalStatus
from devclaw.goal.store import GoalStore
from devclaw.goal.tick import Outcome, tick_all
from devclaw.goal.tick_context import NotifyLevel, TRIAGE_ELIGIBLE, triaged_notify
from tests.goal_fakes import (
    Clock, FakeClaude, FakeEngine, RecordingNotifier, fake_prepare, seed_goal,
)

RAW_MSG = "🚨 devclaw.db has grown to 2.1 GB (incl. WAL) — retention isn't keeping up"

GOOD = json.dumps({
    "is_duplicate": False,
    "dedupe_note": "",
    "proposed_fix": "Set DEVCLAW_TRACE_RETENTION_DAYS=30 — it is currently disabled (0)",
    "approve_hint": "export DEVCLAW_TRACE_RETENTION_DAYS=30 and restart the service",
    "confidence": "high",
})

DUP = json.dumps({
    "is_duplicate": True,
    "dedupe_note": "seen 4× already — same retention-disabled root cause",
    "proposed_fix": "Set DEVCLAW_TRACE_RETENTION_DAYS=30",
    "approve_hint": "export DEVCLAW_TRACE_RETENTION_DAYS=30",
    "confidence": "high",
})


def _store(tmp_path):
    return GoalStore(tmp_path, now=Clock())


# ---- layer-3 caller: parse / validate / render -----------------------------


def test_validate_accepts_a_well_formed_proposal():
    p = _triage.validate(json.loads(GOOD))
    assert p.proposed_fix.startswith("Set DEVCLAW_TRACE_RETENTION_DAYS")
    assert p.approve_hint
    assert p.confidence == "high"
    assert p.is_duplicate is False


def test_validate_rejects_empty_proposed_fix():
    with pytest.raises(_triage.TriageError):
        _triage.validate({"proposed_fix": "   ", "approve_hint": "x"})


def test_validate_normalizes_unknown_confidence_to_low():
    p = _triage.validate({"proposed_fix": "do X", "confidence": "banana"})
    assert p.confidence == "low"


def test_extract_json_handles_fenced_and_bare():
    assert _triage.extract_json(GOOD).strip().startswith("{")
    fenced = f"here you go:\n```json\n{GOOD}\n```\nthanks"
    assert json.loads(_triage.extract_json(fenced))["confidence"] == "high"


def test_render_carries_raw_message_and_marks_proposal_not_applied():
    p = _triage.validate(json.loads(GOOD))
    out = _triage.render(p, RAW_MSG)
    assert RAW_MSG in out                          # original alert preserved verbatim
    assert "Proposed fix:" in out
    assert "DEVCLAW_TRACE_RETENTION_DAYS=30" in out
    assert "To approve:" in out
    assert "nothing has been changed" in out       # propose-only is explicit


def test_render_surfaces_dedupe_note_for_a_recurring_problem():
    p = _triage.validate(json.loads(DUP))
    out = _triage.render(p, RAW_MSG)
    assert "seen 4×" in out


def test_format_catalog_renders_rows_and_survives_bad_rows():
    rows = [
        {"category": "block", "kind": "needs_answer", "summary": "lost ref", "count": 3,
         "terminal_count": 3},
        {"bogus": "row"},  # missing keys — must not raise
    ]
    out = _triage.format_catalog(rows)
    assert "needs_answer" in out and "×3" in out


def test_retention_context_grounds_on_the_env_config(monkeypatch):
    monkeypatch.setenv("DEVCLAW_TRACE_RETENTION_DAYS", "0")     # the usual root cause
    monkeypatch.delenv("DEVCLAW_EVENTS_RETENTION_DAYS", raising=False)
    ctx = _triage.retention_context(2_100 * 1024 * 1024)
    assert "2100 MB" in ctx
    assert "DEVCLAW_TRACE_RETENTION_DAYS" in ctx
    assert "DISABLED" in ctx                                     # names the misconfig


@pytest.mark.asyncio
async def test_triage_returns_none_on_garbage_never_raises():
    caller = FakeClaude("this is not json at all", role="triage")
    p = await _triage.triage("problem", catalog="", repo_context="", caller=caller)
    assert p is None            # fail toward the owner — caller falls back to raw


@pytest.mark.asyncio
async def test_triage_returns_none_when_caller_raises():
    async def boom(_prompt):
        raise RuntimeError("cognition down")
    p = await _triage.triage("problem", catalog="", repo_context="", caller=boom)
    assert p is None


@pytest.mark.asyncio
async def test_triage_returns_parsed_proposal_on_good_output():
    caller = FakeClaude(GOOD, role="triage")
    p = await _triage.triage("problem", catalog="c", repo_context="r", caller=caller)
    assert p is not None and p.confidence == "high"


# ---- allowlist choke point --------------------------------------------------


@pytest.mark.asyncio
async def test_triaged_notify_only_intercepts_eligible_kinds():
    """A kind NOT on the allowlist bypasses triage entirely — raw ping, zero
    triage tokens — even when a caller is wired."""
    assert "db_size" in TRIAGE_ELIGIBLE
    notifier = RecordingNotifier()
    caller = FakeClaude(GOOD, role="triage")
    await triaged_notify(
        notifier, NotifyLevel.OWNER, RAW_MSG,
        kind="some_other_ping", triage_caller=caller, catalog="", repo_context="",
    )
    assert notifier.sent == [RAW_MSG]
    assert caller.calls == 0


@pytest.mark.asyncio
async def test_triaged_notify_none_caller_is_plain_notify():
    notifier = RecordingNotifier()
    await triaged_notify(
        notifier, NotifyLevel.OWNER, RAW_MSG,
        kind="db_size", triage_caller=None,
    )
    assert notifier.sent == [RAW_MSG]


# ---- tick wiring: the DB-size alarm route ----------------------------------


async def _tick(store, engine, notifier, *, triage_caller, planner=None, evaluator=None):
    planner = planner or FakeClaude("{}")
    evaluator = evaluator or FakeClaude("{}")
    return await tick_all(
        store=store, engine=engine, planner_caller=planner, evaluator_caller=evaluator,
        notifier=notifier, notify_url="http://relay", prepare_ws=fake_prepare,
        triage_caller=triage_caller,
    ), planner, evaluator


@pytest.mark.asyncio
async def test_triage_enriches_db_size_ping_with_proposed_fix(tmp_path):
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", workspace_dir="/repos/g")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    engine = FakeEngine(db_size_alert_msg=RAW_MSG, db_size_bytes_val=2_100 * 1024 * 1024)
    notifier = RecordingNotifier()
    triage = FakeClaude(GOOD, role="triage")

    (_out, planner, evaluator) = await _tick(store, engine, notifier, triage_caller=triage)

    assert len(notifier.sent) == 1
    enriched = notifier.sent[0]
    assert RAW_MSG in enriched                       # raw alert preserved
    assert "Proposed fix:" in enriched
    assert "DEVCLAW_TRACE_RETENTION_DAYS=30" in enriched
    assert "To approve:" in enriched
    assert triage.calls == 1                         # one triage call, on the real alert
    assert planner.calls == 0 and evaluator.calls == 0   # idle goal → no goal cognition


@pytest.mark.asyncio
async def test_triage_dedupes_db_size_against_catalog(tmp_path):
    store = _store(tmp_path)
    engine = FakeEngine(
        db_size_alert_msg=RAW_MSG, db_size_bytes_val=2_100 * 1024 * 1024,
        problems=[{"category": "other", "kind": "db_size", "summary": "db too large",
                   "count": 4, "terminal_count": 0}],
    )
    notifier = RecordingNotifier()
    triage = FakeClaude(DUP, role="triage")

    await _tick(store, engine, notifier, triage_caller=triage)

    assert "seen 4×" in notifier.sent[0]
    # the catalog was passed into the prompt (dedup substrate reached the model)
    assert "db_size" in triage.last_prompt


@pytest.mark.asyncio
async def test_triage_failure_falls_back_to_raw_db_size_ping(tmp_path):
    """Fail toward the owner: a triage failure never breaks the heartbeat and
    still delivers the ORIGINAL raw alert (loud, not silent)."""
    store = _store(tmp_path)
    engine = FakeEngine(db_size_alert_msg=RAW_MSG, db_size_bytes_val=2_100 * 1024 * 1024)
    notifier = RecordingNotifier()
    triage = FakeClaude("not json — triage is having a bad day", role="triage")

    (out, _p, _e) = await _tick(store, engine, notifier, triage_caller=triage)

    assert notifier.sent == [RAW_MSG]                # raw ping still delivered
    assert triage.calls == 1


@pytest.mark.asyncio
async def test_triage_never_runs_on_idle_tick_zero_tokens(tmp_path):
    """The zero-token idle guard: under threshold the alarm returns None, so
    triage is never invoked and no notification fires — FakeClaude.calls == 0
    across planner, evaluator, AND triage."""
    store = _store(tmp_path)
    seed_goal(tmp_path, "g", workspace_dir="/repos/g")
    store.save_status("g", GoalStatus(phase="idle", last_plan_at=store.now_iso()))
    engine = FakeEngine(db_size_alert_msg=None)     # under threshold — no alert
    notifier = RecordingNotifier()
    triage = FakeClaude(GOOD, role="triage")

    (out, planner, evaluator) = await _tick(store, engine, notifier, triage_caller=triage)

    assert out["g"] is Outcome.IDLE
    assert triage.calls == 0
    assert planner.calls == 0 and evaluator.calls == 0
    assert notifier.sent == []


@pytest.mark.asyncio
async def test_triage_disabled_delivers_raw_ping(tmp_path):
    """triage_caller=None (the default, and the DEVCLAW_SELF_TRIAGE=0 path) keeps
    the raw owner send byte-identical to before this feature existed."""
    store = _store(tmp_path)
    engine = FakeEngine(db_size_alert_msg=RAW_MSG)
    notifier = RecordingNotifier()

    await _tick(store, engine, notifier, triage_caller=None)

    assert notifier.sent == [RAW_MSG]
