"""Model-tiering tests — per-role model selection on the cognition calls.

Running every `claude` call on the account default (Opus) burns the Pro/Max
quota fast; each role binds its own tier. These tests pin the argv construction,
the model-binding factory, the shipped per-role defaults, and that each role's
default caller is actually wired to its tier (a regression guard — it's easy to
silently revert a default back to the untiered `call_claude`).
"""

import inspect

import pytest

from devclaw import eval_judge, elicitation
from devclaw import planner
from devclaw.engine import sandcastle as sandcastle_runner
from devclaw.planner import _build_claude_argv, call_claude, claude_with_model


# ---- argv construction (pure) ----


def test_argv_includes_model_when_set():
    argv = _build_claude_argv("do the thing", "sonnet")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[-1] == "do the thing"  # prompt stays last
    assert argv[:3] == [planner.CLAUDE_BIN, "--print", "--output-format=text"]


def test_argv_omits_model_when_none():
    argv = _build_claude_argv("p", None)
    assert "--model" not in argv  # → CLI account default
    assert argv[-1] == "p"


# ---- the model-binding factory ----


async def test_claude_with_model_forwards_model(monkeypatch):
    captured = {}

    async def fake_call(prompt, model=None, *, role="unknown"):
        captured["prompt"] = prompt
        captured["model"] = model
        captured["role"] = role
        return "ok"

    monkeypatch.setattr(planner, "call_claude", fake_call)
    caller = claude_with_model("claude-opus-4-8", role="planner")
    out = await caller("hello")
    assert out == "ok"
    assert captured == {"prompt": "hello", "model": "claude-opus-4-8", "role": "planner"}


# ---- shipped per-role defaults (intent, and a guard against accidental change) ----


def test_shipped_default_tiers():
    assert planner.PLANNER_MODEL == "opus"  # rare + high-leverage
    assert elicitation.GRILL_MODEL == "sonnet"  # conversational
    assert eval_judge.JUDGE_MODEL == "haiku"  # bounded classification
    assert sandcastle_runner.EXEC_MODEL == "claude-sonnet-4-6"  # the coding bulk


# ---- each role's default caller is wired to its tier ----


def test_role_default_callers_are_tiered():
    # plan_goal / plan_spec → planner tier
    assert inspect.signature(planner.plan_goal).parameters["claude_caller"].default is planner._planner_caller
    assert inspect.signature(planner.plan_spec).parameters["claude_caller"].default is planner._planner_caller
    # grill → grill tier (next_step binds lazily via default_caller; assert the
    # factory routes the configured tier)
    assert elicitation.default_caller.__module__ == "devclaw.elicitation"
    # judge → judge tier
    assert inspect.signature(eval_judge.judge_run).parameters["claude_caller"].default is eval_judge.judge_caller


def test_call_claude_accepts_model_kwarg():
    # signature contract the role callers rely on
    params = inspect.signature(call_claude).parameters
    assert "model" in params and params["model"].default is None
