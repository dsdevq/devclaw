"""limits.classify_failure — the rate-limit/quota vs real-failure discriminator.

Uses realistic error strings (Anthropic API + Claude Code OAuth usage-limit + the
actual ACP auth error seen in the Wave-0 dogfood) so the classifier earns trust."""
from __future__ import annotations

import pytest

from devclaw.limits import FailureKind, classify_failure, _parse_retry_after


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
