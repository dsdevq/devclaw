"""Unit tests for the orchestrator.events emitters.

Each of the five lifecycle events has its own emitter; this file exercises
formatting, truncation, default-channel selection, env-var fallback chain,
and the safe-fire contract (never raises if the callback does).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from orchestrator import events

# ─── resolve_events_chat: env-var fallback chain ─────────────────────────────


def test_resolve_events_chat_prefers_events_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(events.ENV_EVENTS_CHAT, "111")
    monkeypatch.setenv(events.ENV_FALLBACK_CHAT, "222")
    assert events.resolve_events_chat() == "111"


def test_resolve_events_chat_falls_back_to_shared_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(events.ENV_EVENTS_CHAT, raising=False)
    monkeypatch.setenv(events.ENV_FALLBACK_CHAT, "333")
    assert events.resolve_events_chat() == "333"


def test_resolve_events_chat_default_when_both_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(events.ENV_EVENTS_CHAT, raising=False)
    monkeypatch.delenv(events.ENV_FALLBACK_CHAT, raising=False)
    assert events.resolve_events_chat(default="zzz") == "zzz"


# ─── emit_queued ─────────────────────────────────────────────────────────────


def test_emit_queued_with_target_repo():
    announce = MagicMock()
    events.emit_queued(
        task_id="2026-05-20-abc",
        target_repo="dsdevq/devclaw",
        chat_id="42",
        announce=announce,
    )
    announce.assert_called_once()
    channel, target, msg = announce.call_args.args
    assert channel == "telegram"
    assert target == "42"
    assert msg == "📋 Queued: 2026-05-20-abc → dsdevq/devclaw"


def test_emit_queued_without_target_repo_uses_placeholder():
    announce = MagicMock()
    events.emit_queued(
        task_id="abc",
        target_repo=None,
        chat_id="42",
        announce=announce,
    )
    msg = announce.call_args.args[2]
    assert "(project-less)" in msg
    assert msg == "📋 Queued: abc → (project-less)"


# ─── emit_dispatched ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["subagent", "build", "human"])
def test_emit_dispatched_includes_runner_kind(kind: str):
    announce = MagicMock()
    events.emit_dispatched(
        task_id="t1",
        runner_kind=kind,
        chat_id="9",
        announce=announce,
    )
    msg = announce.call_args.args[2]
    assert msg == f"🚀 Dispatched: t1 ({kind})"


# ─── emit_done ──────────────────────────────────────────────────────────────


def test_emit_done_with_pr_url():
    announce = MagicMock()
    events.emit_done(
        task_id="t2",
        pr_url="https://github.com/dsdevq/devclaw/pull/99",
        chat_id="9",
        announce=announce,
    )
    msg = announce.call_args.args[2]
    assert (
        msg
        == "✅ Done: t2\nhttps://github.com/dsdevq/devclaw/pull/99"
    )


def test_emit_done_without_pr_url():
    announce = MagicMock()
    events.emit_done(
        task_id="t3",
        pr_url=None,
        chat_id="9",
        announce=announce,
    )
    assert announce.call_args.args[2] == "✅ Done: t3"


# ─── emit_terminal_failure ───────────────────────────────────────────────────


def test_emit_terminal_failure_with_reason():
    announce = MagicMock()
    events.emit_terminal_failure(
        task_id="t4",
        new_state="blocked",
        reason="tests_failed",
        chat_id="9",
        announce=announce,
    )
    assert announce.call_args.args[2] == "❌ blocked: t4\ntests_failed"


def test_emit_terminal_failure_with_none_reason_uses_placeholder():
    announce = MagicMock()
    events.emit_terminal_failure(
        task_id="t5",
        new_state="abandoned",
        reason=None,
        chat_id="9",
        announce=announce,
    )
    assert announce.call_args.args[2] == "❌ abandoned: t5\nno reason captured"


def test_emit_terminal_failure_with_blank_reason_uses_placeholder():
    announce = MagicMock()
    events.emit_terminal_failure(
        task_id="t6",
        new_state="failed",
        reason="   ",
        chat_id="9",
        announce=announce,
    )
    assert announce.call_args.args[2] == "❌ failed: t6\nno reason captured"


# ─── truncation guarantee ────────────────────────────────────────────────────


def test_truncation_caps_at_300_chars():
    announce = MagicMock()
    long_reason = "x" * 1000
    events.emit_terminal_failure(
        task_id="big",
        new_state="blocked",
        reason=long_reason,
        chat_id="9",
        announce=announce,
    )
    msg = announce.call_args.args[2]
    assert len(msg) <= 300
    assert msg.endswith("…")


def test_truncation_leaves_short_messages_untouched():
    announce = MagicMock()
    events.emit_queued(
        task_id="short",
        target_repo="org/repo",
        chat_id="9",
        announce=announce,
    )
    msg = announce.call_args.args[2]
    assert not msg.endswith("…")
    assert len(msg) < 100


# ─── safe-fire contract ──────────────────────────────────────────────────────


def test_announce_raising_does_not_propagate(caplog: pytest.LogCaptureFixture):
    def bad_announce(channel: str, target: str, message: str) -> None:
        raise RuntimeError("transport broken")

    with caplog.at_level(logging.WARNING, logger="orchestrator.events"):
        events.emit_queued(
            task_id="x",
            target_repo=None,
            chat_id="9",
            announce=bad_announce,
        )
    assert any("events announce failed" in r.message for r in caplog.records)


def test_default_announce_is_noop():
    """Calling without an `announce=` kwarg must not raise."""
    events.emit_queued(task_id="x", target_repo="o/r", chat_id="9")
    events.emit_dispatched(task_id="x", runner_kind="subagent", chat_id="9")
    events.emit_done(task_id="x", pr_url=None, chat_id="9")
    events.emit_terminal_failure(task_id="x", new_state="blocked", reason=None, chat_id="9")
