"""In-sandbox usage-limit detection — the structured `status="rate_limited"` signal.

The whole limit stack used to hang on HOST-side regexing of error text; the
`rate_limited` consumer branch in `TaskQueue._run_and_settle` was dead code
because nothing emitted it. The runner now carries a vendored subset of
devclaw/loom/limits.py (it runs inside the sandbox without the devclaw package)
and flips a CLEAR limit error to `status="rate_limited"` + `retry_after`, with
the original text preserved. Conservative by contract: false negatives are fine
(the host regex fallback still classifies the raw text), false positives are
not. These pin the detector against the live-observed wordings and prove the
terminal `result:` line carries the structured shape.
"""

import importlib.util
import io
import json
from pathlib import Path

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_limits", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # top-level only; openhands imports live in main()
    return mod


# ---- detector: the vendored QUOTA/RATE subset -------------------------------


def test_out_of_extra_usage_wording_is_a_limit(runner):
    # live-observed Claude Code wording (dogfood finding 2026-06-20)
    matched, retry_after = runner._detect_usage_limit(
        "Internal error: You're out of extra usage · resets 10pm (UTC)"
    )
    assert matched is True
    assert retry_after is None  # absolute reset time — intentionally not parsed


def test_session_limit_wording_is_a_limit(runner):
    # live-observed 5-hour session cap wording (dogfood finding 2026-06-21)
    matched, retry_after = runner._detect_usage_limit(
        "You've hit your session limit · resets 12:20am"
    )
    assert matched is True
    assert retry_after is None


def test_429_too_many_requests_is_a_limit(runner):
    matched, _ = runner._detect_usage_limit("HTTP 429: too many requests")
    assert matched is True


def test_plain_error_is_not_a_limit(runner):
    # the load-bearing negative: a real bug must NOT be paused on
    matched, retry_after = runner._detect_usage_limit(
        "TypeError: cannot read properties of undefined (reading 'foo')"
    )
    assert matched is False and retry_after is None


def test_empty_and_none_are_not_limits(runner):
    assert runner._detect_usage_limit("") == (False, None)
    assert runner._detect_usage_limit(None) == (False, None)


def test_relative_retry_hint_is_parsed_to_seconds(runner):
    matched, retry_after = runner._detect_usage_limit(
        "rate limit exceeded; try again in 5 minutes"
    )
    assert matched is True
    assert retry_after == 300


def test_bare_retry_after_header_is_seconds(runner):
    matched, retry_after = runner._detect_usage_limit(
        "429 Too Many Requests. Retry-After: 30"
    )
    assert matched is True
    assert retry_after == 30


def test_auth_shaped_text_is_never_a_limit(runner):
    # The runner must NOT tag auth text rate_limited: it flows through as a
    # plain error so the HOST classifier (loom/limits.py) sees the original
    # wording and routes it onto the AUTH pause path (fixed re-probe +
    # actionable re-login ping — 2026-07-20 night incident).
    matched, retry_after = runner._detect_usage_limit(
        "401 rate limit: invalid authentication, please run /login"
    )
    assert matched is False and retry_after is None


def test_authentication_required_flows_through_as_plain_error(runner):
    # the exact worker wording from the 2026-07-20 night — must not be tagged
    # rate_limited here (the host classifies it AUTH from the original text)
    matched, retry_after = runner._detect_usage_limit(
        "Conversation run failed: Authentication required (failed after 2 attempts)"
    )
    assert matched is False and retry_after is None


# ---- emission: the terminal `result:` line ----------------------------------


def _emitted_result(runner, monkeypatch, payload: dict) -> dict:
    """Emit through the real result-line seam and parse what the host would see."""
    out = io.StringIO()
    monkeypatch.setattr(runner, "_PROTO_OUT", out)
    runner._emit_result(payload)
    line = out.getvalue()
    assert line.startswith("result: ") and line.endswith("\n")
    return json.loads(line[len("result: "):])


def test_limit_shaped_failure_emits_rate_limited_with_original_text(runner, monkeypatch):
    original = "You've hit your session limit · resets 12:20am"
    result = _emitted_result(
        runner, monkeypatch,
        runner._failure_result(original, trace="tb", agent_output="banner"),
    )
    assert result["status"] == "rate_limited"
    assert result["error"] == original  # host regex fallback needs the raw text
    assert result["retry_after"] is None  # no relative hint stated
    assert result["trace"] == "tb" and result["agent_output"] == "banner"


def test_limit_failure_with_relative_hint_carries_retry_after_seconds(runner, monkeypatch):
    result = _emitted_result(
        runner, monkeypatch,
        runner._failure_result("rate limited — try again in 2 minutes"),
    )
    assert result["status"] == "rate_limited"
    assert result["retry_after"] == 120


def test_plain_failure_still_emits_status_error(runner, monkeypatch):
    result = _emitted_result(
        runner, monkeypatch,
        runner._failure_result("boom: the agent crashed", trace="tb"),
    )
    assert result["status"] == "error"
    assert result["error"] == "boom: the agent crashed"
    assert "retry_after" not in result  # plain errors keep the pre-existing shape
