"""Cognition-timeout headroom + configurability — regression tests.

Production evidence (2026-07-14/15 night): five `claude --print` cognition
calls timed out at exactly the old hardcoded 90s cap on MODERATE prompts,
while successful calls on the same prompts ran 50–78s at peak hours — p90
hugged the cap, each timeout burned a full model call plus a 15-minute tick,
and three consecutive planner timeouts contributed to a goal blocking itself.

The default ceiling is now 180s, env-configurable via
``DEVCLAW_COGNITION_TIMEOUT_S`` (seconds); an explicit per-call ``timeout_ms``
still wins (decomposer/review/grill keep their role-level budgets). Invalid or
``<= 0`` values fall back to the default — a typo in ``.env`` must never crash
import. Scope is headroom + config ONLY: no retries were added and the
timeout's fail-closed classification is untouched.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from devclaw import planner


# ---- the env parse (pure, fail-safe) ----


def test_cognition_timeout_default_is_180s():
    """Unset / empty env → the shipped 180_000 ms default."""
    assert planner._cognition_timeout_ms_from_env(None) == 180_000
    assert planner._cognition_timeout_ms_from_env("") == 180_000


def test_cognition_timeout_env_override_respected():
    assert planner._cognition_timeout_ms_from_env("240") == 240_000
    assert planner._cognition_timeout_ms_from_env(" 90 ") == 90_000


def test_cognition_timeout_invalid_or_nonpositive_falls_back_to_default():
    """Fail-safe, never crash import: garbage, floats, zero and negatives all
    degrade to the default instead of raising or disabling the ceiling."""
    for bad in ("abc", "1.5", "0", "-5", "  "):
        assert planner._cognition_timeout_ms_from_env(bad) == 180_000, bad


# ---- the module-level constant actually derives from the env at import ----

_REPO_ROOT = Path(planner.__file__).resolve().parents[1]
_PRINT_TIMEOUT = "import devclaw.planner as p; print(p.PLANNER_TIMEOUT_MS)"


def _import_time_timeout(env_value: str | None) -> str:
    """Import devclaw.planner in a FRESH interpreter (the constant is read at
    import time, same pattern as CLAUDE_BIN) and print PLANNER_TIMEOUT_MS.
    cwd is pinned to this checkout's root so the tree under test wins over the
    shared venv's editable .pth (see .claude/rules/testing.md)."""
    env = {k: v for k, v in os.environ.items() if k != "DEVCLAW_COGNITION_TIMEOUT_S"}
    if env_value is not None:
        env["DEVCLAW_COGNITION_TIMEOUT_S"] = env_value
    out = subprocess.run(
        [sys.executable, "-c", _PRINT_TIMEOUT],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def test_planner_timeout_ms_defaults_to_180s_at_import():
    assert _import_time_timeout(None) == "180000"


def test_planner_timeout_ms_env_override_respected_at_import():
    assert _import_time_timeout("240") == "240000"


def test_planner_timeout_ms_invalid_env_falls_back_at_import():
    assert _import_time_timeout("not-a-number") == "180000"


# ---- per-call override still wins inside call_claude ----


class _FakeProc:
    returncode = 0

    async def communicate(self, input=None):  # noqa: A002
        return b'{"type": "result", "subtype": "success", "result": "ok"}', b""


def _spy_timeout(monkeypatch, seen: dict) -> None:
    async def fake_spawn(*argv, **kwargs):
        return _FakeProc()

    real_wait_for = asyncio.wait_for

    async def spy_wait_for(aw, timeout):
        seen["timeout_s"] = timeout
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(planner.asyncio, "create_subprocess_exec", fake_spawn)
    monkeypatch.setattr(planner.asyncio, "wait_for", spy_wait_for)


async def test_explicit_per_call_timeout_ms_still_wins(monkeypatch):
    """Roles that pass their own budget (decomposer 300s, review 180s) must keep
    it — the env-derived default only fills in when the caller passes nothing."""
    seen: dict = {}
    _spy_timeout(monkeypatch, seen)
    out = await planner.call_claude("p", timeout_ms=300_000)
    assert out == "ok"
    assert seen["timeout_s"] == 300.0


async def test_call_without_override_uses_the_default_ceiling(monkeypatch):
    seen: dict = {}
    _spy_timeout(monkeypatch, seen)
    out = await planner.call_claude("p")
    assert out == "ok"
    assert seen["timeout_s"] == planner.PLANNER_TIMEOUT_MS / 1000
