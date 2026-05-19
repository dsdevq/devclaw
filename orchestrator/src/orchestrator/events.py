"""Task-lifecycle Telegram announces.

Fires `AnnounceCallback` on five state transitions in the atomic-task pipeline:

  1. `task_intake → spec_created`        — intake.py writes a fresh spec
  2. `task_dispatch → dispatched-*`      — sweep flips ready → dispatched-*
  3. `task_runner → done` (with pr_url)  — runner completes successfully
  4. `task_runner → done` (no pr_url)    — runner completes without a PR
  5. `task_runner → failed | abandoned`  — runner self-blocks (failed) or
                                           watchdog kills it (abandoned)

Each emitted summary is capped at `MAX_SUMMARY_CHARS` (300) — long reasons are truncated with `…`.

Env-var resolution (`resolve_events_chat`):
    LIFEKIT_TELEGRAM_EVENTS_CHAT → preferred (task-lifecycle target)
    LIFEKIT_TELEGRAM_CHAT        → fallback (shared with other narration)
    `default` arg                → last-resort default

Reuses the `AnnounceCallback` type shipped with PR #21 so daemon plumbing
only carries one announce abstraction.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

logger = logging.getLogger(__name__)


AnnounceCallback = Callable[[str, str, str], None]
"""(channel, target, message) → None. Same shape as the audit-loop announce."""


EVENTS_CHAT_ENV = "LIFEKIT_TELEGRAM_EVENTS_CHAT"
FALLBACK_CHAT_ENV = "LIFEKIT_TELEGRAM_CHAT"
MAX_SUMMARY_CHARS = 300
EVENTS_CHANNEL = "telegram"


def _noop_announce(channel: str, target: str, message: str) -> None:  # noqa: ARG001
    """Default events_announce — logs at INFO so dry-run / unit-test paths can
    observe what would have been sent."""
    logger.info("announce(%s -> %s): %s", channel, target, message)


def resolve_events_chat(default: str = "default") -> str:
    """Resolve the Telegram chat id for task-lifecycle announces.

    Precedence: LIFEKIT_TELEGRAM_EVENTS_CHAT > LIFEKIT_TELEGRAM_CHAT > `default`.
    """
    return (
        os.environ.get(EVENTS_CHAT_ENV)
        or os.environ.get(FALLBACK_CHAT_ENV)
        or default
    )


def _truncate(text: str, cap: int = MAX_SUMMARY_CHARS) -> str:
    if len(text) <= cap:
        return text
    if cap <= 1:
        return text[:cap]
    return text[: cap - 1] + "…"


def _safe_call(
    announce: AnnounceCallback, channel: str, target: str, message: str
) -> None:
    """Invoke `announce` with the standard error swallow — events_announce
    must NEVER raise into the caller."""
    try:
        announce(channel, target, message)
    except Exception as exc:  # noqa: BLE001
        logger.warning("events_announce failed: %s", exc)


# ─── Per-event emitters ──────────────────────────────────────────────────────


def emit_spec_created(
    announce: AnnounceCallback,
    target: str,
    *,
    task_id: str,
    target_repo: str | None,
) -> None:
    """Event 1: a task_intake successfully persisted a spec.yaml on disk."""
    repo_label = target_repo or "(project-less)"
    message = _truncate(f"📋 Queued: {task_id} → {repo_label}")
    _safe_call(announce, EVENTS_CHANNEL, target, message)


def emit_dispatched(
    announce: AnnounceCallback,
    target: str,
    *,
    task_id: str,
    runner_kind: str,
) -> None:
    """Event 2: task_dispatch flipped a spec from ready → dispatched-*."""
    message = _truncate(f"🚀 Dispatched: {task_id} ({runner_kind})")
    _safe_call(announce, EVENTS_CHANNEL, target, message)


def emit_done(
    announce: AnnounceCallback,
    target: str,
    *,
    task_id: str,
    pr_url: str | None,
) -> None:
    """Event 3/4: task_runner reached `done` (optionally with a pr_url)."""
    if pr_url:
        message = _truncate(f"✅ Done: {task_id}\n{pr_url}")
    else:
        message = _truncate(f"✅ Done: {task_id}")
    _safe_call(announce, EVENTS_CHANNEL, target, message)


def emit_terminal_failure(
    announce: AnnounceCallback,
    target: str,
    *,
    task_id: str,
    new_state: str,
    reason: str | None,
) -> None:
    """Event 5: task_runner reached a terminal failure state (failed | abandoned).

    `new_state` is the human-readable label that goes into the message ("failed" or "abandoned").
    The reason is truncated so the whole summary still fits in MAX_SUMMARY_CHARS.
    """
    head = f"❌ {new_state}: {task_id}\n"
    reason_text = (reason or "").strip() or "no reason captured"
    budget = MAX_SUMMARY_CHARS - len(head)
    if budget <= 0:
        # Pathological — the head itself would overflow. Just truncate the whole thing.
        _safe_call(announce, EVENTS_CHANNEL, target, _truncate(head + reason_text))
        return
    if len(reason_text) > budget:
        reason_text = reason_text[: budget - 1] + "…" if budget > 1 else reason_text[:budget]
    _safe_call(announce, EVENTS_CHANNEL, target, head + reason_text)
