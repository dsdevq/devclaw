"""measure_passrate drain-vs-pause seam — the eval runner must wait out an
account-wide pause instead of recording paused tickets as pending/0.0s.

Found live on the 2026-07-20 baseline run: a rate_limit correctly paused the
queue and requeued the ticket (July pause machinery working as designed), but
``queue.drain()`` only awaits in-flight work — with dispatch gated and nothing
re-pumping, drain returned immediately and 6 of 8 tickets were recorded
pending/0.0s. The June runner had simply never met the July pause machinery."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from evals import measure_passrate as mp


class FakeStore:
    """Scripted store double: a pending task that settles 'done' on the first
    pump AFTER the pause has elapsed (mirroring the real queue's behavior)."""

    def __init__(self, pause_until_ms: float, reason: str = "rate_limit: 429",
                 settles_on_pump: bool = True) -> None:
        self.status = "pending"
        self.pause = (pause_until_ms, reason)
        self.settles_on_pump = settles_on_pump

    def get_task(self, tid: str):
        return SimpleNamespace(status=self.status)

    def global_pause(self):
        return self.pause

    def on_pump(self) -> None:
        if self.settles_on_pump and self.pause[0] <= time.time() * 1000:
            self.status = "done"
            self.pause = (0, "")


class FakeQueue:
    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.pumps = 0

    async def drain(self) -> None:
        return None

    def pump(self) -> None:
        self.pumps += 1
        self.store.on_pump()


async def test_settle_waits_out_queue_pause_instead_of_recording_pending(monkeypatch):
    store = FakeStore(pause_until_ms=time.time() * 1000 + 60_000)
    queue = FakeQueue(store)
    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(s)
        # simulate the pause window elapsing while we slept
        store.pause = (time.time() * 1000 - 1, store.pause[1])

    monkeypatch.setattr(mp.asyncio, "sleep", fake_sleep)
    await mp._settle(queue, store, "t1")

    assert store.status == "done"          # settled, not abandoned pending
    assert sleeps and sleeps[0] >= 60      # actually waited the pause out
    assert queue.pumps >= 1                # re-dispatched after the pause lifted


async def test_settle_refuses_to_hang_when_dispatch_is_stuck(monkeypatch):
    # pending forever with NO active pause is a structural dispatch bug — the
    # runner must fail loudly instead of spinning for the rest of the night
    store = FakeStore(pause_until_ms=0, settles_on_pump=False)
    store.pause = (0, "")
    queue = FakeQueue(store)

    async def fake_sleep(s: float) -> None:
        return None

    monkeypatch.setattr(mp.asyncio, "sleep", fake_sleep)
    with pytest.raises(RuntimeError, match="pending"):
        await mp._settle(queue, store, "t1")
