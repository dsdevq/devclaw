"""Task-lifecycle Telegram announces.

Fires on five orchestrator state transitions so an operator with a phone
sees what the daemon is doing without tailing logs:

  1. task_intake     → spec_created             ("📋 Queued: ...")
  2. task_dispatch   → dispatched-*             ("🚀 Dispatched: ...")
  3. task_runner     → done WITH pr_url         ("✅ Done: ...\\n<url>")
  4. task_runner     → done WITHOUT pr_url      ("✅ Done: ...")
  5. task_runner     → failed | abandoned       ("❌ <state>: ...\\n<reason>")

Reuses PR #21's `AnnounceCallback` (channel, target, message). The events
module is purely a *formatter + safe-firer* — it never touches the network.
Wiring to the real `openclaw message send` subprocess lives in `cli.py`.
"""

from __future__ import annotations

import logging
import os

from orchestrator.daemon import AnnounceCallback, _noop_announce

logger = logging.getLogger(__name__)

SUMMARY_CAP = 300

ENV_EVENTS_CHAT = "LIFEKIT_TELEGRAM_EVENTS_CHAT"
ENV_FALLBACK_CHAT = "LIFEKIT_TELEGRAM_CHAT"

DEFAULT_CHANNEL = "telegram"


def resolve_events_chat(default: str = "default") -> str:
    """Pick the Telegram chat id for lifecycle events.

    Resolution order:
      1. `LIFEKIT_TELEGRAM_EVENTS_CHAT` (lifecycle-specific override)
      2. `LIFEKIT_TELEGRAM_CHAT`        (shared fallback)
      3. `default`                      (caller-supplied last resort)
    """
    return (
        os.environ.get(ENV_EVENTS_CHAT)
        or os.environ.get(ENV_FALLBACK_CHAT)
        or default
    )


def _truncate(text: str, cap: int = SUMMARY_CAP) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 1] + "…"


def _safe_fire(
    announce: AnnounceCallback,
    channel: str,
    chat_id: str,
    message: str,
) -> None:
    """Invoke `announce`; never raise. Truncates message to SUMMARY_CAP."""
    try:
        announce(channel, chat_id, _truncate(message))
    except Exception as exc:  # noqa: BLE001
        logger.warning("events announce failed: %s", exc)


def emit_queued(
    *,
    task_id: str,
    target_repo: str | None,
    chat_id: str,
    announce: AnnounceCallback = _noop_announce,
    channel: str = DEFAULT_CHANNEL,
) -> None:
    """Fire when intake produces a fresh spec (state="new" path)."""
    repo = target_repo or "(project-less)"
    _safe_fire(announce, channel, chat_id, f"📋 Queued: {task_id} → {repo}")


def emit_dispatched(
    *,
    task_id: str,
    runner_kind: str,
    chat_id: str,
    announce: AnnounceCallback = _noop_announce,
    channel: str = DEFAULT_CHANNEL,
) -> None:
    """Fire when sweep/supervisor flips a spec to one of the dispatched-* states."""
    _safe_fire(announce, channel, chat_id, f"🚀 Dispatched: {task_id} ({runner_kind})")


def emit_done(
    *,
    task_id: str,
    pr_url: str | None,
    chat_id: str,
    announce: AnnounceCallback = _noop_announce,
    channel: str = DEFAULT_CHANNEL,
) -> None:
    """Fire when the per-task graph terminates with status=done."""
    if pr_url:
        msg = f"✅ Done: {task_id}\n{pr_url}"
    else:
        msg = f"✅ Done: {task_id}"
    _safe_fire(announce, channel, chat_id, msg)


def emit_terminal_failure(
    *,
    task_id: str,
    new_state: str,
    reason: str | None,
    chat_id: str,
    announce: AnnounceCallback = _noop_announce,
    channel: str = DEFAULT_CHANNEL,
) -> None:
    """Fire when the per-task graph (or watchdog) flips a spec to a terminal-failure state.

    `new_state` is the literal state name to surface ("blocked", "abandoned", …).
    `reason` is `result.blocker` / `spec.result_summary` if either is set, else
    falls back to "no reason captured".
    """
    rsn = (reason or "").strip() or "no reason captured"
    _safe_fire(announce, channel, chat_id, f"❌ {new_state}: {task_id}\n{rsn}")
