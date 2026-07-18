"""StateStore.check_db_size_alert — the loud-not-silent DB-size alarm.

Converts a silent disk-fill wedge into ONE owner ping when devclaw.db crosses a
threshold, re-arming when it drops back under (volume hygiene, 2026-07-18).
"""

from __future__ import annotations

import pytest

from devclaw.state_store import StateStore
from devclaw.state_store.core import (
    DB_SIZE_ALERT_MB_DEFAULT,
    _DB_SIZE_ALERTED_META_KEY,
    db_size_alert_bytes,
)

_MB = 1024 * 1024


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "size.db"))


def test_db_size_bytes_is_positive_for_a_real_store(store):
    store.set_meta("x", "y")  # force at least one page written
    assert store.db_size_bytes() > 0


def test_alert_fires_once_over_threshold_then_dedupes(store):
    # A 1-byte threshold: any real .db is "over".
    msg = store.check_db_size_alert(threshold_bytes=1)
    assert msg is not None and "devclaw.db" in msg
    assert store.get_meta(_DB_SIZE_ALERTED_META_KEY) == "1"
    # Same episode → silent (one ping per crossing, not one per tick).
    assert store.check_db_size_alert(threshold_bytes=1) is None


def test_alert_rearms_after_dropping_back_under_threshold(store):
    assert store.check_db_size_alert(threshold_bytes=1) is not None      # fires
    assert store.check_db_size_alert(threshold_bytes=1) is None          # deduped
    # Size now "under" a huge threshold → clears the flag (re-arm), no ping.
    assert store.check_db_size_alert(threshold_bytes=10**15) is None
    assert store.get_meta(_DB_SIZE_ALERTED_META_KEY) is None
    # A later re-crossing pings again.
    assert store.check_db_size_alert(threshold_bytes=1) is not None


def test_alert_disabled_when_threshold_non_positive(store):
    assert store.check_db_size_alert(threshold_bytes=0) is None
    assert store.check_db_size_alert(threshold_bytes=-5) is None
    assert store.get_meta(_DB_SIZE_ALERTED_META_KEY) is None  # never armed


def test_db_size_alert_bytes_env_parsing(monkeypatch):
    monkeypatch.delenv("DEVCLAW_DB_SIZE_ALERT_MB", raising=False)
    assert db_size_alert_bytes() == DB_SIZE_ALERT_MB_DEFAULT * _MB   # unset → default
    monkeypatch.setenv("DEVCLAW_DB_SIZE_ALERT_MB", "500")
    assert db_size_alert_bytes() == 500 * _MB
    monkeypatch.setenv("DEVCLAW_DB_SIZE_ALERT_MB", "0")
    assert db_size_alert_bytes() == 0                                 # explicit off
    monkeypatch.setenv("DEVCLAW_DB_SIZE_ALERT_MB", "-3")
    assert db_size_alert_bytes() == 0                                 # negative → off
    monkeypatch.setenv("DEVCLAW_DB_SIZE_ALERT_MB", "big")
    assert db_size_alert_bytes() == 0                                 # unparseable → off
    monkeypatch.setenv("DEVCLAW_DB_SIZE_ALERT_MB", "   ")
    assert db_size_alert_bytes() == DB_SIZE_ALERT_MB_DEFAULT * _MB    # blank → default


def test_default_env_disabled_leaves_healthy_db_silent(store, monkeypatch):
    """With the real default threshold (2GB), a tiny test .db never pings —
    the alarm only fires on a genuinely oversized DB."""
    monkeypatch.delenv("DEVCLAW_DB_SIZE_ALERT_MB", raising=False)
    assert store.check_db_size_alert() is None


def test_engine_exposes_db_size_alert_seam(store, monkeypatch):
    """The heartbeat reaches the alarm through the engine (getattr seam, same as
    vacuum/prune). Default threshold → a small DB returns None."""
    from devclaw.goal.engine import InProcessEngine
    from devclaw.task_queue import TaskQueue

    monkeypatch.delenv("DEVCLAW_DB_SIZE_ALERT_MB", raising=False)
    engine = InProcessEngine(TaskQueue(store), store)
    assert engine.check_db_size_alert() is None
