"""Elicitation primitives — step validation and next_step.

The one-shot ``build_project`` interview flow (and its ProjectService/ProjectStore)
was removed as vault-rejected spec-kit drift. The grill primitives stay because the
durable per-goal grill (``goal_grill``, off by default until the Telegram answer
channel is validated) still reuses them.
"""

import json

import pytest

from devclaw.elicitation import next_step, validate_step
from devclaw.planner import PlannerError


# ---- validate_step ----


def test_validate_ask_step():
    s = validate_step({"action": "ask", "question": "What stack?", "recommended": "Next.js"})
    assert s == {"action": "ask", "question": "What stack?", "recommended": "Next.js"}


def test_validate_done_step():
    s = validate_step({"action": "done", "spec": "# spec\n## Goal\nx"})
    assert s["action"] == "done" and s["spec"].startswith("# spec")


def test_validate_ask_without_question_rejected():
    with pytest.raises(PlannerError):
        validate_step({"action": "ask", "recommended": "x"})


def test_validate_done_without_spec_rejected():
    with pytest.raises(PlannerError):
        validate_step({"action": "done"})


def test_validate_unknown_action_rejected():
    with pytest.raises(PlannerError):
        validate_step({"action": "build_it_now"})


# ---- next_step ----


async def test_next_step_asks_then_finalizes():
    async def ask_stub(_prompt):
        return json.dumps({"action": "ask", "question": "Who is it for?", "recommended": "devs"})

    step = await next_step("a CLI tool", [], ask_stub)
    assert step["action"] == "ask" and step["question"] == "Who is it for?"

    async def done_stub(_prompt):
        return '```json\n{"action":"done","spec":"# spec\\n## Goal\\nship it"}\n```'

    step = await next_step("a CLI tool", [{"question": "q", "recommended": "r", "answer": "a"}], done_stub)
    assert step["action"] == "done" and "ship it" in step["spec"]
