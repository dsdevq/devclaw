"""The plain-language summarizer is BEST-EFFORT: it rewrites owner notifications,
but any failure (caller error, empty / runaway output) must fall back to the raw
text so a notification is never lost."""

from __future__ import annotations

import pytest

from devclaw.goal_summary import plain_summary


@pytest.mark.asyncio
async def test_rewrites_via_caller():
    async def caller(prompt: str) -> str:
        assert "non-technical" in prompt.lower()  # the owner-facing instruction
        return "  The dashboard is ready for you to look at.  "  # padded → stripped

    out = await plain_summary("✅ [g] goal complete (verified) — added /health endpoint + tests", caller=caller)
    assert out == "The dashboard is ready for you to look at."


@pytest.mark.asyncio
async def test_falls_back_to_raw_on_caller_error():
    async def caller(prompt: str) -> str:
        raise RuntimeError("model unavailable")

    raw = "🟡 [g] needs you — which auth provider?"
    assert await plain_summary(raw, caller=caller) == raw


@pytest.mark.asyncio
async def test_falls_back_to_raw_on_empty_output():
    async def caller(prompt: str) -> str:
        return "   "

    raw = "🟡 [g] needs you — pick a DB"
    assert await plain_summary(raw, caller=caller) == raw


@pytest.mark.asyncio
async def test_falls_back_to_raw_on_runaway_output():
    async def caller(prompt: str) -> str:
        return "x" * 5000  # a runaway response is garbage, not a 1-2 sentence summary

    raw = "✅ [g] goal complete (verified) — done"
    assert await plain_summary(raw, caller=caller) == raw
