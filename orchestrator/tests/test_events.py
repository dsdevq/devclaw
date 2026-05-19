"""Tests for `orchestrator.events` — the task-lifecycle Telegram announce layer.

Each of the 5 event formats has at least one unit test pinning its exact wire shape, plus tests for:
  - LIFEKIT_TELEGRAM_EVENTS_CHAT > LIFEKIT_TELEGRAM_CHAT > default precedence
  - 300-char cap with ellipsis truncation
  - `_safe_call` swallows callback exceptions (never raises into the caller)
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from orchestrator.events import (
    EVENTS_CHANNEL,
    EVENTS_CHAT_ENV,
    FALLBACK_CHAT_ENV,
    MAX_SUMMARY_CHARS,
    _noop_announce,
    _safe_call,
    _truncate,
    emit_dispatched,
    emit_done,
    emit_spec_created,
    emit_terminal_failure,
    resolve_events_chat,
)

# ─── env var resolution ──────────────────────────────────────────────────────


def test_resolve_events_chat_prefers_events_env(monkeypatch):
    monkeypatch.setenv(EVENTS_CHAT_ENV, "111")
    monkeypatch.setenv(FALLBACK_CHAT_ENV, "222")
    assert resolve_events_chat(default="333") == "111"


def test_resolve_events_chat_falls_back_to_telegram_chat(monkeypatch):
    monkeypatch.delenv(EVENTS_CHAT_ENV, raising=False)
    monkeypatch.setenv(FALLBACK_CHAT_ENV, "222")
    assert resolve_events_chat(default="333") == "222"


def test_resolve_events_chat_uses_default_when_neither_set(monkeypatch):
    monkeypatch.delenv(EVENTS_CHAT_ENV, raising=False)
    monkeypatch.delenv(FALLBACK_CHAT_ENV, raising=False)
    assert resolve_events_chat(default="fallback") == "fallback"


# ─── truncation ──────────────────────────────────────────────────────────────


def test_truncate_passthrough_when_under_cap():
    assert _truncate("hello", cap=10) == "hello"


def test_truncate_appends_ellipsis_when_over_cap():
    text = "x" * 400
    out = _truncate(text, cap=MAX_SUMMARY_CHARS)
    assert len(out) == MAX_SUMMARY_CHARS
    assert out.endswith("…")


# ─── safe_call ──────────────────────────────────────────────────────────────


def test_safe_call_swallows_exceptions(caplog):
    def boom(channel, target, message):
        raise RuntimeError("kaboom")

    with caplog.at_level(logging.WARNING, logger="orchestrator.events"):
        _safe_call(boom, "telegram", "123", "hi")
    # _safe_call must not propagate
    assert any("events_announce failed" in rec.message for rec in caplog.records)


# ─── event 1: spec_created ───────────────────────────────────────────────────


def test_emit_spec_created_with_target_repo():
    announce = MagicMock()
    emit_spec_created(
        announce,
        "987",
        task_id="2026-05-19-task-abcd",
        target_repo="dsdevq/devclaw",
    )
    announce.assert_called_once_with(
        EVENTS_CHANNEL,
        "987",
        "📋 Queued: 2026-05-19-task-abcd → dsdevq/devclaw",
    )


def test_emit_spec_created_without_target_repo_uses_project_less_label():
    announce = MagicMock()
    emit_spec_created(
        announce,
        "987",
        task_id="2026-05-19-research-xyz",
        target_repo=None,
    )
    args = announce.call_args.args
    assert args[2] == "📋 Queued: 2026-05-19-research-xyz → (project-less)"


# ─── event 2: dispatched ─────────────────────────────────────────────────────


def test_emit_dispatched_carries_runner_kind():
    announce = MagicMock()
    emit_dispatched(
        announce,
        "987",
        task_id="2026-05-19-task-abcd",
        runner_kind="subagent",
    )
    announce.assert_called_once_with(
        EVENTS_CHANNEL,
        "987",
        "🚀 Dispatched: 2026-05-19-task-abcd (subagent)",
    )


# ─── event 3/4: done ─────────────────────────────────────────────────────────


def test_emit_done_with_pr_url_includes_url_on_second_line():
    announce = MagicMock()
    emit_done(
        announce,
        "987",
        task_id="t-1",
        pr_url="https://github.com/dsdevq/devclaw/pull/22",
    )
    args = announce.call_args.args
    assert args[2] == "✅ Done: t-1\nhttps://github.com/dsdevq/devclaw/pull/22"


def test_emit_done_without_pr_url_is_single_line():
    announce = MagicMock()
    emit_done(announce, "987", task_id="t-1", pr_url=None)
    args = announce.call_args.args
    assert args[2] == "✅ Done: t-1"


# ─── event 5: terminal failure ───────────────────────────────────────────────


def test_emit_terminal_failure_failed():
    announce = MagicMock()
    emit_terminal_failure(
        announce,
        "987",
        task_id="t-1",
        new_state="failed",
        reason="verification_failed",
    )
    args = announce.call_args.args
    assert args[2] == "❌ failed: t-1\nverification_failed"


def test_emit_terminal_failure_abandoned():
    announce = MagicMock()
    emit_terminal_failure(
        announce,
        "987",
        task_id="t-1",
        new_state="abandoned",
        reason=None,
    )
    args = announce.call_args.args
    assert args[2] == "❌ abandoned: t-1\nno reason captured"


def test_emit_terminal_failure_long_reason_is_truncated():
    announce = MagicMock()
    long_reason = "x" * 1000
    emit_terminal_failure(
        announce,
        "987",
        task_id="t-1",
        new_state="failed",
        reason=long_reason,
    )
    args = announce.call_args.args
    assert len(args[2]) <= MAX_SUMMARY_CHARS
    assert args[2].endswith("…")


def test_emit_terminal_failure_empty_string_reason_is_treated_as_no_reason():
    announce = MagicMock()
    emit_terminal_failure(
        announce,
        "987",
        task_id="t-1",
        new_state="failed",
        reason="   ",  # whitespace-only
    )
    args = announce.call_args.args
    assert "no reason captured" in args[2]


# ─── default no-op behaviour ─────────────────────────────────────────────────


def test_noop_announce_logs_at_info(caplog):
    with caplog.at_level(logging.INFO, logger="orchestrator.events"):
        _noop_announce("telegram", "123", "hi there")
    assert any("hi there" in rec.message for rec in caplog.records)
