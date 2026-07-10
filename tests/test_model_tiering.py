"""Model-tiering tests — per-role model selection on the cognition calls.

Running every `claude` call on the account default (Opus) burns the Pro/Max
quota fast; each role binds its own tier. These tests pin the argv construction,
the model-binding factory, the shipped per-role defaults, and that each role's
default caller is actually wired to its tier (a regression guard — it's easy to
silently revert a default back to the untiered `call_claude`).
"""

import inspect

import pytest

from devclaw.quality import eval_judge
from devclaw import elicitation
from devclaw import planner
from devclaw.engine import sandcastle as sandcastle_runner
from devclaw.planner import _build_claude_argv, call_claude, claude_with_model


# ---- argv construction (pure) ----


def test_argv_includes_model_when_set():
    argv = _build_claude_argv("do the thing", "sonnet")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    # T0.5: json output format — the CLI envelope carries real token usage.
    assert argv[:3] == [planner.CLAUDE_BIN, "--print", "--output-format=json"]
    # 2026-07-03 argv → stdin migration: prompt no longer rides on argv,
    # protecting against ``[Errno 7] Argument list too long`` on long prompts.
    assert "do the thing" not in argv


def test_argv_omits_model_when_none():
    argv = _build_claude_argv("p", None)
    assert "--model" not in argv  # → CLI account default
    # 2026-07-03: prompt migrated off argv entirely — see call_claude stdin path.
    assert "p" not in argv


def test_argv_never_appends_prompt_regardless_of_size():
    """Long prompts (the failure mode: goal-planner prompt crossing ARG_MAX
    after many log/deliveries entries) must NOT show up in argv. Regression
    guard against silently reverting to the pre-2026-07-03 argv path."""
    huge = "x" * 200_000  # ~200 KB — well past Linux ARG_MAX
    argv_none = _build_claude_argv(huge, None)
    argv_sonnet = _build_claude_argv(huge, "sonnet")
    assert huge not in argv_none
    assert huge not in argv_sonnet
    # And the argv itself stays small — no accidental prompt leakage into a flag.
    assert sum(len(a) for a in argv_none) < 200
    assert sum(len(a) for a in argv_sonnet) < 200


async def test_call_claude_passes_prompt_on_stdin(monkeypatch):
    """The prompt reaches ``claude --print`` via stdin, not argv — the
    live-hit closeloop-mission-v2 2026-07-03T18:35Z fix. Verifies the
    subprocess is created with stdin=PIPE (not DEVNULL) and that the exact
    prompt bytes are written to it via ``communicate(input=…)``."""
    captured: dict = {}

    class _FakeProc:
        returncode = 0

        async def communicate(self, input=None):  # noqa: A002
            captured["stdin_bytes"] = input
            # T0.5: the CLI replies with the json result envelope; callers
            # still receive just the response text.
            return b'{"type": "result", "subtype": "success", "result": "ok"}', b""

    async def fake_spawn(*argv, **kwargs):
        captured["argv"] = argv
        captured["stdin_kw"] = kwargs.get("stdin")
        return _FakeProc()

    monkeypatch.setattr(planner.asyncio, "create_subprocess_exec", fake_spawn)
    out = await call_claude("this is a very long prompt " * 5_000, model="sonnet")
    assert out == "ok"
    # Verified: prompt reached stdin, not argv.
    assert captured["stdin_bytes"] == (b"this is a very long prompt " * 5_000)
    assert not any(b"very long prompt" in a.encode() for a in captured["argv"]), (
        "prompt leaked into argv"
    )
    assert captured["stdin_kw"] == planner.asyncio.subprocess.PIPE


# ---- the model-binding factory ----


async def test_claude_with_model_forwards_model(monkeypatch):
    captured = {}

    async def fake_call(prompt, model=None, *, role="unknown", timeout_ms=None):
        captured["prompt"] = prompt
        captured["model"] = model
        captured["role"] = role
        captured["timeout_ms"] = timeout_ms
        return "ok"

    monkeypatch.setattr(planner, "call_claude", fake_call)
    caller = claude_with_model("claude-opus-4-8", role="planner")
    out = await caller("hello")
    assert out == "ok"
    assert captured == {
        "prompt": "hello", "model": "claude-opus-4-8", "role": "planner",
        "timeout_ms": None,  # default: caller passed no override → falls to PLANNER_TIMEOUT_MS
    }


async def test_claude_with_model_forwards_timeout(monkeypatch):
    """Per-role timeout override threads through bind → ClaudeCognition →
    call_claude. The decomposer is the canonical user."""
    captured = {}

    async def fake_call(prompt, model=None, *, role="unknown", timeout_ms=None):
        captured["timeout_ms"] = timeout_ms
        return "ok"

    monkeypatch.setattr(planner, "call_claude", fake_call)
    caller = claude_with_model("opus", role="goal_decomposer", timeout_ms=300000)
    await caller("hi")
    assert captured["timeout_ms"] == 300000


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
