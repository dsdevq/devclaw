"""The cognition seam — one protocol behind which the LLM hides.

Today every cognition call shells out to ``claude --print``. That's a sound
default (Pro/Max OAuth, no API key, the constraint is session quota not a
bill), but it's coupled to *every* cognition site. Swapping in another backend
— a different model family, a local model, an HTTP API, a recorded fixture for
deterministic evals — would mean touching ten files. This module is the
chokepoint: each role calls the configured :class:`Cognition` with its prompt
and a label (``role``, optional ``model``), and the backend is decided once,
by env.

The factory is deliberately small. Today there are two implementations:
:class:`ClaudeCognition` (the current subprocess), and :class:`StubCognition`
(canned responses keyed by role, for harnesses and offline evals). A third —
HTTP, a different model family — would be ~50 lines and drop into the same
seam.

Selection is by env: ``DEVCLAW_COGNITION=claude`` (default) or ``stub``. Code
that calls cognition does NOT care which backend is wired; tests that need a
specific response inject their own caller directly (as they do today via
``claude_caller=`` parameters), so this seam is the *default* path, not the
*only* path.
"""

from __future__ import annotations

import os
from typing import Awaitable, Callable, Optional, Protocol


class Cognition(Protocol):
    """One LLM call. ``role`` labels the cognition site (planner, evaluator,
    grill, judge, summary, review, goal_planner) for the trace + (future)
    backend-specific routing. ``model`` is the tier the caller selected (alias
    or full id); ``None`` → backend default."""

    async def __call__(
        self, prompt: str, *, role: str = "unknown", model: Optional[str] = None,
    ) -> str: ...


class ClaudeCognition:
    """Production cognition: ``claude --print`` over the user's Pro/Max OAuth.
    Delegates to :func:`devclaw.planner.call_claude` so the existing timeout,
    error classification, and trace recording stay in one place."""

    async def __call__(
        self, prompt: str, *, role: str = "unknown", model: Optional[str] = None,
    ) -> str:
        # Local import: planner imports from this module would cycle on the
        # `claude_with_model` shim that delegates back here.
        from .planner import call_claude

        return await call_claude(prompt, model=model, role=role)


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
    ) -> str:
        from .loom import trace as _trace

        response = self.responses.get(role, self.default)
        self.calls.append((role, model or "", prompt))
        _trace.record_cognition(
            role=role, model=model or "(stub)", prompt=prompt,
            response=response, latency_ms=0,
        )
        return response


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
    raise ValueError(
        f"unknown DEVCLAW_COGNITION={name!r}; supported: claude, stub"
    )


def bind(model: Optional[str], *, role: str = "unknown") -> Callable[[str], Awaitable[str]]:
    """Convenience: return a one-arg caller bound to (model, role) via the
    configured cognition. Each role's ``default_caller`` uses this so the
    swap point is centralized."""

    async def _caller(prompt: str) -> str:
        return await get_cognition()(prompt, role=role, model=model)

    return _caller
