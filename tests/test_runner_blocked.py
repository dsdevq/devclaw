"""In-sandbox honest-exit — the structured ``status="blocked"`` self-report.

The task prompt already instructs the engineer to end its final hand-back with
``STATUS: DONE`` or ``BLOCKED: <reason>`` when it genuinely cannot finish, but
that string used to ride invisibly inside ``agent_output`` and never touched the
done/failed decision — so a truly-stuck agent either fabricated a plausible
result or failed generically and got retried pointlessly. The runner now parses
its OWN final message for that self-report and promotes it to a first-class
terminal ``status="blocked"`` (+ ``reason``). These pin the parser and prove the
prompt-echo can't false-positive (the parse reads the agent message, not the
captured decorative stdout that echoes the literal contract text).
"""

import importlib.util
import io
import json
from pathlib import Path

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_blocked", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # top-level only; openhands imports live in main()
    return mod


# ---- parser: the BLOCKED self-report ----------------------------------------


def test_bare_blocked_line_is_parsed(runner):
    msg = (
        "I looked into it but the repo needs a paid API key I don't have.\n"
        "BLOCKED: the integration needs a Stripe secret key not present in the repo\n"
    )
    assert (
        runner._parse_blocked_reason(msg)
        == "the integration needs a Stripe secret key not present in the repo"
    )


def test_status_prefixed_blocked_is_parsed(runner):
    msg = (
        "CHANGED: nothing shippable.\n"
        "STATUS: BLOCKED: the task contradicts itself — cannot both keep and remove X\n"
    )
    assert (
        runner._parse_blocked_reason(msg)
        == "the task contradicts itself — cannot both keep and remove X"
    )


def test_markdown_decorated_blocked_is_parsed(runner):
    # models often bold/bullet the field — strip light decoration.
    msg = "**BLOCKED:** missing capability\n"
    assert runner._parse_blocked_reason(msg) == "missing capability"


def test_status_done_is_not_blocked(runner):
    msg = "STATUS: DONE\nCHANGED: added the endpoint.\nFOLLOW-UPS: none\n"
    assert runner._parse_blocked_reason(msg) is None


def test_mid_sentence_blocked_prose_does_not_false_positive(runner):
    # the load-bearing negative: only a line-start BLOCKED: (the contract field)
    # counts — prose that mentions being blocked must NOT fail a done task.
    msg = (
        "STATUS: DONE\n"
        "FOLLOW-UPS: I was briefly blocked: on a flaky test but retried and it passed.\n"
    )
    assert runner._parse_blocked_reason(msg) is None


def test_last_blocked_line_wins(runner):
    msg = "BLOCKED: first pass hit a wall\nlater...\nBLOCKED: real final reason\n"
    assert runner._parse_blocked_reason(msg) == "real final reason"


def test_blocked_with_empty_reason_still_surfaces(runner):
    # an honest "I'm blocked" with no stated reason must still be a block, never
    # silently lost as "no reason ⇒ not blocked".
    assert runner._parse_blocked_reason("BLOCKED:\n") is not None


def test_empty_and_none_are_not_blocked(runner):
    assert runner._parse_blocked_reason("") is None
    assert runner._parse_blocked_reason(None) is None


# ---- agent-message extraction from a MessageEvent payload -------------------


def test_agent_message_text_concatenates_text_parts(runner):
    payload = {
        "llm_message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "first "},
                {"type": "image", "image_url": "..."},
                {"type": "text", "text": "second"},
            ],
        }
    }
    assert runner._agent_message_text(payload) == "first second"


def test_agent_message_text_degrades_to_empty_on_bad_shape(runner):
    assert runner._agent_message_text({}) == ""
    assert runner._agent_message_text({"llm_message": None}) == ""
    assert runner._agent_message_text("not a dict") == ""


# ---- emission: the terminal `result:` line ----------------------------------


def test_blocked_payload_emits_structured_result(runner, monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(runner, "_PROTO_OUT", out)
    runner._emit_result(
        {"status": "blocked", "reason": "cannot access the private registry",
         "workspace_dir": "/ws", "agent_output": "banner"}
    )
    line = out.getvalue()
    assert line.startswith("result: ") and line.endswith("\n")
    result = json.loads(line[len("result: "):])
    assert result["status"] == "blocked"
    assert result["reason"] == "cannot access the private registry"
