"""Operator dispatch controls: a manual pause toggle and a daily run-window.

Both gate NEW dispatch — in-flight tasks already launched always run to
completion. They are the human-facing siblings of the automatic quota pause
(``StateStore.global_pause``) and the per-workspace circuit-breaker, read at the
two heartbeat gates: ``goal.tick.tick_all`` and ``task_queue.TaskQueue._pump``,
right beside the quota-pause check.

The window math is kept as pure functions over primitives so it is unit-testable
without a DB or a live clock: the callers read the persisted state + ``_now_ms``
and pass them in. Every uncertain input (malformed time, unknown timezone)
FAILS OPEN — a bad schedule must never silently wedge dispatch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

#: Shape of a run_schedule; the disabled default returned when none is persisted.
DEFAULT_SCHEDULE: dict = {"enabled": False, "start": "09:00", "end": "18:00", "tz": "Europe/Kyiv"}


def _parse_hhmm(s: str) -> int | None:
    """Minutes since midnight for an ``'HH:MM'`` string, or None if malformed."""
    try:
        hh, mm = str(s).split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h * 60 + m


def within_window(now_minutes: int, start: str, end: str) -> bool:
    """True if local wall-clock ``now_minutes`` (0..1439) is inside ``[start, end)``.

    Handles overnight windows (start > end), e.g. ``22:00``–``06:00``. A malformed
    or zero-width window is treated as always-open (fail-open)."""
    sm = _parse_hhmm(start)
    em = _parse_hhmm(end)
    if sm is None or em is None or sm == em:
        return True
    if sm < em:
        return sm <= now_minutes < em
    return now_minutes >= sm or now_minutes < em  # overnight span


def schedule_blocks(schedule: dict, now_utc_ms: int) -> tuple[bool, str]:
    """``(blocked, reason)`` for a run_schedule dict evaluated at ``now_utc_ms``.

    A disabled schedule or an unknown timezone never blocks (fail-open)."""
    if not schedule.get("enabled"):
        return False, ""
    tz = schedule.get("tz") or DEFAULT_SCHEDULE["tz"]
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — any tz resolution failure fails open
        return False, ""
    local = datetime.fromtimestamp(now_utc_ms / 1000, tz=timezone.utc).astimezone(zone)
    start = schedule.get("start") or DEFAULT_SCHEDULE["start"]
    end = schedule.get("end") or DEFAULT_SCHEDULE["end"]
    if within_window(local.hour * 60 + local.minute, start, end):
        return False, ""
    return True, f"outside run window {start}–{end} {tz} (local {local:%H:%M})"


def next_window_open_ms(schedule: dict, now_utc_ms: int) -> int | None:
    """UTC ms of the next window-open instant, or None when the schedule isn't
    blocking right now (disabled, malformed, unknown tz, or already open).

    The legibility half of the window gate: a blocked read surface can say
    "held until <when>" instead of looking idle. Same fail-open stance as
    :func:`schedule_blocks` — an uncertain schedule yields None, never a bogus
    timestamp."""
    blocked, _ = schedule_blocks(schedule, now_utc_ms)
    if not blocked:
        return None
    # blocked=True implies the tz resolved and start/end parsed non-degenerate.
    zone = ZoneInfo(schedule.get("tz") or DEFAULT_SCHEDULE["tz"])
    local = datetime.fromtimestamp(now_utc_ms / 1000, tz=timezone.utc).astimezone(zone)
    sm = _parse_hhmm(schedule.get("start") or DEFAULT_SCHEDULE["start"])
    if sm is None:  # unreachable given blocked=True; belt-and-braces fail-open
        return None
    target = local.replace(hour=sm // 60, minute=sm % 60, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return int(target.timestamp() * 1000)


def operator_block(
    hold: tuple[bool, str], schedule: dict, now_utc_ms: int
) -> tuple[bool, str]:
    """Combined manual-hold + run-window gate. The manual hold WINS over the
    schedule — an explicit pause is never overridden by an open window."""
    on, reason = hold
    if on:
        return True, reason or "operator pause"
    return schedule_blocks(schedule, now_utc_ms)
