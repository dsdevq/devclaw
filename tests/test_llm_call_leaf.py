"""llm_call is a LEAF module — the quality gate imports it without the planner.

The quality gate needed exactly three symbols (`PlannerError`,
`claude_with_model`, `extract_json`) but imported them from `planner`, which
drags `state_store` + `task_git` and closed the
``quality → planner → loom → goal`` import cycle. The primitive now lives in
``llm_call.py`` (only internal dep: ``loom.trace``, itself pure stdlib) and
``planner`` re-exports it for back-compat. These pin the leaf-ness, the
rewiring, and the re-export contract so the cycle can't silently return.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def test_llm_call_imports_without_planner_or_state_store():
    # Fresh interpreter: importing the leaf must not pull the heavy modules.
    code = (
        "import sys; import devclaw.llm_call; "
        "heavy = [m for m in ('devclaw.planner', 'devclaw.state_store', "
        "'devclaw.task_git', 'devclaw.task_queue', 'devclaw.goal') "
        "if m in sys.modules]; "
        "assert not heavy, f'leaf pulled heavy modules: {heavy}'; print('leaf-ok')"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=_REPO
    )
    assert out.returncode == 0, out.stderr
    assert "leaf-ok" in out.stdout


def test_quality_modules_import_llm_call_not_planner():
    # Static source pin: the gate's modules must never re-grow the planner
    # import (that would silently re-close the quality → planner cycle).
    for mod in ("__init__.py", "eval_judge.py", "reachability.py"):
        src = (_REPO / "devclaw" / "quality" / mod).read_text()
        assert "from ..llm_call import" in src, mod
        assert "from ..planner import" not in src, mod


async def _no_spawn(*argv, **kwargs):  # pragma: no cover - must not be reached
    raise AssertionError("subprocess must not spawn in this test")


def test_call_claude_strips_api_keys_from_subprocess_env(monkeypatch):
    # The OAuth-only invariant, pinned on the subprocess path itself: the env
    # dict handed to create_subprocess_exec must never carry an API key —
    # a stray key must not silently switch cognition onto metered billing.
    import asyncio as real_asyncio

    from devclaw import llm_call

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-leak")
    seen: dict = {}

    async def fake_spawn(*argv, **kwargs):
        seen["env"] = kwargs.get("env")
        raise OSError("stop here — env captured")

    monkeypatch.setattr(llm_call.asyncio, "create_subprocess_exec", fake_spawn)
    with pytest.raises(llm_call.PlannerError):
        real_asyncio.run(llm_call.call_claude("hi"))
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in seen["env"]


def test_planner_reexports_are_the_same_objects():
    # Back-compat contract: `from .planner import …` call sites (and tests
    # patching planner.call_claude — cognition resolves it lazily through the
    # planner namespace) keep working because planner re-exports the SAME
    # objects, not copies.
    from devclaw import llm_call, planner

    for name in (
        "PlannerError",
        "call_claude",
        "claude_with_model",
        "extract_json",
        "parse_cli_envelope",
        "CliEnvelope",
        "CLAUDE_BIN",
        "PLANNER_TIMEOUT_MS",
    ):
        assert getattr(planner, name) is getattr(llm_call, name), name
