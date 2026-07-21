"""Classify an agent/planner failure so a usage-limit is never mistaken for a bug.

The load-bearing distinction: a model **usage/rate limit** must NOT be treated like
a code failure. Today a quota hit surfaces as a generic error → the queue retries
immediately → it burns the remaining quota on the same doomed call → the task fails
and the goal blocks. Instead we classify the failure text and let the caller PAUSE
on a limit and resume when it resets, while real failures still fail and transient
blips back off briefly.

Kinds:
  - RATE_LIMIT  short-term cap (per-minute / 5-hour) — pause, resume on reset
  - QUOTA       longer cap (weekly / "usage limit reached") — pause, resume on reset
  - AUTH        expired/broken login — pause + actionable owner ping, re-probe
  - TRANSIENT   overloaded / 5xx / network blip — short backoff, then retry
  - REAL        genuine code/agent failure — fail (with feedback), don't wait

Auth failures (401 / "failed to authenticate" / an expired OAuth session) used to
be REAL ("surface, don't pause") — the 2026-07-20 unattended night proved that
wrong: an expired VPS login "surfaced" as ~58 terminal cognition failures across
the whole run window with no pause and no owner ping, every call doomed until a
human re-login. AUTH is now a pausing kind like QUOTA — one account-wide pause
gates queue + heartbeat — but with its own fixed re-probe backoff
(:data:`AUTH_PAUSE_S`, no reset time exists to parse) and an ACTIONABLE ping
(the goal layer words it as "re-login needed", not "usage limit"). Waiting still
doesn't fix auth; pausing stops the doomed-call burn while the ping gets the
human, and the re-probe auto-resumes work after the re-login with no extra verb.

Pure + deterministic (no I/O, no hidden clock) so it's trivially unit-tested
against real error strings. ``retry_after_s`` is parsed from the text when the
provider states a reset hint; otherwise None and the caller applies a default
backoff. Absolute reset TIMES ("resets 10pm (UTC)") need a clock, so they are
parsed only when the caller injects ``now_utc`` — the module stays pure.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

# Quota-pause policy (shared by the task queue and the goal heartbeat so both
# layers pause as one). When a usage/rate limit gives no reset hint, pause this
# long before re-probing (capped at MAX). A STATED hint — the provider told us
# when the limit lifts, relative ("in 2 hours") or absolute ("resets 10pm") —
# is trusted up to STATED_MAX instead: clamping it to MAX made devclaw re-probe
# a multi-hour/day cap hourly, each probe a doomed dispatch.
RATE_LIMIT_PAUSE_S = 1800
RATE_LIMIT_MAX_PAUSE_S = 3600
RATE_LIMIT_STATED_MAX_S = 86_400

#: fixed re-probe cadence for an AUTH pause. No reset time exists to parse (the
#: provider can't say when a human will re-login), so the trade is: short →
#: faster auto-resume after the re-login but chattier reminder pings + more
#: doomed probe calls while broken; long → the reverse. 2h ≈ 3-4 pings across a
#: broken night, ≤2h resume lag after the fix, ≤12 probe calls/day worst case.
AUTH_PAUSE_S = 7200


class FailureKind(str, Enum):
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    AUTH = "auth"
    TRANSIENT = "transient"
    REAL = "real"


def pause_seconds(
    retry_after_s: int | None, *, stated: bool = False,
    kind: "FailureKind | None" = None,
) -> int:
    """The backoff to use for a pausing failure. Centralizes the policy so task +
    goal layers agree. ``stated=True`` means ``retry_after_s`` came from the
    provider's own text (see :class:`Classification`), so it's trusted up to the
    generous STATED cap; unstated hints keep the legacy default/cap. AUTH ignores
    hints entirely — there is no stated reset for a broken login, only the fixed
    :data:`AUTH_PAUSE_S` re-probe cadence."""
    if kind is FailureKind.AUTH:
        return AUTH_PAUSE_S
    if stated and retry_after_s:
        return min(retry_after_s, RATE_LIMIT_STATED_MAX_S)
    return min(retry_after_s or RATE_LIMIT_PAUSE_S, RATE_LIMIT_MAX_PAUSE_S)


#: kinds that mean "the model is unavailable for a while — pause and resume", as
#: opposed to retry-now (TRANSIENT) or fail (REAL). AUTH pauses too (2026-07-20
#: night incident) — the difference is the ping wording + fixed re-probe, not
#: the pause mechanics.
PAUSING_KINDS = (FailureKind.RATE_LIMIT, FailureKind.QUOTA, FailureKind.AUTH)


@dataclass(frozen=True)
class Classification:
    kind: FailureKind
    retry_after_s: int | None
    matched: str  # the phrase that triggered the classification (for logs)
    #: True when ``retry_after_s`` was stated by the provider's own text
    #: (relative "in 2 hours" or absolute "resets 10pm") — the caller may trust
    #: it past the default re-probe cap (see :func:`pause_seconds`).
    stated: bool = False

    @property
    def is_pausing(self) -> bool:
        return self.kind in PAUSING_KINDS


# --- patterns (checked in priority order; first hit wins) --------------------
# NB: a vendored subset of these patterns (AUTH/QUOTA/RATE + the relative
# retry-after parser) lives in openhands-runner/runner.py — the in-sandbox
# runner can't import devclaw, so it carries its own copy to emit a structured
# status="rate_limited" result. Keep the two in sync when editing.
# AUTH first: harness-shaped auth wording must classify as AUTH even when the
# text also mentions a rate-limit-shaped code — an expired login is never a
# quota event. "authentication required" is the ACP/worker wording from the
# 2026-07-20 night incident ("Conversation run failed …: Authentication
# required"); the planner-side wording that night ("Failed to authenticate:
# OAuth session expired and could not be refreshed") already matched.
# STRONG vs WEAK split (invariant-guard find, 2026-07-21): now that AUTH
# pauses the whole account, a bare "401"/"Unauthorized" in gate/review
# feedback about the app under development ("expected 200 got 401", "the
# /admin endpoint returns 401 for logged-in users") must NOT trigger a 2h
# pause + a false re-login ping. Only STRONG (harness-shaped) wording is AUTH;
# WEAK matches stay REAL — still checked before quota/rate so a 401 with
# rate-limit words nearby is never misrouted onto the pause-with-reset path.
# NB: the vendored runner.py copy (_LIMIT_AUTH) keeps the strong∪weak UNION —
# its only job is shielding auth text from the rate_limited tag; the host
# re-classifies from the original wording.
_AUTH = re.compile(
    r"invalid authentication|failed to authenticate|"
    r"authentication[ _]required|"
    r"authentication_error|oauth.*(expired|invalid)|please run /login",
    re.IGNORECASE,
)
_AUTH_WEAK = re.compile(r"\b401\b|unauthor", re.IGNORECASE)
# QUOTA: the longer "you're out for a while" caps (Claude Pro/Max usage limits).
# NOTE: "out of (extra )?usage" is the REAL Claude Code wording observed live —
# "Internal error: You're out of extra usage · resets 10pm (UTC)" — which the
# "usage limit"/"quota" patterns alone missed (dogfood finding 2026-06-20).
_QUOTA = re.compile(
    r"usage limit|weekly limit|quota|out of (extra )?usage|ran out of \w*\s*usage|"
    r"limit reached|reached your (usage|plan) limit|you'?ve reached|"
    r"plan limit|insufficient_quota|credit balance|"
    # "You've hit your session limit · resets 12:20am" — the Claude Code 5-hour
    # session cap; resets at a time, so pause + resume (dogfood finding 2026-06-21,
    # this wording slipped past and the goal churned instead of pausing).
    r"session limit|hit your [\w ]{0,16}limit",
    re.IGNORECASE,
)
# RATE_LIMIT: short-term throttling (per-min / 5-hour) + HTTP 429.
_RATE = re.compile(
    r"\b429\b|rate[ _-]?limit|too many requests|5[ -]?hour limit|"
    r"slow down|requests per",
    re.IGNORECASE,
)
# TRANSIENT: overloaded / network — retry after a short backoff. We match PHRASES
# (not bare 5xx codes): a bare "500"/"502" shows up in assertion messages like
# "expected 200 got 500" and must NOT be mistaken for a server error. 529 is kept
# because it's Anthropic's overloaded code and not a common assertion number.
_TRANSIENT = re.compile(
    r"\boverloaded\b|overloaded_error|\b529\b|internal server error|"
    r"service unavailable|temporarily unavailable|bad gateway|gateway timeout|"
    r"econnreset|connection reset|connection refused|timed? ?out|timeout|"
    r"network error|eai_again|temporary failure",
    re.IGNORECASE,
)

_UNITS = {"s": 1, "m": 60, "h": 3600}

# "try again in 5 minutes", "reset in 10m", "wait 2 hours" (number + unit) …
_RETRY_AFTER_UNIT = re.compile(
    r"(?:retry[- ]after|try again in|reset[s]? in(?: about)?|wait)\D{0,8}?"
    r"(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|h|m|s)\b",
    re.IGNORECASE,
)
# … and the bare HTTP header form "Retry-After: 30" (seconds, no unit word).
_RETRY_AFTER_HEADER = re.compile(r"retry[- ]after:?\s*(\d+)\b", re.IGNORECASE)

# Absolute reset TIME — Claude's actual usage-cap wording states a wall-clock
# time, not a delay: "You're out of extra usage · resets 10pm (UTC)", "You've
# hit your session limit · resets 12:20am", "Your limit will reset at 3:30pm".
# Optional minutes, optional trailing "(zone)". Non-UTC zone names are treated
# as UTC too — a wrong pause is self-correcting (the next probe re-classifies),
# whereas skipping the hint left a multi-hour cap re-probed on the short cap.
_RESET_AT_ABS = re.compile(
    r"reset[s]?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b",
    re.IGNORECASE,
)
#: slack added past the stated reset so we don't probe a second early and
#: re-trip the same limit.
_RESET_ABS_SLACK_S = 120


def _seconds_until_reset(text: str, now_utc: datetime) -> int | None:
    """Seconds from ``now_utc`` to the NEXT occurrence of an absolute reset time
    stated in ``text`` ("resets 10pm (UTC)"), plus a small slack. None when no
    absolute time is stated. Assumes UTC when no/other zone is given (see the
    regex note). Pure — the clock is injected, never read."""
    m = _RESET_AT_ABS.search(text or "")
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "pm":
        hour += 12
    minute = int(m.group(2) or 0)
    if minute > 59:
        return None
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    target = now_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_utc:
        target += timedelta(days=1)  # stated time already passed today → tomorrow
    return int((target - now_utc).total_seconds()) + _RESET_ABS_SLACK_S


def _parse_retry_after(text: str) -> int | None:
    """Best-effort parse of a stated reset delay → seconds. None if not stated.
    (Absolute reset *times* like 'resets at 3pm' need a clock — see
    :func:`_seconds_until_reset`, tried by :func:`classify_failure` only when
    the caller injects ``now_utc``.)"""
    t = text or ""
    m = _RETRY_AFTER_UNIT.search(t)
    if m:
        return int(m.group(1)) * _UNITS[m.group(2)[0].lower()]
    m = _RETRY_AFTER_HEADER.search(t)
    if m:
        return int(m.group(1))  # bare Retry-After is seconds
    return None


def classify_failure(text: str | None, *, now_utc: datetime | None = None) -> Classification:
    """Classify a failure string. Defaults to REAL when nothing matches — an
    unrecognized failure is treated as a real bug (fail), never silently paused.

    ``now_utc``, when given, also lets absolute reset times ("resets 10pm (UTC)")
    become a ``retry_after_s`` hint; omitted (default) preserves the pure
    text-only behavior exactly."""
    t = text or ""

    def _hint() -> int | None:
        h = _parse_retry_after(t)
        if h is None and now_utc is not None:
            h = _seconds_until_reset(t, now_utc)
        return h

    if _AUTH.search(t):
        # No retry_after: a login has no reset time; pause_seconds(kind=AUTH)
        # supplies the fixed re-probe cadence.
        return Classification(FailureKind.AUTH, None, "auth")
    if _AUTH_WEAK.search(t):
        # Bare 401/unauthorized without harness wording: most likely feedback
        # prose about the app under development — REAL (retry with feedback),
        # but still shielded from the quota/rate patterns below.
        return Classification(FailureKind.REAL, None, "auth")
    if _QUOTA.search(t):
        h = _hint()
        return Classification(FailureKind.QUOTA, h, "quota", stated=h is not None)
    if _RATE.search(t):
        h = _hint()
        return Classification(FailureKind.RATE_LIMIT, h, "rate_limit", stated=h is not None)
    if _TRANSIENT.search(t):
        h = _hint()
        return Classification(FailureKind.TRANSIENT, h, "transient", stated=h is not None)
    return Classification(FailureKind.REAL, None, "")
