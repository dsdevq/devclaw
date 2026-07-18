"""StateStore.maybe_vacuum — reclaim the disk the retention prunes free.

VACUUM (volume hygiene, 2026-07-18) rebuilds the .db file so pages freed by the
trace/events retention prunes actually return to the OS. It runs weekly, only
when the freelist is worth reclaiming, and never inside an open transaction.
"""

from __future__ import annotations

import pytest

from devclaw.state_store import StateStore
from devclaw.state_store.core import _VACUUM_META_KEY

_NOW = 1_800_000_000_000
_WEEK_MS = 7 * 24 * 3600 * 1000


@pytest.fixture
def store(tmp_path):
    return StateStore(str(tmp_path / "vacuum.db"))


def _freelist(store) -> int:
    return int(store._db.execute("PRAGMA freelist_count").fetchone()[0])


def _make_freelist(store, rows: int = 3000) -> int:
    """Insert then delete many rows so their pages land on the freelist —
    exactly the state a big retention prune leaves behind."""
    for _ in range(rows):
        store.append_event(
            task_id="t", program_id=None, type="x", source="s",
            payload_json="p" * 512, ts=1,
        )
    store._db.commit()
    store._db.execute("DELETE FROM events")
    store._db.commit()
    return _freelist(store)


def test_vacuum_runs_and_reclaims_when_freelist_exceeds_threshold(store):
    freed = _make_freelist(store)
    assert freed > 0  # the deletes really did strand pages
    assert store.maybe_vacuum(now_ms=_NOW, min_freelist_pages=1) is True
    assert _freelist(store) == 0  # VACUUM returned every free page to the file


def test_vacuum_skipped_when_freelist_below_threshold(store):
    _make_freelist(store)  # some freelist, but we demand far more
    assert store.maybe_vacuum(now_ms=_NOW, min_freelist_pages=10_000_000) is False
    # Still stamped: "checked, not worth it" — don't re-inspect every tick.
    assert store.get_meta(_VACUUM_META_KEY) == str(_NOW)


def test_vacuum_runs_at_most_once_per_interval(store):
    _make_freelist(store)
    assert store.maybe_vacuum(now_ms=_NOW, min_freelist_pages=1) is True
    # Fresh freelist appears, but the weekly watermark gates the next cycle...
    _make_freelist(store)
    assert store.maybe_vacuum(now_ms=_NOW + 3600 * 1000, min_freelist_pages=1) is False
    # ...until a week has passed.
    assert store.maybe_vacuum(now_ms=_NOW + _WEEK_MS + 1, min_freelist_pages=1) is True


def test_vacuum_skipped_inside_open_transaction(store):
    """VACUUM cannot run inside a transaction — maybe_vacuum defers and does NOT
    stamp the watermark, so a later (out-of-transaction) tick still reclaims."""
    _make_freelist(store)
    with store.transaction():
        assert store.maybe_vacuum(now_ms=_NOW, min_freelist_pages=1) is False
    # Watermark untouched → the deferred cycle runs on the next attempt.
    assert store.get_meta(_VACUUM_META_KEY) is None
    assert store.maybe_vacuum(now_ms=_NOW, min_freelist_pages=1) is True


def test_vacuum_failure_leaves_watermark_unstamped_for_retry(store, monkeypatch):
    """A VACUUM that RAISES (e.g. not enough scratch disk) must not stamp the
    watermark — otherwise a transient failure defers the retry a full week. The
    below-threshold check stamps; a real-but-failed rewrite does not."""
    _make_freelist(store)

    class _VacuumBoom:
        """Proxy the real connection but blow up on VACUUM (a C sqlite3
        Connection won't accept setattr on its methods, so wrap the whole thing)."""

        def __init__(self, real):
            self._real = real

        def execute(self, sql, *a, **k):
            if sql.strip().upper().startswith("VACUUM"):
                raise RuntimeError("database or disk is full")
            return self._real.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(store, "_db", _VacuumBoom(store._db))
    with pytest.raises(RuntimeError):
        store.maybe_vacuum(now_ms=_NOW, min_freelist_pages=1)
    assert store.get_meta(_VACUUM_META_KEY) is None  # untouched → retries next tick


def test_engine_exposes_vacuum_seam(store):
    """The goal heartbeat reaches VACUUM through the engine (getattr seam, same
    as the prune accessors) — InProcessEngine must delegate to maybe_vacuum."""
    from devclaw.goal.engine import InProcessEngine
    from devclaw.task_queue import TaskQueue

    _make_freelist(store)
    engine = InProcessEngine(TaskQueue(store), store)
    # The real seam uses the weekly default interval + freelist threshold; a
    # freshly-freed test DB won't hit the 10k-page floor, so this exercises the
    # delegation + returns False (nothing worth reclaiming), not a real rewrite.
    assert engine.vacuum() is False
