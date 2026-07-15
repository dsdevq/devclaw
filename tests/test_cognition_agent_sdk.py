"""AgentSDKCognition — the OPT-IN cognition backend over ``claude_agent_sdk``.

These tests never import ``claude_agent_sdk`` (an optional extra the stubbed
suite lacks): they inject a fake ``query_fn`` yielding fabricated message objects
whose class NAMES match what the adapter dispatches on (``AssistantMessage`` /
``ResultMessage`` / ``RateLimitEvent``), exactly like the real SDK's classes.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from devclaw import cognition
from devclaw.cognition import AgentSDKCognition, ClaudeCognition, get_cognition, set_cognition
from devclaw.loom.limits import FailureKind, classify_failure
from devclaw.planner import PlannerError


# --- fabricated SDK message shapes (class NAMES match the real SDK) -----------
class TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class AssistantMessage:
    def __init__(self, content: list) -> None:
        self.content = content


class ResultMessage:
    def __init__(self, *, is_error=False, result="", usage=None,
                 total_cost_usd=None, errors=None, api_error_status=None,
                 duration_ms=0, duration_api_ms=0) -> None:
        self.is_error = is_error
        self.result = result
        self.usage = usage or {}
        self.total_cost_usd = total_cost_usd
        self.errors = errors
        self.api_error_status = api_error_status
        self.duration_ms = duration_ms
        self.duration_api_ms = duration_api_ms


class RateLimitInfo:
    def __init__(self, *, status, resets_at=None, rate_limit_type=None,
                 overage_status=None, overage_resets_at=None,
                 overage_disabled_reason=None, raw=None) -> None:
        self.status = status
        self.resets_at = resets_at
        self.rate_limit_type = rate_limit_type
        self.overage_status = overage_status
        self.overage_resets_at = overage_resets_at
        self.overage_disabled_reason = overage_disabled_reason
        self.raw = raw


class RateLimitEvent:
    def __init__(self, rate_limit_info) -> None:
        self.rate_limit_info = rate_limit_info


def _query_fn_yielding(messages, *, capture=None, per_msg_delay=0.0):
    """Build a fake ``query(prompt=, options=)`` returning an async generator
    over ``messages``. ``capture`` (a dict) records the kwargs the adapter
    passed so tests can assert the scrubbed env / grounding options."""

    def _query(*, prompt, options):
        if capture is not None:
            capture["prompt"] = prompt
            capture["options"] = options

        async def _gen():
            for m in messages:
                if per_msg_delay:
                    await asyncio.sleep(per_msg_delay)
                yield m

        return _gen()

    return _query


@pytest.fixture(autouse=True)
def _reset_default():
    set_cognition(None)
    yield
    set_cognition(None)


@pytest.mark.asyncio
async def test_agent_sdk_returns_text_from_normal_stream():
    """Text from AssistantMessage TextBlocks is concatenated and returned; a
    trailing ResultMessage carries usage/cost into the trace."""
    messages = [
        AssistantMessage([TextBlock("Hello "), TextBlock("world")]),
        ResultMessage(
            is_error=False, result="Hello world",
            usage={"input_tokens": 10, "output_tokens": 5,
                   "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20},
            total_cost_usd=0.01,
        ),
    ]
    cog = AgentSDKCognition(query_fn=_query_fn_yielding(messages))
    out = await cog("do a thing", role="planner", model="sonnet")
    assert out == "Hello world"


@pytest.mark.asyncio
async def test_agent_sdk_inactivity_raises_planner_error():
    """No message within the inactivity window → PlannerError('timed out'), and
    the async generator is closed (its aclose fires, killing the subprocess)."""
    closed = {"aclose": False}

    def _query(*, prompt, options):
        async def _gen():
            # First message arrives, then the stream stalls forever.
            yield AssistantMessage([TextBlock("partial")])
            try:
                await asyncio.sleep(100)
                yield ResultMessage()
            finally:
                closed["aclose"] = True

        return _gen()

    cog = AgentSDKCognition(query_fn=_query)
    with pytest.raises(PlannerError, match="timed out"):
        # 30ms inactivity window (timeout_ms honored) — the second yield never
        # comes, so the wait_for on __anext__ trips.
        await cog("prompt", role="planner", timeout_ms=30)
    # aclose ran → the generator's finally executed.
    assert closed["aclose"] is True


@pytest.mark.asyncio
async def test_agent_sdk_rate_limit_rejected_maps_to_quota():
    """A RateLimitEvent(status='rejected') raises a PlannerError whose wording
    classify_failure maps to QUOTA WITH a stated reset hint — the pause
    machinery keys off exactly this."""
    resets_at = int(time.time()) + 3600
    info = RateLimitInfo(status="rejected", resets_at=resets_at,
                         rate_limit_type="seven_day")
    messages = [RateLimitEvent(info)]
    cog = AgentSDKCognition(query_fn=_query_fn_yielding(messages))

    with pytest.raises(PlannerError) as ei:
        await cog("prompt", role="planner")

    msg = str(ei.value)
    cls = classify_failure(msg)
    assert cls.kind is FailureKind.QUOTA, (msg, cls)
    assert cls.retry_after_s is not None and cls.retry_after_s > 0
    assert cls.stated is True
    # The retry hint reflects the stated reset (~3600s, within a wide band).
    assert 3000 < cls.retry_after_s <= 3600


@pytest.mark.asyncio
async def test_agent_sdk_rate_limit_allowed_does_not_raise():
    """A non-rejected RateLimitEvent (allowed_warning) is informational — it
    refreshes the liveness timer but does NOT fail the call."""
    info = RateLimitInfo(status="allowed_warning", rate_limit_type="five_hour")
    messages = [
        RateLimitEvent(info),
        AssistantMessage([TextBlock("still fine")]),
        ResultMessage(is_error=False, result="still fine"),
    ]
    cog = AgentSDKCognition(query_fn=_query_fn_yielding(messages))
    assert await cog("prompt", role="planner") == "still fine"


@pytest.mark.asyncio
async def test_agent_sdk_scrubs_oauth_keys_from_env(monkeypatch):
    """OAuth invariant: even if os.environ carries an API key / auth token,
    NEITHER is in the env handed to query()."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-should-be-stripped")
    capture: dict = {}
    messages = [
        AssistantMessage([TextBlock("ok")]),
        ResultMessage(is_error=False, result="ok"),
    ]
    cog = AgentSDKCognition(query_fn=_query_fn_yielding(messages, capture=capture))
    await cog("prompt", role="planner")

    opts = capture["options"]
    assert "ANTHROPIC_API_KEY" not in opts.env
    assert "ANTHROPIC_AUTH_TOKEN" not in opts.env


