"""Regression tests for the runner-side browser-report parser
(``_read_browser_report`` in openhands-runner/runner.py) — the proof-of-execution
crux: the host browser-gate trusts these parsed Playwright counts as evidence a
real browser ran. A silent parse regression would make every UI change read as
never-ran (fail closed forever), so the parse is pinned here.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNNER_PATH = _REPO_ROOT / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_browser_report", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_report(ws: Path, payload) -> None:
    p = ws / ".devclaw" / "playwright-report.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def test_parses_well_formed_stats_into_coerced_counts(runner, tmp_path):
    _write_report(tmp_path, {"stats": {"expected": 6, "unexpected": 1, "flaky": 0, "skipped": 2}})
    got = runner._read_browser_report(str(tmp_path))
    assert got == {"expected": 6, "unexpected": 1, "flaky": 0, "skipped": 2}


def test_coerces_missing_and_stringy_counts(runner, tmp_path):
    # A partial/stringy stats block still yields ints (defaults 0) — the verdict
    # layer keys off these, so they must be numbers, never None/str.
    _write_report(tmp_path, {"stats": {"expected": "4"}})
    got = runner._read_browser_report(str(tmp_path))
    assert got == {"expected": 4, "unexpected": 0, "flaky": 0, "skipped": 0}


def test_all_skipped_run_still_returns_counts(runner, tmp_path):
    # 0 executed is the scar case — the PARSER returns the counts faithfully;
    # deciding "never_ran" from executed==0 is the host verdict's job.
    _write_report(tmp_path, {"stats": {"expected": 0, "unexpected": 0, "flaky": 0, "skipped": 9}})
    assert runner._read_browser_report(str(tmp_path)) == {
        "expected": 0, "unexpected": 0, "flaky": 0, "skipped": 9
    }


def test_missing_artifact_returns_none(runner, tmp_path):
    assert runner._read_browser_report(str(tmp_path)) is None


def test_garbled_json_returns_none(runner, tmp_path):
    _write_report(tmp_path, "{not valid json")
    assert runner._read_browser_report(str(tmp_path)) is None


def test_report_without_stats_returns_none(runner, tmp_path):
    _write_report(tmp_path, {"suites": []})  # no stats block at all
    assert runner._read_browser_report(str(tmp_path)) is None


def test_stats_without_any_count_keys_returns_none(runner, tmp_path):
    _write_report(tmp_path, {"stats": {"duration": 1234, "startTime": "..."}})
    assert runner._read_browser_report(str(tmp_path)) is None
