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
  - TRANSIENT   overloaded / 5xx / network blip — short backoff, then retry
  - REAL        genuine code/agent/auth failure — fail (with feedback), don't wait

Auth failures (401 / "failed to authenticate") are REAL on purpose: waiting won't
fix them (they need a re-login), so they must surface, not silently pause forever.

Pure + deterministic (no I/O, no clock) so it's trivially unit-tested against real
error strings. ``retry_after_s`` is parsed from the text when the provider states a
reset hint; otherwise None and the caller applies a default backoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class FailureKind(str, Enum):
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    TRANSIENT = "transient"
    REAL = "real"


#: kinds that mean "the model is unavailable for a while — pause and resume", as
#: opposed to retry-now (TRANSIENT) or fail (REAL).
PAUSING_KINDS = (FailureKind.RATE_LIMIT, FailureKind.QUOTA)


@dataclass(frozen=True)
class Classification:
    kind: FailureKind
    retry_after_s: int | None
    matched: str  # the phrase that triggered the classification (for logs)

    @property
    def is_pausing(self) -> bool:
        return self.kind in PAUSING_KINDS


# --- patterns (checked in priority order; first hit wins) --------------------
# AUTH first: "401 ... authenticate" must be REAL even though it's an API error —
# otherwise we'd pause forever on an expired login instead of surfacing it.
_AUTH = re.compile(
    r"\b401\b|invalid authentication|failed to authenticate|unauthor|"
    r"authentication_error|oauth.*(expired|invalid)|please run /login",
    re.IGNORECASE,
)
# QUOTA: the longer "you're out for a while" caps (Claude Pro/Max usage limits).
_QUOTA = re.compile(
    r"usage limit|weekly limit|quota|"
    r"limit reached|reached your (usage|plan) limit|you'?ve reached|"
    r"plan limit|insufficient_quota|credit balance",
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


def _parse_retry_after(text: str) -> int | None:
    """Best-effort parse of a stated reset delay → seconds. None if not stated.
    (Absolute reset *times* like 'resets at 3pm' are intentionally not parsed —
    that needs a clock/timezone; the caller applies a default backoff instead.)"""
    t = text or ""
    m = _RETRY_AFTER_UNIT.search(t)
    if m:
        return int(m.group(1)) * _UNITS[m.group(2)[0].lower()]
    m = _RETRY_AFTER_HEADER.search(t)
    if m:
        return int(m.group(1))  # bare Retry-After is seconds
    return None


def classify_failure(text: str | None) -> Classification:
    """Classify a failure string. Defaults to REAL when nothing matches — an
    unrecognized failure is treated as a real bug (fail), never silently paused."""
    t = text or ""
    if _AUTH.search(t):
        return Classification(FailureKind.REAL, None, "auth")
    if _QUOTA.search(t):
        return Classification(FailureKind.QUOTA, _parse_retry_after(t), "quota")
    if _RATE.search(t):
        return Classification(FailureKind.RATE_LIMIT, _parse_retry_after(t), "rate_limit")
    if _TRANSIENT.search(t):
        return Classification(FailureKind.TRANSIENT, _parse_retry_after(t), "transient")
    return Classification(FailureKind.REAL, None, "")