@pytest.mark.asyncio
async def test_agent_sdk_grounding_neutral_cwd_and_no_setting_sources(monkeypatch):
    """Grounding invariant (#227): a NEUTRAL temp cwd (not the devclaw checkout)
    and setting_sources=[] so ambient project context can't leak in."""
    capture: dict = {}
    messages = [ResultMessage(is_error=False, result="ok")]
    cog = AgentSDKCognition(query_fn=_query_fn_yielding(messages, capture=capture))
    await cog("prompt", role="planner")

    opts = capture["options"]
    assert opts.setting_sources == []
    assert opts.allowed_tools == []
    assert opts.max_turns == 1
    # cwd is a fresh temp dir, not the repo the test runs in.
    assert os.path.realpath(opts.cwd) != os.path.realpath(os.getcwd())


@pytest.mark.asyncio
async def test_agent_sdk_result_error_fails_closed():
    """A ResultMessage(is_error=True) fails CLOSED — never returns a value —
    and preserves the wording for the classifier (mirrors call_claude)."""
    messages = [
        ResultMessage(is_error=True, result="You've reached your usage limit"),
    ]
    cog = AgentSDKCognition(query_fn=_query_fn_yielding(messages))
    with pytest.raises(PlannerError, match="usage limit"):
        await cog("prompt", role="planner")


def test_env_agent_sdk_selects_agent_sdk(monkeypatch):
    monkeypatch.setenv("DEVCLAW_COGNITION", "agent_sdk")
    assert isinstance(get_cognition(), AgentSDKCognition)


def test_default_backend_still_claude(monkeypatch):
    """agent_sdk is OPT-IN — the default (unset env) stays ClaudeCognition."""
    monkeypatch.delenv("DEVCLAW_COGNITION", raising=False)
    assert isinstance(get_cognition(), ClaudeCognition)


def test_agent_sdk_timeout_env_default_and_override(monkeypatch):
    from devclaw.cognition import _inactivity_budget_s

    monkeypatch.delenv("DEVCLAW_COGNITION_TIMEOUT_S", raising=False)
    assert _inactivity_budget_s(None) == 180.0
    monkeypatch.setenv("DEVCLAW_COGNITION_TIMEOUT_S", "45")
    assert _inactivity_budget_s(None) == 45.0
    monkeypatch.setenv("DEVCLAW_COGNITION_TIMEOUT_S", "garbage")
    assert _inactivity_budget_s(None) == 180.0
    # explicit timeout_ms wins over env
    assert _inactivity_budget_s(5000) == 5.0
