"""T0.5 — ``claude --print --output-format json`` envelope handling.

``call_claude`` now asks the CLI for the JSON result envelope so the trace
records REAL token usage + cost instead of len/4 guesses. These tests pin:

  * envelope parsing against the REAL captured shape (one dev-time live call,
    2026-07-10, scrubbed → ``tests/fixtures/claude_print_json_envelope.json``);
  * the error-subtype → PlannerError path (envelope wording preserved);
  * the raw-stdout fallback when stdout isn't the envelope (behavior identical
    to pre-json mode, plus a one-line stderr breadcrumb);
  * THE QUOTA INVARIANT: a usage-limit failure surfaces as plain text on
    STDOUT with an EMPTY stderr, and the pause machinery keys off
    ``classify_failure`` regexing the PlannerError message — the raw wording
    must land in that message verbatim, json mode or not.

No test here invokes the real CLI — the subprocess seam is monkeypatched
exactly like test_model_tiering does.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from devclaw import planner
from devclaw.loom import trace as _trace
from devclaw.loom.limits import FailureKind, classify_failure
from devclaw.planner import PlannerError, call_claude, parse_cli_envelope

FIXTURE = Path(__file__).parent / "fixtures" / "claude_print_json_envelope.json"


def _fixture_text() -> str:
    return FIXTURE.read_text()


def _fake_subprocess(monkeypatch, *, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
    """Stub the subprocess seam the same way test_model_tiering does."""

    class _FakeProc:
        def __init__(self) -> None:
            self.returncode = returncode

        async def communicate(self, input=None):  # noqa: A002
            return stdout, stderr

    async def fake_spawn(*_argv, **_kwargs):
        return _FakeProc()

    monkeypatch.setattr(planner.asyncio, "create_subprocess_exec", fake_spawn)


# ---- parse_cli_envelope (pure) ----------------------------------------------


def test_parse_captured_fixture_envelope():
    """The scrubbed capture of a REAL CLI reply parses into exactly the fields
    devclaw consumes."""
    env = parse_cli_envelope(_fixture_text())
    assert env is not None
    assert env.is_error is False
    assert env.subtype == "success"
    assert env.result_text == "OK"
    assert env.tokens_in == 10
    assert env.tokens_out == 38
    assert env.cache_read == 17209
    assert env.cache_creation == 8201
    assert env.cost_usd == pytest.approx(0.0188989)


def test_parse_non_json_returns_none():
    assert parse_cli_envelope("You're out of extra usage · resets 10pm (UTC)") is None
    assert parse_cli_envelope("") is None


def test_parse_json_that_is_not_the_envelope_returns_none():
    # A model's own JSON (e.g. a plan) must NOT be mistaken for the envelope —
    # `type: "result"` is the discriminator.
    assert parse_cli_envelope('{"tasks": [{"key": "t1", "goal": "g"}]}') is None
    assert parse_cli_envelope("[1, 2, 3]") is None
    assert parse_cli_envelope('"just a string"') is None


def test_parse_success_without_string_result_returns_none():
    # Envelope-shaped but no usable text → raw fallback, not a crash.
    assert parse_cli_envelope('{"type": "result", "subtype": "success", "result": null}') is None
    assert parse_cli_envelope('{"type": "result", "subtype": "success"}') is None


def test_parse_error_envelope_prefers_result_wording():
    env = parse_cli_envelope(json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "You're out of extra usage · resets 10pm (UTC)",
    }))
    assert env is not None and env.is_error
    assert env.error_text == "You're out of extra usage · resets 10pm (UTC)"


def test_parse_error_envelope_without_result_keeps_whole_envelope():
    # No `result` wording → the whole envelope becomes the error text so no
    # failure wording is ever lost to the classifier.
    env = parse_cli_envelope('{"type": "result", "subtype": "error_max_turns", "is_error": true}')
    assert env is not None and env.is_error
    assert "error_max_turns" in env.error_text


# ---- call_claude: success envelope ------------------------------------------


async def test_call_claude_unwraps_envelope_and_traces_real_usage(monkeypatch):
    """Success envelope → callers receive the result TEXT (contract unchanged)
    and the cognition trace carries real tokens + cost."""
    _fake_subprocess(monkeypatch, stdout=_fixture_text().encode())
    tracer = _trace.Tracer()
    with _trace.tracer_scope(tracer):
        out = await call_claude("Reply with exactly: OK", model="haiku", role="planner")
    assert out == "OK"
    (e,) = tracer.by_kind("cognition")
    assert e["response_text"] == "OK"
    assert e["tokens_in"] == 10
    assert e["tokens_out"] == 38
    assert e["cache_read"] == 17209
    assert e["cache_creation"] == 8201
    assert e["cost_usd"] == pytest.approx(0.0188989)
    assert e["error"] == ""
    # est fields are still populated for consumers that read them
    assert e["tokens_in_est"] == len("Reply with exactly: OK") // 4


async def test_call_claude_result_may_itself_be_json(monkeypatch):
    """The model's own JSON (a plan) rides INSIDE the envelope's result field;
    callers get it back as text and parse it themselves."""
    plan = json.dumps({"tasks": [{"key": "t1", "goal": "do it"}]})
    envelope = json.dumps({"type": "result", "subtype": "success", "result": plan})
    _fake_subprocess(monkeypatch, stdout=envelope.encode())
    out = await call_claude("plan it")
    assert json.loads(out) == {"tasks": [{"key": "t1", "goal": "do it"}]}


# ---- call_claude: error envelope --------------------------------------------


async def test_error_subtype_envelope_raises_planner_error_with_wording(monkeypatch):
    envelope = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": "Execution failed: something exploded",
    })
    _fake_subprocess(monkeypatch, stdout=envelope.encode(), returncode=0)
    tracer = _trace.Tracer()
    with _trace.tracer_scope(tracer):
        with pytest.raises(PlannerError) as ei:
            await call_claude("p", role="evaluator")
    assert "Execution failed: something exploded" in str(ei.value)
    (e,) = tracer.by_kind("cognition")
    assert "cli error envelope" in e["error"]


# ---- call_claude: raw-stdout fallback ----------------------------------------


async def test_garbage_stdout_falls_back_to_legacy_raw_text(monkeypatch, capsys):
    """Exit 0, stdout not the envelope → treated exactly like pre-json mode:
    raw stdout IS the response, with a one-line stderr breadcrumb. Trace keeps
    only the est fields (no real usage invented)."""
    _fake_subprocess(monkeypatch, stdout=b"plain prose the CLI printed")
    tracer = _trace.Tracer()
    with _trace.tracer_scope(tracer):
        out = await call_claude("p")
    assert out == "plain prose the CLI printed"
    assert "did not parse as a JSON result envelope" in capsys.readouterr().err
    (e,) = tracer.by_kind("cognition")
    assert e["response_text"] == "plain prose the CLI printed"
    assert e["tokens_in"] is None and e["tokens_out"] is None and e["cost_usd"] is None
    assert e["tokens_out_est"] == len("plain prose the CLI printed") // 4


# ---- THE QUOTA INVARIANT ------------------------------------------------------


async def test_quota_plain_text_on_stdout_under_json_mode(monkeypatch):
    """CRITICAL: usage-limit failures surface as plain text on STDOUT with an
    EMPTY stderr and a non-zero exit. Under json mode that stdout does NOT
    parse as the envelope — the raw wording must still land VERBATIM in the
    PlannerError message, because classify_failure regexes exactly that
    message to pause the whole layer instead of crash-looping."""
    quota = "You're out of extra usage · resets 10pm (UTC)"
    _fake_subprocess(monkeypatch, stdout=quota.encode(), stderr=b"", returncode=1)
    with pytest.raises(PlannerError) as ei:
        await call_claude("p", role="goal_planner")
    msg = str(ei.value)
    assert quota in msg  # verbatim — the classifier's regexes hit this
    cls = classify_failure(msg, now_utc=datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc))
    assert cls.kind is FailureKind.QUOTA
    assert cls.is_pausing
    assert cls.stated and cls.retry_after_s  # "resets 10pm" hint parsed
    # raw stdout preserved on .raw for forensics
    assert ei.value.raw == quota


async def test_quota_wording_inside_json_error_envelope(monkeypatch):
    """If the CLI instead WRAPS the usage-limit wording in a json error
    envelope (non-zero exit), the envelope's error text must flow verbatim
    into the message — same classifier, same pause."""
    quota = "You're out of extra usage · resets 10pm (UTC)"
    envelope = json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "result": quota,
    })
    _fake_subprocess(monkeypatch, stdout=envelope.encode(), stderr=b"", returncode=1)
    with pytest.raises(PlannerError) as ei:
        await call_claude("p")
    msg = str(ei.value)
    assert quota in msg
    assert classify_failure(msg).kind is FailureKind.QUOTA


async def test_nonzero_exit_with_stderr_keeps_stderr_in_message(monkeypatch):
    """The pre-existing stderr path is untouched: non-zero exit with real
    stderr still surfaces it (auth errors etc. stay classifiable)."""
    _fake_subprocess(
        monkeypatch, stdout=b"", stderr=b"failed to authenticate", returncode=1,
    )
    with pytest.raises(PlannerError) as ei:
        await call_claude("p")
    assert "failed to authenticate" in str(ei.value)
    assert classify_failure(str(ei.value)).kind is FailureKind.REAL
