"""The night-window close report — the mechanical, ZERO-LLM edge (ADR 0006
decision 3).

When the nightly run window (22:00–05:00 Europe/London by default) closes, the
*scheduled-edge owner* — today the goal heartbeat — assembles the night's slice
from rows devclaw already writes (``eval_outcomes``, the ``problems`` catalog)
and pushes a human-readable report through the existing notifier. It answers the
operator's real done-criterion: "kick off a goal for the night and it runs
without me." A night is **clean** iff **zero mechanism-wedges** fired in the
window.

Clean-night boundary (LOCKED, ADR 0006 §5-O1):

- **wedge** (fails the night) = ``mechanical:*`` blocks, cognition-timeout-
  treated-as-terminal, and engine/gate **crash** classes (an engine error, a
  review-gate crash, a no-result-line worker, a wall-clock timeout, a broken
  delivery). These are the loop's own plumbing breaking.
- **clean** = a genuine ``needs_answer`` (human-gated is the design — surfaced
  so the operator knows to answer, but it does NOT fail the night) and a
  **self-healed quota/auth pause** (the pause machinery working unattended IS
  the mechanism working — listed in ``pauses`` so the operator sees it, never a
  wedge). A gate *verdict* (review requested changes, verify/test-integrity/
  browser gate failed closed on genuinely bad code) is the gate doing its job —
  a quality signal, not a mechanism wedge.

Everything here is arithmetic over existing rows: no LLM call, no subprocess.
The window math is pure functions over primitives so it is unit-testable without
a DB or a live clock (mirrors ``dispatch_gate``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

# ---- window config ----------------------------------------------------------
# The nightly run window. Defaults to 22:00–05:00 Europe/London (the schedule tz
# the operator's night runs use). Overridable via env for a different cadence /
# timezone; the pure helpers also take explicit overrides so tests pin a clock.
NIGHT_WINDOW_START = os.environ.get("DEVCLAW_NIGHT_WINDOW_START", "22:00")
NIGHT_WINDOW_END = os.environ.get("DEVCLAW_NIGHT_WINDOW_END", "05:00")
NIGHT_WINDOW_TZ = os.environ.get("DEVCLAW_NIGHT_WINDOW_TZ", "Europe/London")


# ---- failure-class → clean-night bucket (mechanical, zero LLM) ---------------
# eval_outcomes failure_class values (state_store/rows.derive_failure_class) that
# are MECHANISM wedges: the loop's own plumbing broke. review_rejected /
# verify_failed / test_integrity / browser_gate_failed / blocked:worker are
# DELIBERATELY absent — those are the gate/worker producing a genuine verdict.
_EVAL_WEDGE_CLASSES = frozenset({
    "engine_error", "review_crash", "timeout", "no_result_line", "delivery_failed",
})
# Quota/auth failure_classes — pause-class, reported in `pauses`, never a wedge.
_EVAL_PAUSE_CLASSES = frozenset({"auth", "rate_limited"})

# problems-catalog categories (state_store/problems.PROBLEM_CATEGORIES) that are
# mechanism wedges. `block` is special-cased on its kind (mechanical:* wedges;
# needs_answer/bug/lost_ref/dispatch_cap are human-gated → clean).
_PROBLEM_WEDGE_CATEGORIES = frozenset({"cognition", "subprocess", "delivery"})
# The self-healed quota/auth pause category — reported, never a wedge.
_PROBLEM_PAUSE_CATEGORIES = frozenset({"limit"})
# block kinds that are human-gated (a genuine needs_answer) — clean, surfaced as
# "needs operator" but never failing the night. Anything else on a block whose
# kind starts "mechanical:" is a wedge.
_HUMAN_GATED_BLOCK_KINDS = frozenset({
    "needs_answer", "bug", "lost_ref", "dispatch_cap", "block",
})


def _parse_hhmm(s: str) -> Optional[tuple[int, int]]:
    """(hour, minute) for an ``'HH:MM'`` string, or None if malformed."""
    try:
        hh, mm = str(s).split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= h < 24 and 0 <= m < 60):
        return None
    return h, m


def most_recent_closed_window(
    now_ms: int,
    *,
    start: str = NIGHT_WINDOW_START,
    end: str = NIGHT_WINDOW_END,
    tz: str = NIGHT_WINDOW_TZ,
) -> Optional[tuple[str, int, int]]:
    """``(night_date, window_start_ms, window_end_ms)`` for the most-recent
    night window that has ALREADY closed at ``now_ms``, or None if the schedule
    can't be resolved (fail-safe — a bad tz/time skips the report, never crashes
    the heartbeat).

    A window opens at ``start`` on date D and closes at ``end`` on D+1 (overnight
    span). ``night_date`` is the YYYY-MM-DD of the OPEN (D), in the schedule tz —
    the PRIMARY KEY that makes the report fire exactly once per night. The
    "already closed" cut is what makes the heartbeat fire on the first wakeup
    after ``end`` and stay a no-op the rest of the day (the existence check does
    the deduping; this picks WHICH night)."""
    sh = _parse_hhmm(start)
    eh = _parse_hhmm(end)
    if sh is None or eh is None:
        return None
    try:
        zone = ZoneInfo(tz)
    except Exception:  # noqa: BLE001 — unknown tz fails safe (skip, never crash)
        return None
    local_now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(zone)
    today_close = local_now.replace(hour=eh[0], minute=eh[1], second=0, microsecond=0)
    # The date whose `end`-instant is the most recent close at or before now.
    close_date = local_now.date() if local_now >= today_close else (local_now.date() - timedelta(days=1))
    # The window that closed on close_date opened `start` the previous day.
    open_date = close_date - timedelta(days=1)
    window_start = datetime.combine(open_date, datetime.min.time(), tzinfo=zone).replace(
        hour=sh[0], minute=sh[1]
    )
    window_end = datetime.combine(close_date, datetime.min.time(), tzinfo=zone).replace(
        hour=eh[0], minute=eh[1]
    )
    return (
        open_date.isoformat(),
        int(window_start.timestamp() * 1000),
        int(window_end.timestamp() * 1000),
    )


@dataclass
class NightReport:
    """The assembled night slice — the shape :meth:`GoalService._maybe_emit_night_report`
    persists + pushes. ``clean`` is 1 iff ``wedges`` is empty."""

    night_date: str
    window_start_ms: int
    window_end_ms: int
    clean: bool
    wedges: list[dict] = field(default_factory=list)   # [{class, detail, ref}]
    pauses: list[dict] = field(default_factory=list)   # [{class, detail, ref}]
    needs_operator: list[dict] = field(default_factory=list)  # genuine needs_answer (clean)
    summary: str = ""
    #: throughput counts for the summary line (settled in the window)
    settled: int = 0
    done: int = 0
    failed: int = 0


def _entry(cls: str, detail: str, ref: str) -> dict:
    """One wedge/pause/needs-operator entry — the {class, detail, ref} wire shape."""
    return {"class": cls, "detail": (detail or "")[:200], "ref": ref or ""}


def assemble_night_report(
    store,
    night_date: str,
    window_start_ms: int,
    window_end_ms: int,
) -> NightReport:
    """Project the night's slice out of existing rows — ZERO LLM, pure SQL reads
    + mechanical bucketing. Reads ``eval_outcomes`` (PR1's read surface) for
    throughput + settle-path wedges/pauses, and the ``problems`` catalog for
    goal-tick-layer wedges (mechanical blocks, cognition/subprocess/delivery
    failures) and self-healed quota/auth pauses. ``clean`` iff no wedges."""
    wedges: list[dict] = []
    pauses: list[dict] = []
    needs_operator: list[dict] = []
    settled = done = failed = 0

    # --- eval_outcomes: throughput + settle-path wedges/pauses ---------------
    outcomes = store.list_eval_outcomes(source="live", limit=5000)
    for o in outcomes:
        s = o.get("settled_at")
        if s is None or not (window_start_ms <= s < window_end_ms):
            continue
        settled += 1
        status = o.get("status")
        if status == "done":
            done += 1
            continue
        if status == "failed":
            failed += 1
        fc = (o.get("failure_class") or "").lower()
        ref = o.get("task_id") or o.get("ticket") or ""
        detail = o.get("error") or fc or status or ""
        if fc.startswith("mechanical:") or fc in _EVAL_WEDGE_CLASSES:
            wedges.append(_entry(fc or "engine_error", detail, ref))
        elif fc in _EVAL_PAUSE_CLASSES:
            pauses.append(_entry(fc, detail, ref))
        # else: a gate verdict / cancelled — a genuine quality outcome, not a wedge.

    # --- problems catalog: goal-tick-layer wedges + self-healed pauses -------
    problems = store.list_problems(limit=5000)
    for p in problems:
        last = p.get("last_seen_ms")
        if last is None or not (window_start_ms <= last < window_end_ms):
            continue
        category = (p.get("category") or "").lower()
        kind = (p.get("kind") or "").strip()
        detail = p.get("sample_message") or p.get("summary") or kind
        ref = p.get("last_goal_id") or p.get("last_task_id") or p.get("fingerprint") or ""
        if category in _PROBLEM_PAUSE_CATEGORIES:
            pauses.append(_entry(kind or "pause", detail, ref))
        elif category == "block":
            if kind.startswith("mechanical:"):
                wedges.append(_entry(kind, detail, ref))
            elif kind in _HUMAN_GATED_BLOCK_KINDS:
                needs_operator.append(_entry(kind or "needs_answer", detail, ref))
            else:
                # An unrecognized block kind is human-gated by default (surfaced,
                # not a wedge) — a mechanical block is always kind="mechanical:*".
                needs_operator.append(_entry(kind or "block", detail, ref))
        elif category in _PROBLEM_WEDGE_CATEGORIES:
            wedges.append(_entry(kind or category, detail, ref))
        # else: task_fail / other → a genuine outcome, not a mechanism wedge.

    clean = len(wedges) == 0
    report = NightReport(
        night_date=night_date,
        window_start_ms=window_start_ms,
        window_end_ms=window_end_ms,
        clean=clean,
        wedges=wedges,
        pauses=pauses,
        needs_operator=needs_operator,
        settled=settled,
        done=done,
        failed=failed,
    )
    report.summary = render_summary(report)
    return report


def render_summary(r: NightReport) -> str:
    """The human-readable message body — the notifier payload + the persisted
    ``summary`` column. Concise, Telegram-friendly."""
    head = "✅ CLEAN — no mechanism-wedges." if r.clean else f"⚠️ {len(r.wedges)} wedge(s):"
    lines = [
        f"🌙 Night report {r.night_date} ({NIGHT_WINDOW_START}–{NIGHT_WINDOW_END} {NIGHT_WINDOW_TZ})",
        head,
    ]
    if r.wedges:
        for w in r.wedges:
            ref = f" ({w['ref']})" if w.get("ref") else ""
            lines.append(f"  • {w['class']} — {w['detail']}{ref}")
    if r.pauses:
        lines.append(f"self-healed pauses ({len(r.pauses)}):")
        for p in r.pauses:
            ref = f" ({p['ref']})" if p.get("ref") else ""
            lines.append(f"  • {p['class']} — {p['detail']}{ref}")
    if r.needs_operator:
        lines.append(f"needs operator ({len(r.needs_operator)}):")
        for n in r.needs_operator:
            ref = f" ({n['ref']})" if n.get("ref") else ""
            lines.append(f"  • {n['class']} — {n['detail']}{ref}")
    lines.append(f"settled {r.settled}: {r.done} done, {r.failed} failed")
    return "\n".join(lines)
