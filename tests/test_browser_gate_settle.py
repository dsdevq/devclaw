"""Regression tests for the browser-gate settle wiring
(``_browser_gate_failure`` + ``_has_playwright_config`` in task_queue).

These assert the host-side half of the gate: a web-UI change with no passing
real-browser run fails CLOSED (a feed-back reason → the retry loop, never a
silent 'done'), a passing run proceeds, a backend-only change is untouched, and
a project with no browser suite isn't wedged under the default (flexible) mode
but is under strict. The pure verdict is covered in test_browser_gate.py; this
covers config detection + enablement + mode against a real workspace on disk.
"""

from __future__ import annotations

import devclaw.task_queue as tq
from devclaw.task_queue import _browser_gate_failure, _has_playwright_config

_FRONTEND_DIFF = (
    "diff --git a/frontend/src/app/select/select.component.ts "
    "b/frontend/src/app/select/select.component.ts\n"
    "--- a/frontend/src/app/select/select.component.ts\n"
    "+++ b/frontend/src/app/select/select.component.ts\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)
_BACKEND_DIFF = (
    "diff --git a/backend/Api/ContactsController.cs b/backend/Api/ContactsController.cs\n"
    "--- a/backend/Api/ContactsController.cs\n+++ b/backend/Api/ContactsController.cs\n"
    "@@ -1 +1 @@\n-old\n+new\n"
)


def _verify(browser_report=None):
    v = {"ran": True, "passed": True, "cmd": "ng build && vitest run"}
    if browser_report is not None:
        v["browser_report"] = browser_report
    return v


def _with_pw_config(tmp_path):
    fe = tmp_path / "frontend"
    fe.mkdir()
    (fe / "playwright.config.ts").write_text("export default {};\n")
    return str(tmp_path)


# ---- config detection ---------------------------------------------------------

def test_has_playwright_config_finds_it_in_a_subdir(tmp_path):
    assert _has_playwright_config(_with_pw_config(tmp_path)) is True


def test_has_playwright_config_false_when_only_in_node_modules(tmp_path):
    nm = tmp_path / "frontend" / "node_modules" / "pw"
    nm.mkdir(parents=True)
    (nm / "playwright.config.ts").write_text("export default {};\n")
    assert _has_playwright_config(str(tmp_path)) is False


def test_has_playwright_config_false_when_absent(tmp_path):
    (tmp_path / "backend").mkdir()
    assert _has_playwright_config(str(tmp_path)) is False


# ---- the gate decision --------------------------------------------------------

def test_ui_change_with_no_browser_run_fails_closed(tmp_path):
    ws = _with_pw_config(tmp_path)
    reason = _browser_gate_failure(_verify(), _FRONTEND_DIFF, ws, mode="flexible")
    assert reason is not None
    assert reason.startswith(tq._BROWSER_GATE_MARKER)
    assert "playwright" in reason.lower()


def test_ui_change_with_passing_browser_run_proceeds(tmp_path):
    ws = _with_pw_config(tmp_path)
    reason = _browser_gate_failure(
        _verify({"expected": 6, "unexpected": 0, "flaky": 0, "skipped": 0}),
        _FRONTEND_DIFF, ws, mode="flexible",
    )
    assert reason is None


def test_ui_change_with_failing_browser_run_fails_closed(tmp_path):
    ws = _with_pw_config(tmp_path)
    reason = _browser_gate_failure(
        _verify({"expected": 5, "unexpected": 1, "flaky": 0, "skipped": 0}),
        _FRONTEND_DIFF, ws, mode="flexible",
    )
    assert reason is not None and reason.startswith(tq._BROWSER_GATE_MARKER)


def test_backend_only_change_proceeds_unchanged(tmp_path):
    ws = _with_pw_config(tmp_path)
    reason = _browser_gate_failure(_verify(), _BACKEND_DIFF, ws, mode="strict")
    assert reason is None  # never triggered → never a false positive on backend work


def test_project_without_browser_suite_not_wedged_in_flexible(tmp_path):
    (tmp_path / "frontend").mkdir()  # no playwright config
    reason = _browser_gate_failure(_verify(), _FRONTEND_DIFF, str(tmp_path), mode="flexible")
    assert reason is None


def test_project_without_browser_suite_blocks_in_strict(tmp_path):
    (tmp_path / "frontend").mkdir()  # no playwright config
    reason = _browser_gate_failure(_verify(), _FRONTEND_DIFF, str(tmp_path), mode="strict")
    assert reason is not None and reason.startswith(tq._BROWSER_GATE_MARKER)


def test_disabled_gate_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(tq, "BROWSER_GATE_ENABLED", False)
    ws = _with_pw_config(tmp_path)
    # Would fail closed if enabled; disabled → None.
    assert _browser_gate_failure(_verify(), _FRONTEND_DIFF, ws, mode="strict") is None
