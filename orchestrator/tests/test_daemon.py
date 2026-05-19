"""Tests for the daemon scheduler.

We don't sleep wall-clock seconds — every test injects a tiny interval and
asserts the loop ticked the expected number of times before shutdown.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from orchestrator.audits.state_currency import AuditReport, RetiredHit
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


def _stub_audit_fn(report: AuditReport, life_root: Path):
    """Build an audit_fn that returns the given report and writes a dated stub file."""
    audits = life_root / "audits"
    audits.mkdir(parents=True, exist_ok=True)
    report_path = audits / f"{dt.date.today().isoformat()}-state-currency.md"

    def fake_audit(p: Path):
        report_path.write_text("stub report")
        return report, report_path

    return fake_audit, report_path


def test_audit_loop_clean_report_does_not_announce(tmp_path: Path) -> None:
    life = _make_life(tmp_path)
    clean = AuditReport(generated_at="2026-05-19T00:00:00+00:00")
    assert not clean.has_drift
    audit_fn, _ = _stub_audit_fn(clean, life)

    announce = MagicMock()
    shutdown = threading.Event()
    config = DaemonConfig(
        life_root=life,
        sweep_interval_s=60.0,
        supervise_interval_s=60.0,
        supervise_offset_s=60.0,
        audit_interval_s=0.05,
        audit_offset_s=0.0,
        telegram_chat="123456",
        announce=announce,
    )

    runner = threading.Thread(
        target=run_daemon,
        kwargs={
            "config": config,
            "shutdown": shutdown,
            "sweep_fn": lambda p: "scanned=0",
            "supervise_fn": lambda p, r: [],
            "audit_fn": audit_fn,
        },
    )
    runner.start()
    time.sleep(0.2)
    shutdown.set()
    runner.join(timeout=2.0)

    assert announce.call_count == 0, (
        f"announce must NOT fire on a clean report; got {announce.call_args_list!r}"
    )


def test_audit_loop_drift_report_announces_once(tmp_path: Path) -> None:
    life = _make_life(tmp_path)
    drift = AuditReport(
        generated_at="2026-05-19T00:00:00+00:00",
        retired_hits=[
            RetiredHit(
                term="swarm-langgraph",
                file="docs/architecture.md",
                line_no=42,
                line="the swarm-langgraph service…",
                replacement="devclaw-orchestrator",
                retired_on="2026-03-01",
                reason="container renamed",
            ),
            RetiredHit(
                term="swarm-langgraph",
                file="docs/architecture.md",
                line_no=99,
                line="another mention",
                replacement="devclaw-orchestrator",
                retired_on="2026-03-01",
                reason="container renamed",
            ),
        ],
    )
    assert drift.has_drift
    audit_fn, report_path = _stub_audit_fn(drift, life)

    announce = MagicMock()
    shutdown = threading.Event()
    config = DaemonConfig(
        life_root=life,
        sweep_interval_s=60.0,
        supervise_interval_s=60.0,
        supervise_offset_s=60.0,
        audit_interval_s=0.05,
        audit_offset_s=0.0,
        telegram_chat="987654",
        announce=announce,
    )

    runner = threading.Thread(
        target=run_daemon,
        kwargs={
            "config": config,
            "shutdown": shutdown,
            "sweep_fn": lambda p: "scanned=0",
            "supervise_fn": lambda p, r: [],
            "audit_fn": audit_fn,
        },
    )
    runner.start()
    time.sleep(0.25)
    shutdown.set()
    runner.join(timeout=2.0)

    # Idempotent per day: even though _audit_loop iterates many times in 0.25s, only
    # the first tick (when no prior `<today>-state-currency.md` exists) announces.
    assert announce.call_count == 1, (
        f"expected exactly one announce, got {announce.call_count}: "
        f"{announce.call_args_list!r}"
    )
    channel, target, message = announce.call_args.args
    assert channel == "telegram"
    assert target == "987654"
    assert "2" in message  # retired-hit count
    assert str(report_path) in message
    assert len(message) <= 500


# ─── events_announce wiring (PR-#21 audit announce unchanged) ────────────────


def test_daemon_default_sweep_threads_events_announce_into_sweep_once(tmp_path: Path):
    """run_daemon's default sweep_fn must close over config.events_announce/events_chat
    so task-lifecycle events fire from inside sweep_once."""
    from unittest.mock import patch

    life = _make_life(tmp_path)
    events_announce = MagicMock()

    captured: dict = {}

    def fake_sweep_once(life_root, **kwargs):  # noqa: ARG001
        captured["events_announce"] = kwargs.get("events_announce")
        captured["events_chat"] = kwargs.get("events_chat")

        class _R:
            def summary(self) -> str:
                return "scanned=0"

        return _R()

    shutdown = threading.Event()
    config = DaemonConfig(
        life_root=life,
        sweep_interval_s=0.05,
        supervise_interval_s=60.0,
        supervise_offset_s=60.0,
        audit_interval_s=60.0,
        audit_offset_s=60.0,
        pr_review_interval_s=60.0,
        pr_review_offset_s=60.0,
        events_announce=events_announce,
        events_chat="EVENTS-987",
    )

    with patch("orchestrator.daemon.sweep_once", side_effect=fake_sweep_once):
        runner = threading.Thread(
            target=run_daemon,
            kwargs={
                "config": config,
                "shutdown": shutdown,
                # supervise/audit/pr_review stubbed out — only sweep matters here
                "supervise_fn": lambda p, r: [],
                "audit_fn": lambda p: (
                    AuditReport(generated_at="2026-05-19T00:00:00+00:00"),
                    p / "audits" / "stub.md",
                ),
                "pr_review_fn": lambda p: type(
                    "R", (), {"merged": [], "considered": [], "circuit_paused": False}
                )(),
            },
        )
        runner.start()
        time.sleep(0.15)
        shutdown.set()
        runner.join(timeout=2.0)

    assert captured.get("events_announce") is events_announce
    assert captured.get("events_chat") == "EVENTS-987"


def test_daemon_events_announce_does_not_collide_with_audit_announce(tmp_path: Path):
    """events_announce and the PR-#21 audit `announce` are independent callbacks.
    Firing events from sweep must not invoke `audit.announce`, and vice versa."""
    life = _make_life(tmp_path)
    drift = AuditReport(
        generated_at="2026-05-19T00:00:00+00:00",
        retired_hits=[
            RetiredHit(
                term="swarm-langgraph",
                file="docs/architecture.md",
                line_no=1,
                line="x",
                replacement="devclaw-orchestrator",
                retired_on="2026-03-01",
                reason="renamed",
            )
        ],
    )
    audit_fn, _ = _stub_audit_fn(drift, life)

    audit_announce = MagicMock()
    events_announce = MagicMock()

    shutdown = threading.Event()
    config = DaemonConfig(
        life_root=life,
        sweep_interval_s=60.0,
        supervise_interval_s=60.0,
        supervise_offset_s=60.0,
        audit_interval_s=0.05,
        audit_offset_s=0.0,
        announce=audit_announce,
        events_announce=events_announce,
        telegram_chat="AUDIT-CHAT",
        events_chat="EVENTS-CHAT",
    )

    runner = threading.Thread(
        target=run_daemon,
        kwargs={
            "config": config,
            "shutdown": shutdown,
            "sweep_fn": lambda p: "scanned=0",
            "supervise_fn": lambda p, r: [],
            "audit_fn": audit_fn,
        },
    )
    runner.start()
    time.sleep(0.2)
    shutdown.set()
    runner.join(timeout=2.0)

    # Audit drift announces to AUDIT-CHAT via the audit `announce` field, NOT events_announce.
    assert audit_announce.call_count == 1
    assert audit_announce.call_args.args[1] == "AUDIT-CHAT"
    assert events_announce.call_count == 0
