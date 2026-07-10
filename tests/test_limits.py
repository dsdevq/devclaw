"""limits.classify_failure — the rate-limit/quota vs real-failure discriminator.

Uses realistic error strings (Anthropic API + Claude Code OAuth usage-limit + the
actual ACP auth error seen in the Wave-0 dogfood) so the classifier earns trust."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from devclaw.limits import (
    RATE_LIMIT_MAX_PAUSE_S,
    RATE_LIMIT_PAUSE_S,
    RATE_LIMIT_STATED_MAX_S,
    FailureKind,
    _parse_retry_after,
    _seconds_until_reset,
    classify_failure,
    pause_seconds,
)


@pytest.mark.parametrize("text,kind", [
    # --- QUOTA (long cap → pause + resume) ---
    ("Claude usage limit reached. Your limit will reset at 9:00 PM.", FailureKind.QUOTA),
    ("You've reached your usage limit for this week.", FailureKind.QUOTA),
    ("rate_limit_error: This request would exceed your plan limit", FailureKind.QUOTA),
    ("insufficient_quota", FailureKind.QUOTA),
    # the REAL Claude Code usage-cap wording observed in the dogfood:
    ("Internal error: You're out of extra usage · resets 10pm (UTC)", FailureKind.QUOTA),
    ("Conversation run failed: Internal error: You're out of extra usage · resets 10pm (UTC)", FailureKind.QUOTA),
    # the Claude Code 5-hour SESSION-cap wording (dogfood 2026-06-21 — slipped
    # past the patterns, so the goal churned instead of pausing):
    ("You've hit your session limit · resets 12:20am (Europe/Dublin)", FailureKind.QUOTA),
    ("claude --print exited 1. stdout:\nYou've hit your session limit · resets 12:20am", FailureKind.QUOTA),
    # --- RATE_LIMIT (short cap / 429) ---
    ("API Error: 429 Too Many Requests", FailureKind.RATE_LIMIT),
    ("rate limit exceeded, slow down", FailureKind.RATE_LIMIT),
    ("You have hit the 5-hour limit", FailureKind.RATE_LIMIT),
    # --- TRANSIENT (retry-after-backoff) ---
    ("API Error: 529 Overloaded", FailureKind.TRANSIENT),
    ("overloaded_error: the service is temporarily overloaded", FailureKind.TRANSIENT),
    ("503 Service Unavailable", FailureKind.TRANSIENT),
    ("ECONNRESET: connection reset by peer", FailureKind.TRANSIENT),
    ("the request timed out after 90s", FailureKind.TRANSIENT),
    # --- REAL (auth + genuine bugs → fail, never pause) ---
    ("ACP error: Internal error: Failed to authenticate. API Error: 401 Invalid authentication credentials", FailureKind.REAL),
    ("ModuleNotFoundError: No module named 'fastapi'", FailureKind.REAL),
    ("AssertionError: expected 200 got 500", FailureKind.REAL),
    ("", FailureKind.REAL),
    (None, FailureKind.REAL),
])
def test_classify(text, kind):
    assert classify_failure(text).kind is kind


def test_auth_beats_429_substring():
    # an auth error that happens to mention a code must still be REAL, not paused
    c = classify_failure("401 Invalid authentication credentials (rate limit headers present)")
    assert c.kind is FailureKind.REAL


def test_pausing_flag():
    assert classify_failure("429 too many requests").is_pausing is True
    assert classify_failure("usage limit reached").is_pausing is True
    assert classify_failure("503 overloaded").is_pausing is False  # transient retries, not pauses
    assert classify_failure("AssertionError").is_pausing is False


@pytest.mark.parametrize("text,secs", [
    ("Retry-After: 30", 30),                      # bare HTTP header → seconds
    ("rate limit; try again in 5 minutes", 300),
    ("usage limit — resets in 2 hours", 7200),
    ("please wait 45 seconds", 45),
    ("429 reset in 10m", 600),
    ("Your limit will reset at 9:00 PM", None),   # absolute time not parsed
    ("rate limit exceeded", None),                # no hint
])
def test_retry_after_parsing(text, secs):
    assert _parse_retry_after(text) == secs


def test_retry_after_flows_through_on_pausing_kinds():
    # when the failure is a pausing kind, the parsed hint is surfaced
    assert classify_failure("429 too many requests; retry-after: 60").retry_after_s == 60
    assert classify_failure("usage limit reached; try again in 30 minutes").retry_after_s == 1800


# ---- absolute reset times ("resets 10pm (UTC)") -----------------------------
# Claude's REAL usage-cap wording states a wall-clock reset time, not a delay.
# Parsing needs a clock, so it's injected (now_utc) — the module stays pure.

_NOW = datetime(2026, 7, 10, 18, 0, 0, tzinfo=timezone.utc)  # 18:00 UTC


@pytest.mark.parametrize("text,secs", [
    # 10pm UTC is 4h ahead of 18:00 → 4h + 120s slack
    ("You're out of extra usage · resets 10pm (UTC)", 4 * 3600 + 120),
    # optional minutes + am; 12:20am is tomorrow 00:20 → 6h20m + slack
    ("You've hit your session limit · resets 12:20am", 6 * 3600 + 20 * 60 + 120),
    # "resets at" form, with minutes and a spaced 12-hour suffix
    ("Your limit will reset at 9:00 PM.", 3 * 3600 + 120),
    # stated time already passed today → wraps to tomorrow
    ("usage limit reached · resets 10am", 16 * 3600 + 120),
    # exactly now → next occurrence is a full day out
    ("usage limit reached · resets 6pm", 24 * 3600 + 120),
    # non-UTC zone name is treated as UTC (self-correcting on the next probe)
    ("You've hit your session limit · resets 12:20am (Europe/Dublin)", 6 * 3600 + 20 * 60 + 120),
    ("usage limit reached", None),                    # no absolute time stated
    ("rate limit; try again in 5 minutes", None),     # relative-only → not ours
])
def test_seconds_until_reset(text, secs):
    assert _seconds_until_reset(text, _NOW) == secs


def test_classify_parses_absolute_reset_when_clock_injected():
    c = classify_failure(
        "Internal error: You're out of extra usage · resets 10pm (UTC)", now_utc=_NOW
    )
    assert c.kind is FailureKind.QUOTA
    assert c.stated is True
    assert c.retry_after_s == 4 * 3600 + 120


def test_classify_without_clock_keeps_old_behavior():
    c = classify_failure("Internal error: You're out of extra usage · resets 10pm (UTC)")
    assert c.kind is FailureKind.QUOTA
    assert c.retry_after_s is None
    assert c.stated is False


def test_relative_hint_is_stated_even_without_clock():
    c = classify_failure("usage limit reached; try again in 30 minutes")
    assert c.retry_after_s == 1800 and c.stated is True


def test_relative_hint_beats_absolute_when_both_present():
    # the relative parser runs first; the clock is only consulted when it misses
    c = classify_failure("usage limit — try again in 5 minutes (resets 10pm)", now_utc=_NOW)
    assert c.retry_after_s == 300 and c.stated is True


def test_no_hint_with_clock_is_unstated():
    c = classify_failure("usage limit reached", now_utc=_NOW)
    assert c.retry_after_s is None and c.stated is False


# ---- pause_seconds policy ----------------------------------------------------


def test_stated_hint_survives_past_default_cap():
    # a 10h stated reset must NOT be clobbered to the 3600s re-probe cap — that
    # made devclaw re-probe a multi-hour cap hourly, each probe a doomed dispatch
    assert pause_seconds(36000, stated=True) == 36000


def test_stated_hint_still_bounded():
    assert pause_seconds(RATE_LIMIT_STATED_MAX_S + 999, stated=True) == RATE_LIMIT_STATED_MAX_S


def test_unstated_policy_unchanged():
    assert pause_seconds(None) == RATE_LIMIT_PAUSE_S            # default backoff
    assert pause_seconds(36000) == RATE_LIMIT_MAX_PAUSE_S       # legacy call form: old cap
    assert pause_seconds(None, stated=False) == RATE_LIMIT_PAUSE_S
