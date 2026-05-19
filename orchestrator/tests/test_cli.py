"""Unit tests for the orchestrator CLI subcommands.

Covers --db routing in `dispatch`: postgres:// / postgresql:// schemes must
flow through postgres_checkpointer; anything else falls back to
sqlite_checkpointer (backward compat with on-disk paths).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from orchestrator import cli
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)


def _spec() -> TaskSpec:
    return TaskSpec(
        task_id="2026-05-18-cli-spec-aaaa",
        created_at=datetime(2026, 5, 18, 10, 0, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="tests"),
        verbatim_intent="noop for cli routing test",
        kind=TaskKind.code,
        target_repo="dsdevq/devclaw",
        acceptance_criteria=["noop"],
        budget=Budget(max_runtime_seconds=60),
        status=TaskStatus.ready,
    )


def _stub_graph_invoke():
    graph = mock.MagicMock()
    graph.invoke.return_value = {"spec": _spec(), "result": None, "error": None}
    return graph


@pytest.fixture
def spec_path(tmp_path: Path) -> Path:
    p = tmp_path / "spec.yaml"
    # cmd_dispatch calls load_spec(path); we patch it, but it still requires
    # the path to exist (the is_file() guard runs first).
    p.write_text("placeholder\n")
    return p


def test_dispatch_postgres_url_uses_postgres_checkpointer(spec_path: Path):
    conn = "postgres://user:pw@db.example.com:5432/orchestrator"
    args = mock.MagicMock(spec=["spec", "db", "thread_id"])
    args.spec = str(spec_path)
    args.db = conn
    args.thread_id = None

    with mock.patch.object(cli, "load_spec", return_value=_spec()) as m_load, \
         mock.patch.object(cli, "postgres_checkpointer") as m_pg, \
         mock.patch.object(cli, "sqlite_checkpointer") as m_sqlite, \
         mock.patch.object(cli, "build_task_graph", return_value=_stub_graph_invoke()) as m_build, \
         mock.patch.object(cli, "persist_spec"):
        m_pg.return_value = mock.sentinel.pg_checkpointer
        rc = cli.cmd_dispatch(args)

    assert rc == 0
    m_load.assert_called_once()
    m_pg.assert_called_once_with(conn)
    m_sqlite.assert_not_called()
    m_build.assert_called_once_with(checkpointer=mock.sentinel.pg_checkpointer)


def test_dispatch_postgresql_url_uses_postgres_checkpointer(spec_path: Path):
    conn = "postgresql://localhost/orchestrator"
    args = mock.MagicMock(spec=["spec", "db", "thread_id"])
    args.spec = str(spec_path)
    args.db = conn
    args.thread_id = None

    with mock.patch.object(cli, "load_spec", return_value=_spec()), \
         mock.patch.object(cli, "postgres_checkpointer") as m_pg, \
         mock.patch.object(cli, "sqlite_checkpointer") as m_sqlite, \
         mock.patch.object(cli, "build_task_graph", return_value=_stub_graph_invoke()), \
         mock.patch.object(cli, "persist_spec"):
        cli.cmd_dispatch(args)

    m_pg.assert_called_once_with(conn)
    m_sqlite.assert_not_called()


def test_dispatch_file_path_still_uses_sqlite(tmp_path: Path, spec_path: Path):
    db_path = tmp_path / "orch.sqlite"
    args = mock.MagicMock(spec=["spec", "db", "thread_id"])
    args.spec = str(spec_path)
    args.db = str(db_path)
    args.thread_id = None

    with mock.patch.object(cli, "load_spec", return_value=_spec()), \
         mock.patch.object(cli, "postgres_checkpointer") as m_pg, \
         mock.patch.object(cli, "sqlite_checkpointer") as m_sqlite, \
         mock.patch.object(cli, "build_task_graph", return_value=_stub_graph_invoke()), \
         mock.patch.object(cli, "persist_spec"):
        cli.cmd_dispatch(args)

    m_sqlite.assert_called_once_with(Path(str(db_path)).expanduser())
    m_pg.assert_not_called()


# ─── _openclaw_announce: subprocess wrapper used by events_announce ──────────


def test_openclaw_announce_logs_when_subprocess_returns_nonzero(caplog):
    import logging
    import subprocess as _subprocess

    fake_proc = mock.MagicMock()
    fake_proc.returncode = 2
    fake_proc.stderr = b"openclaw: bad request"

    with mock.patch.object(_subprocess, "run", return_value=fake_proc), \
         caplog.at_level(logging.WARNING, logger="orchestrator.daemon.announce"):
        # Must not raise.
        cli._openclaw_announce("telegram", "123", "hi")

    assert any(
        "openclaw message send rc=2" in rec.message for rec in caplog.records
    )


def test_openclaw_announce_swallows_subprocess_timeout(caplog):
    import logging
    import subprocess as _subprocess

    def _raise_timeout(*a, **kw):
        raise _subprocess.TimeoutExpired(cmd="openclaw", timeout=15)

    with mock.patch.object(_subprocess, "run", side_effect=_raise_timeout), \
         caplog.at_level(logging.WARNING, logger="orchestrator.daemon.announce"):
        cli._openclaw_announce("telegram", "123", "hi")  # must not raise

    assert any("openclaw message send failed" in rec.message for rec in caplog.records)


def test_openclaw_announce_swallows_oserror(caplog):
    import logging
    import subprocess as _subprocess

    with mock.patch.object(_subprocess, "run", side_effect=OSError("no such binary")), \
         caplog.at_level(logging.WARNING, logger="orchestrator.daemon.announce"):
        cli._openclaw_announce("telegram", "123", "hi")  # must not raise

    assert any("openclaw message send failed" in rec.message for rec in caplog.records)


# ─── _resolve_events_announce: env-var fallback chain ────────────────────────


def test_resolve_events_announce_uses_real_when_events_env_set(monkeypatch):
    monkeypatch.setenv("LIFEKIT_TELEGRAM_EVENTS_CHAT", "EVENTS-123")
    monkeypatch.delenv("LIFEKIT_TELEGRAM_CHAT", raising=False)
    fn, target = cli._resolve_events_announce()
    assert fn is cli._openclaw_announce
    assert target == "EVENTS-123"


def test_resolve_events_announce_falls_back_to_telegram_chat(monkeypatch):
    monkeypatch.delenv("LIFEKIT_TELEGRAM_EVENTS_CHAT", raising=False)
    monkeypatch.setenv("LIFEKIT_TELEGRAM_CHAT", "FALLBACK-456")
    fn, target = cli._resolve_events_announce()
    assert fn is cli._openclaw_announce
    assert target == "FALLBACK-456"


def test_resolve_events_announce_returns_noop_when_neither_env_set(monkeypatch):
    from orchestrator import events as events_mod

    monkeypatch.delenv("LIFEKIT_TELEGRAM_EVENTS_CHAT", raising=False)
    monkeypatch.delenv("LIFEKIT_TELEGRAM_CHAT", raising=False)
    fn, target = cli._resolve_events_announce()
    assert fn is events_mod._noop_announce
    assert target == "default"
