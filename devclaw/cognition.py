"""The cognition seam — one protocol behind which the LLM hides.

Today every cognition call shells out to ``claude --print``. That's a sound
default (Pro/Max OAuth, no API key, the constraint is session quota not a
bill), but it's coupled to *every* cognition site. Swapping in another backend
— a different model family, a local model, an HTTP API, a recorded fixture for
deterministic evals — would mean touching ten files. This module is the
chokepoint: each role calls the configured :class:`Cognition` with its prompt
and a label (``role``, optional ``model``), and the backend is decided once,
by env.

The factory is deliberately small. Implementations: :class:`ClaudeCognition`
(the default subprocess), :class:`StubCognition` (canned responses keyed by
role, for harnesses and offline evals), and the OPT-IN
:class:`AgentSDKCognition` (streams over ``claude_agent_sdk.query`` — the same
Pro/Max OAuth session, native liveness, structured usage + rate-limit events).

Selection is by env: ``DEVCLAW_COGNITION=claude`` (default), ``stub``, or the
opt-in ``agent_sdk``. Code
that calls cognition does NOT care which backend is wired; tests that need a
specific response inject their own caller directly (as they do today via
``claude_caller=`` parameters), so this seam is the *default* path, not the
*only* path.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from typing import Any, Awaitable, Callable, Optional, Protocol


class Cognition(Protocol):
    """One LLM call. ``role`` labels the cognition site (planner, evaluator,
    grill, judge, summary, review, goal_planner) for the trace + (future)
    backend-specific routing. ``model`` is the tier the caller selected (alias
    or full id); ``None`` → backend default."""

    async def __call__(
        self, prompt: str, *, role: str = "unknown", model: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> str: ...


class ClaudeCognition:
    """Production cognition: ``claude --print`` over the user's Pro/Max OAuth.
    Delegates to :func:`devclaw.planner.call_claude` so the existing timeout,
    error classification, and trace recording stay in one place."""

    async def __call__(
        self, prompt: str, *, role: str = "unknown", model: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> str:
        # Local import: planner imports from this module would cycle on the
        # `claude_with_model` shim that delegates back here.
        from .planner import call_claude

        return await call_claude(prompt, model=model, role=role, timeout_ms=timeout_ms)


class StubCognition:
    """Deterministic, no-network cognition for harnesses and offline evals.

    ``responses`` is a dict keyed by role; a missing role falls back to
    ``default``. Each call records into the trace exactly like a live call
    (via :func:`devclaw.planner.call_claude`'s recorder path is bypassed, so
    we record directly here), so a stub-mode harness produces the same trace
    *shape* as a live one."""

    def __init__(
        self,
        responses: "Optional[dict[str, str]]" = None,
        *,
        default: str = "{}",
    ) -> None:
        self.responses = dict(responses or {})
        self.default = default
        self.calls: list[tuple[str, str, str]] = []  # (role, model, prompt)

    async def __call__(
        self, prompt: str, *, role: str = "unknown", model: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> str:
        # timeout_ms is accepted for protocol parity; the stub never times out.
        from .loom import trace as _trace

        response = self.responses.get(role, self.default)
        self.calls.append((role, model or "", prompt))
        _trace.record_cognition(
            role=role, model=model or "(stub)", prompt=prompt,
            response=response, latency_ms=0,
        )
        return response


# --- agent-sdk backend -------------------------------------------------------
# OPT-IN alternative cognition: the `claude-agent-sdk` (an optional extra, not a
# core/dev dependency — the stubbed suite never imports it). Its ``query()`` is
# one-shot/stateless over the SAME ~/.claude Pro/Max OAuth session (it spawns the
# same `claude` binary — NO API key), streams messages (native liveness), and
# returns structured usage + a structured RateLimitEvent. This adapter wraps that
# stream with an INACTIVITY timeout implemented HERE (the CLI path's timeout lives
# in call_claude and is owned by a sibling change).

#: default inactivity/overall budget when neither ``timeout_ms`` nor
#: ``DEVCLAW_COGNITION_TIMEOUT_S`` is usable.
_AGENT_SDK_TIMEOUT_DEFAULT_S = 180.0

_MINIMAL_SYSTEM_PROMPT = (
    "You are a precise assistant invoked one-shot. Follow the instructions in "
    "the message exactly and return only what is asked."
)


def _inactivity_budget_s(timeout_ms: Optional[int]) -> float:
    """The per-message inactivity window. An explicit ``timeout_ms`` wins;
    otherwise ``DEVCLAW_COGNITION_TIMEOUT_S`` (default 180, invalid/unset →
    180)."""
    if timeout_ms is not None and timeout_ms > 0:
        return timeout_ms / 1000
    raw = os.environ.get("DEVCLAW_COGNITION_TIMEOUT_S")
    if raw is not None:
        try:
            v = float(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return _AGENT_SDK_TIMEOUT_DEFAULT_S


def _usage_int(usage: dict, key: str) -> Optional[int]:
    v = usage.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def _ratelimit_error_text(info: Any) -> Optional[str]:
    """If a ``RateLimitInfo`` is a hard stop (status ``rejected`` or overage
    disabled), synthesize a failure string that ``loom.limits.classify_failure``
    maps to ``QUOTA`` — WITHOUT forking or weakening the classifier. The wording
    matches the ``_QUOTA`` regex ("usage limit"/"quota") and carries a
    ``retry-after: <secs>`` hint (matched by ``_RETRY_AFTER_HEADER``) so the
    pause machinery gets a stated reset. Returns ``None`` when the limit is not
    a hard stop (``allowed``/``allowed_warning``) — those are informational and
    only refresh the liveness timer."""
    status = getattr(info, "status", None)
    overage_disabled = bool(getattr(info, "overage_disabled_reason", None))
    if status != "rejected" and not overage_disabled:
        return None
    rl_type = getattr(info, "rate_limit_type", None) or "usage"
    resets_at = getattr(info, "resets_at", None)
    hint = ""
    if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool):
        delta = int(resets_at - time.time())
        if delta > 0:
            hint = f" retry-after: {delta}"
    return (
        f"Claude usage limit reached ({rl_type}); the request was rejected until "
        f"the quota resets.{hint}"
    )


async def _aclose(agen: Any) -> None:
    """Close the SDK async iterator — its ``aclose`` tears down the spawned
    ``claude`` subprocess. Best-effort: closing must never mask the real
    failure being raised."""
    aclose = getattr(agen, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except BaseException:  # noqa: BLE001 — cleanup must not shadow the raise
        pass


class AgentSDKCognition:
    """OPT-IN cognition over ``claude_agent_sdk.query`` (see the module comment).

    ``query_fn`` is injectable (``None`` → the real ``claude_agent_sdk.query``,
    lazy-imported inside :meth:`__call__` so the stubbed suite imports this
    module WITHOUT the optional dependency installed). Tests drive it with a
    fake async generator yielding fabricated message objects.

    Invariants held here:
      * **OAuth only** — the env handed to ``query`` is ``os.environ`` with
        ``ANTHROPIC_API_KEY``/``ANTHROPIC_AUTH_TOKEN`` popped (mirrors
        ``planner.call_claude``), so a stray key never switches an autonomous
        run onto metered billing.
      * **Grounding (#227)** — ``cwd`` is a NEUTRAL temp dir and
        ``setting_sources=[]``, so the spawned ``claude`` does NOT read
        devclaw's ``CLAUDE.md`` / ambient project context into the grounded
        cognition prompt.
    """

    def __init__(self, query_fn: Optional[Callable[..., Any]] = None) -> None:
        self._query_fn = query_fn

    async def __call__(
        self, prompt: str, *, role: str = "unknown", model: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> str:
        # Local imports: planner would cycle (it delegates back to this seam),
        # and claude_agent_sdk is an OPTIONAL extra the stubbed suite lacks.
        from .planner import PlannerError
        from .loom import trace as _trace

        query_fn = self._query_fn
        options: Any
        if query_fn is None:
            import claude_agent_sdk  # type: ignore

            query_fn = claude_agent_sdk.query
            options_cls = claude_agent_sdk.ClaudeAgentOptions
        else:
            # Injected fake: build a plain options carrier so the OAuth/grounding
            # fields are still asserted by tests without the SDK installed.
            options_cls = _FakeAgentOptions

        inactivity_s = _inactivity_budget_s(timeout_ms)

        # OAuth invariant: copy the environment, strip both keys.
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)

        # Grounding invariant: neutral cwd + no ambient setting sources.
        cwd = tempfile.mkdtemp(prefix="devclaw-cognition-")

        options = options_cls(
            model=model,
            max_turns=1,
            allowed_tools=[],
            setting_sources=[],
            include_partial_messages=True,
            cwd=cwd,
            env=env,
            system_prompt=_MINIMAL_SYSTEM_PROMPT,
        )

        argv_head = "claude-agent-sdk query" + (f" --model {model}" if model else "")
        started = _trace.now_ms()
        agen = query_fn(prompt=prompt, options=options)
        aiter = agen.__aiter__()

        text_parts: list[str] = []
        result_msg: Any = None

        def _record_fail(error: str) -> None:
            latency = _trace.now_ms() - started
            _trace.record_cognition(
                role=role, model=model or "", prompt=prompt, response="",
                latency_ms=latency, error=error,
            )
            _trace.record_subprocess(
                cmd="claude-agent-sdk query", argv_head=argv_head,
                latency_ms=latency, exit_code=None, error=error,
            )

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(
                        aiter.__anext__(), timeout=inactivity_s
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    _record_fail("timeout")
                    raise PlannerError(
                        "claude-agent-sdk query timed out after "
                        f"{int(inactivity_s * 1000)}ms of inactivity"
                    )

                name = type(msg).__name__
                if name == "RateLimitEvent":
                    info = getattr(msg, "rate_limit_info", None)
                    if info is not None:
                        text = _ratelimit_error_text(info)
                        if text is not None:
                            _record_fail(f"rate_limit_rejected: {text[:120]}")
                            raise PlannerError(text)
                    continue
                if name == "AssistantMessage":
                    content = getattr(msg, "content", None)
                    if isinstance(content, (list, tuple)):
                        for block in content:
                            txt = getattr(block, "text", None)
                            if isinstance(txt, str):
                                text_parts.append(txt)
                    continue
                if name == "ResultMessage":
                    result_msg = msg
                    continue
                # SystemMessage / partial StreamEvent / UserMessage: ignored for
                # text, but each still refreshed the inactivity timer above.
        finally:
            await _aclose(agen)
            shutil.rmtree(cwd, ignore_errors=True)

        response_text = "".join(text_parts)
        if not response_text and result_msg is not None:
            r = getattr(result_msg, "result", None)
            if isinstance(r, str):
                response_text = r

        # ResultMessage error (e.g. quota wording that arrives via the result,
        # not a RateLimitEvent) fails CLOSED — mirror call_claude's envelope
        # error path so classify_failure still sees the raw wording.
        if result_msg is not None and getattr(result_msg, "is_error", False):
            detail = (
                getattr(result_msg, "result", None)
                or getattr(result_msg, "errors", None)
                or getattr(result_msg, "api_error_status", None)
                or "unknown error"
            )
            _record_fail(f"result error: {str(detail)[:200]}")
            raise PlannerError(
                f"claude-agent-sdk query returned an error result: {detail}"
            )

        usage = getattr(result_msg, "usage", None)
        usage = usage if isinstance(usage, dict) else {}
        cost = getattr(result_msg, "total_cost_usd", None)
        latency = _trace.now_ms() - started
        _trace.record_subprocess(
            cmd="claude-agent-sdk query", argv_head=argv_head,
            latency_ms=latency, exit_code=0,
        )
        _trace.record_cognition(
            role=role, model=model or "", prompt=prompt,
            response=response_text, latency_ms=latency,
            tokens_in=_usage_int(usage, "input_tokens"),
            tokens_out=_usage_int(usage, "output_tokens"),
            cache_read=_usage_int(usage, "cache_read_input_tokens"),
            cache_creation=_usage_int(usage, "cache_creation_input_tokens"),
            cost_usd=float(cost) if isinstance(cost, (int, float))
            and not isinstance(cost, bool) else None,
        )
        return response_text


class _FakeAgentOptions:
    """Stand-in for ``ClaudeAgentOptions`` when a fake ``query_fn`` is injected
    (the SDK is an optional extra the stubbed suite lacks). Carries the same
    fields so tests can assert the OAuth-scrubbed env and grounding settings
    that were handed to ``query``."""

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


_default: Optional[Cognition] = None


def get_cognition() -> Cognition:
    """Return the configured default. Lazy + cached so the env decides backend
    once per process."""
    global _default
    if _default is None:
        _default = _from_env()
    return _default


def set_cognition(cog: Optional[Cognition]) -> None:
    """Replace the configured cognition. ``None`` resets to lazy-from-env on
    the next :func:`get_cognition`. Test harnesses use this to swap in a
    deterministic stub for a single run."""
    global _default
    _default = cog


def _from_env() -> Cognition:
    """Backend selection — read once at first use."""
    name = os.environ.get("DEVCLAW_COGNITION", "claude").strip().lower()
    if name == "claude":
        return ClaudeCognition()
    if name == "stub":
        return StubCognition()
    if name == "agent_sdk":
        return AgentSDKCognition()
    raise ValueError(
        f"unknown DEVCLAW_COGNITION={name!r}; supported: claude, stub, agent_sdk"
    )


def bind(
    model: Optional[str],
    *,
    role: str = "unknown",
    timeout_ms: Optional[int] = None,
) -> Callable[[str], Awaitable[str]]:
    """Convenience: return a one-arg caller bound to (model, role, timeout_ms)
    via the configured cognition. Each role's ``default_caller`` uses this so
    the swap point is centralized. ``timeout_ms`` is threaded through to
    :meth:`Cognition.__call__` — claude backend honors it, stub ignores it."""

    async def _caller(prompt: str) -> str:
        return await get_cognition()(
            prompt, role=role, model=model, timeout_ms=timeout_ms,
        )

    return _caller
