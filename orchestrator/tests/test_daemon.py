"""Tests for the daemon scheduler.

We don't sleep wall-clock seconds — every test injects a tiny interval and
asserts the loop ticked the expected number of times before shutdown.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from orchestrator.daemon import DaemonConfig, run_daemon
from orchestrator.state.models import RequesterRoute


def _make_life(tmp_path: Path) -> Path:
    (tmp_path / "system").mkdir()
    (tmp_path / "tasks").mkdir()
    (tmp_path / "projects").mkdir()
    return tmp_path


def test_run_daemon_ticks_both_loops(tmp_path: Path) -> None:
    life = _make_life(tmp_path)
    sweep_calls: list[Path] = []
    super_calls: list[Path] = []

    def fake_sweep(p: Path) -> object:
        sweep_calls.append(p)
        return "scanned=0"

    def fake_supervise(p: Path, _route: RequesterRoute) -> list[str]:
        super_calls.append(p)
        return []

    shutdown = threading.Event()
    config = DaemonConfig(
        life_root=life,
        sweep_interval_s=0.05,
        supervise_interval_s=0.05,
        supervise_offset_s=0.0,
    )

    runner = threading.Thread(
        target=run_daemon,
        kwargs={
            "config": config,
            "shutdown": shutdown,
            "sweep_fn": fake_sweep,
            "supervise_fn": fake_supervise,
        },
    )
    runner.start()
    time.sleep(0.2)
    shutdown.set()
    runner.join(timeout=2.0)

    assert not runner.is_alive(), "daemon did not stop on shutdown event"
    assert len(sweep_calls) >= 2, f"expected ≥2 sweep ticks, got {len(sweep_calls)}"
    assert len(super_calls) >= 2, f"expected ≥2 supervise ticks, got {len(super_calls)}"
    assert all(c == life for c in sweep_calls + super_calls)


def test_run_daemon_killswitch_skips_both_loops(tmp_path: Path) -> None:
    life = _make_life(tmp_path)
    (life / "system" / "cron-paused").touch()

    sweep_calls: list[Path] = []
    super_calls: list[Path] = []

    def fake_sweep(p: Path) -> object:
        sweep_calls.append(p)
        return "scanned=0"

    def fake_supervise(p: Path, _route: RequesterRoute) -> list[str]:
        super_calls.append(p)
        return []

    shutdown = threading.Event()
    config = DaemonConfig(
        life_root=life,
        sweep_interval_s=0.05,
        supervise_interval_s=0.05,
        supervise_offset_s=0.0,
    )
    runner = threading.Thread(
        target=run_daemon,
        kwargs={
            "config": config,
            "shutdown": shutdown,
            "sweep_fn": fake_sweep,
            "supervise_fn": fake_supervise,
        },
    )
    runner.start()
    time.sleep(0.15)
    shutdown.set()
    runner.join(timeout=2.0)

    assert sweep_calls == []
    assert super_calls == []


def test_run_daemon_swallows_exceptions(tmp_path: Path) -> None:
    """A single failing tick must not crash the daemon — the loop must continue."""
    life = _make_life(tmp_path)

    sweep_calls = 0
    super_calls = 0

    def flaky_sweep(p: Path) -> object:
        nonlocal sweep_calls
        sweep_calls += 1
        if sweep_calls == 1:
            raise RuntimeError("boom")
        return "scanned=0"

    def flaky_supervise(p: Path, _route: RequesterRoute) -> list[str]:
        nonlocal super_calls
        super_calls += 1
        if super_calls == 1:
            raise RuntimeError("kaboom")
        return []

    shutdown = threading.Event()
    config = DaemonConfig(
        life_root=life,
        sweep_interval_s=0.05,
        supervise_interval_s=0.05,
        supervise_offset_s=0.0,
    )
    runner = threading.Thread(
        target=run_daemon,
        kwargs={
            "config": config,
            "shutdown": shutdown,
            "sweep_fn": flaky_sweep,
            "supervise_fn": flaky_supervise,
        },
    )
    runner.start()
    time.sleep(0.25)
    shutdown.set()
    runner.join(timeout=2.0)

    assert sweep_calls >= 2, "daemon did not retry sweep after exception"
    assert super_calls >= 2, "daemon did not retry supervise after exception"


def test_run_daemon_supervise_offset_delays_first_tick(tmp_path: Path) -> None:
    life = _make_life(tmp_path)
    sweep_calls = 0
    super_calls = 0

    def fake_sweep(p: Path) -> object:
        nonlocal sweep_calls
        sweep_calls += 1
        return "scanned=0"

    def fake_supervise(p: Path, _route: RequesterRoute) -> list[str]:
        nonlocal super_calls
        super_calls += 1
        return []

    shutdown = threading.Event()
    config = DaemonConfig(
        life_root=life,
        sweep_interval_s=0.05,
        supervise_interval_s=0.05,
        supervise_offset_s=0.5,
    )
    runner = threading.Thread(
        target=run_daemon,
        kwargs={
            "config": config,
            "shutdown": shutdown,
            "sweep_fn": fake_sweep,
            "supervise_fn": fake_supervise,
        },
    )
    runner.start()
    time.sleep(0.15)
    sweep_before_offset = sweep_calls
    super_before_offset = super_calls
    shutdown.set()
    runner.join(timeout=2.0)

    assert sweep_before_offset >= 1
    assert super_before_offset == 0, "supervise tick fired before its offset window"
