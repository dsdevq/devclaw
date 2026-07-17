"""Regression tests for browser-gate mode resolution (unit 3): a per-project
``browser_gate_mode`` override beats the devclaw-wide default, persists across
a registry reopen, and flips the ``absent`` outcome (a project with no Playwright
suite is wedged under strict, waved through under flexible).
"""

from __future__ import annotations

from devclaw.project_registry import ProjectRegistry
from devclaw.task_queue import _browser_gate_failure

_FRONTEND_DIFF = (
    "diff --git a/frontend/src/app/x.component.ts b/frontend/src/app/x.component.ts\n"
    "--- a/frontend/src/app/x.component.ts\n+++ b/frontend/src/app/x.component.ts\n"
    "@@ -1 +1 @@\n-a\n+b\n"
)


def _reg(tmp_path):
    return ProjectRegistry(str(tmp_path / "devclaw.db"))


def test_browser_gate_mode_persists_and_resolves(tmp_path):
    reg = _reg(tmp_path)
    reg.create(id="fs", name="FS", workspace_dir="/ws/fs", browser_gate_mode="strict")
    # survives a fresh connection (real migration/read path, not an in-memory cache)
    reopened = ProjectRegistry(str(tmp_path / "devclaw.db"))
    assert reopened.get("fs").browser_gate_mode == "strict"
    assert reopened.resolve_override("/ws/fs", "browser_gate_mode", "flexible") == "strict"


def test_override_beats_fleet_default_and_unregistered_falls_back(tmp_path):
    reg = _reg(tmp_path)
    reg.create(id="fs", name="FS", workspace_dir="/ws/fs", browser_gate_mode="strict")
    assert reg.resolve_override("/ws/fs", "browser_gate_mode", "flexible") == "strict"
    # a workspace no project claims → the passed-in fleet default
    assert reg.resolve_override("/ws/other", "browser_gate_mode", "flexible") == "flexible"


def test_update_can_pin_and_clear_the_override(tmp_path):
    reg = _reg(tmp_path)
    reg.create(id="fs", name="FS", workspace_dir="/ws/fs")
    assert reg.get("fs").browser_gate_mode is None  # inherits by default
    reg.update("fs", browser_gate_mode="strict")
    assert reg.resolve_override("/ws/fs", "browser_gate_mode", "flexible") == "strict"
    reg.update("fs", browser_gate_mode=None)  # explicit clear → back to inherit
    assert reg.get("fs").browser_gate_mode is None
    assert reg.resolve_override("/ws/fs", "browser_gate_mode", "flexible") == "flexible"


def test_resolved_mode_flips_absent_outcome(tmp_path):
    """End-to-end: the resolved per-project mode changes whether a frontend
    change in a project with NO playwright suite fails closed."""
    reg = _reg(tmp_path)
    ws = str(tmp_path / "proj")
    (tmp_path / "proj" / "frontend").mkdir(parents=True)  # no playwright.config
    reg.create(id="p", name="P", workspace_dir=ws, browser_gate_mode="strict")

    strict = reg.resolve_override(ws, "browser_gate_mode", "flexible")
    verify = {"ran": True, "passed": True}
    assert _browser_gate_failure(verify, _FRONTEND_DIFF, ws, mode=strict) is not None

    reg.update("p", browser_gate_mode="flexible")
    flexible = reg.resolve_override(ws, "browser_gate_mode", "flexible")
    assert _browser_gate_failure(verify, _FRONTEND_DIFF, ws, mode=flexible) is None
