"""Regression tests for the browser-E2E verification gate's pure verdict layer
(``devclaw/quality/browser_gate.py``).

The gap under test (2026-07-17): a UI change that passes ``ng build && vitest``
+ a static diff review but was never rendered in a browser ships broken. These
assert the verdict folds (browser_report, diff, config_present) into a
fail-closed decision — and, critically, that an exit-0 run which executed NOTHING
is ``never_ran``, not a pass (the existence-vs-execution scar).
"""

from __future__ import annotations

from devclaw.quality.browser_gate import (
    diff_is_library_only,
    BrowserGateResult,
    browser_run_verdict,
    changed_paths,
    config_present_in,
    diff_touches_frontend,
)

# A minimal unified diff touching a UI component.
_FRONTEND_DIFF = """diff --git a/frontend/src/app/select/select.component.ts b/frontend/src/app/select/select.component.ts
index 111..222 100644
--- a/frontend/src/app/select/select.component.ts
+++ b/frontend/src/app/select/select.component.ts
@@ -1,3 +1,3 @@
-old
+new
"""

# A diff touching only backend code — the gate must NOT fire.
_BACKEND_DIFF = """diff --git a/backend/Api/ContactsController.cs b/backend/Api/ContactsController.cs
index 333..444 100644
--- a/backend/Api/ContactsController.cs
+++ b/backend/Api/ContactsController.cs
@@ -1,3 +1,3 @@
-old
+new
"""


def _report(expected=0, unexpected=0, flaky=0, skipped=0) -> dict:
    return {"browser_report": {"expected": expected, "unexpected": unexpected,
                               "flaky": flaky, "skipped": skipped}}


# ---- path / trigger detection -------------------------------------------------

def test_changed_paths_reads_both_git_and_plus_markers():
    paths = changed_paths(_FRONTEND_DIFF)
    assert "frontend/src/app/select/select.component.ts" in paths


def test_frontend_component_change_triggers_gate():
    assert diff_touches_frontend(_FRONTEND_DIFF) is True


def test_angular_json_change_triggers_gate():
    diff = ("diff --git a/frontend/angular.json b/frontend/angular.json\n"
            "--- a/frontend/angular.json\n+++ b/frontend/angular.json\n")
    assert diff_touches_frontend(diff) is True


def test_backend_only_change_does_not_trigger_gate():
    assert diff_touches_frontend(_BACKEND_DIFF) is False


def test_empty_diff_does_not_trigger_gate():
    assert diff_touches_frontend("") is False


# ---- verdict folding ----------------------------------------------------------

def test_backend_change_is_not_triggered():
    v = browser_run_verdict(_report(expected=3), _BACKEND_DIFF, config_present=True)
    assert v.state == "not_triggered"
    assert v.blocks_delivery("strict") is False


def test_frontend_change_with_passing_browser_run_is_satisfied():
    v = browser_run_verdict(_report(expected=5), _FRONTEND_DIFF, config_present=True)
    assert v.state == "ran_passed"
    assert v.blocks_delivery() is False


def test_frontend_change_with_failing_browser_run_blocks_both_modes():
    v = browser_run_verdict(_report(expected=4, unexpected=1), _FRONTEND_DIFF, config_present=True)
    assert v.state == "ran_failed"
    assert v.blocks_delivery("flexible") is True
    assert v.blocks_delivery("strict") is True


def test_report_that_executed_nothing_is_never_ran_not_pass():
    # Exit 0 but every test skipped → 0 executed. THE SCAR: existence != execution.
    v = browser_run_verdict(_report(skipped=9), _FRONTEND_DIFF, config_present=True)
    assert v.state == "never_ran"
    assert v.blocks_delivery("flexible") is True


def test_frontend_change_with_no_report_but_config_present_is_never_ran():
    # A playwright config exists, so a browser run was expected; none happened.
    v = browser_run_verdict({"ran": True, "passed": True}, _FRONTEND_DIFF, config_present=True)
    assert v.state == "never_ran"
    assert v.blocks_delivery("flexible") is True


def test_frontend_change_with_no_config_is_absent_and_mode_dependent():
    v = browser_run_verdict({"ran": True, "passed": True}, _FRONTEND_DIFF, config_present=False)
    assert v.state == "absent"
    assert v.blocks_delivery("flexible") is False  # not wedged by default
    assert v.blocks_delivery("strict") is True     # strict demands a browser suite


def test_none_verify_result_with_config_is_never_ran():
    v = browser_run_verdict(None, _FRONTEND_DIFF, config_present=True)
    assert v.state == "never_ran"


# ---- blocks_delivery matrix ---------------------------------------------------

def test_blocks_delivery_matrix():
    assert BrowserGateResult("ran_passed").blocks_delivery("strict") is False
    assert BrowserGateResult("not_triggered").blocks_delivery("strict") is False
    assert BrowserGateResult("ran_failed").blocks_delivery("flexible") is True
    assert BrowserGateResult("never_ran").blocks_delivery("flexible") is True
    assert BrowserGateResult("absent").blocks_delivery("flexible") is False
    assert BrowserGateResult("absent").blocks_delivery("strict") is True


# ---- config detection ---------------------------------------------------------

def test_config_present_detects_root_playwright_config():
    assert config_present_in(["frontend/playwright.config.ts", "frontend/angular.json"]) is True
    assert config_present_in(["frontend/angular.json", "backend/Program.cs"]) is False


# ---- library-only trigger scoping (the cmn-tab-group wedge, 2026-07-18) -------
# A library-only slice (projects/<lib>/src/lib/…) wires nothing into a running
# app route, so demanding a full-app browser run verdicts `never_ran` in both
# modes and NO retry can ever fix it — the goal wedges on the owner. The
# exemption is trigger-scoping only: it removes the EXPECTATION of a run;
# evidence from a run that actually happened is still processed in full.

_LIBRARY_ONLY_DIFF = """diff --git a/projects/cmn/src/lib/tab-group/tab-group.component.ts b/projects/cmn/src/lib/tab-group/tab-group.component.ts
index 111..222 100644
--- a/projects/cmn/src/lib/tab-group/tab-group.component.ts
+++ b/projects/cmn/src/lib/tab-group/tab-group.component.ts
@@ -1,3 +1,3 @@
-old
+new
diff --git a/projects/cmn/src/lib/tab-group/tab-group.component.html b/projects/cmn/src/lib/tab-group/tab-group.component.html
index 333..444 100644
--- a/projects/cmn/src/lib/tab-group/tab-group.component.html
+++ b/projects/cmn/src/lib/tab-group/tab-group.component.html
@@ -1,3 +1,3 @@
-old
+new
"""

_MIXED_LIBRARY_AND_APP_DIFF = _LIBRARY_ONLY_DIFF + """diff --git a/frontend/src/app/shell/shell.component.ts b/frontend/src/app/shell/shell.component.ts
index 555..666 100644
--- a/frontend/src/app/shell/shell.component.ts
+++ b/frontend/src/app/shell/shell.component.ts
@@ -1,3 +1,3 @@
-old
+new
"""

_LIBRARY_PLUS_ANGULAR_JSON_DIFF = _LIBRARY_ONLY_DIFF + """diff --git a/angular.json b/angular.json
index 777..888 100644
--- a/angular.json
+++ b/angular.json
@@ -1,3 +1,3 @@
-old
+new
"""


def test_browser_gate_library_only_diff_not_triggered():
    """The wedge case: library-only diff + playwright config + no browser run
    used to verdict `never_ran` (blocks both modes, unfixable by retry). It is
    now `not_triggered` — the library's proof is its story+spec."""
    v = browser_run_verdict(
        {"ran": True, "passed": True}, _LIBRARY_ONLY_DIFF, config_present=True
    )
    assert v.state == "not_triggered"
    assert v.blocks_delivery("flexible") is False
    assert v.blocks_delivery("strict") is False
    # Same with no verify dict at all (the None path).
    assert (
        browser_run_verdict(None, _LIBRARY_ONLY_DIFF, config_present=True).state
        == "not_triggered"
    )


def test_browser_gate_mixed_library_and_app_diff_still_fires():
    """One app-surface path in the diff keeps the gate REQUIRED — the exemption
    never leaks onto diffs that touch the running app."""
    v = browser_run_verdict(
        {"ran": True, "passed": True}, _MIXED_LIBRARY_AND_APP_DIFF, config_present=True
    )
    assert v.state == "never_ran"
    assert v.blocks_delivery("flexible") is True


def test_browser_gate_library_plus_angular_json_still_fires():
    """angular.json is app/workspace surface, not library surface — touching it
    alongside library files keeps the gate required."""
    v = browser_run_verdict(
        {"ran": True, "passed": True}, _LIBRARY_PLUS_ANGULAR_JSON_DIFF, config_present=True
    )
    assert v.state == "never_ran"


def test_browser_gate_library_only_zero_executed_report_not_triggered():
    """A report that executed nothing on a library-only diff is the same no-run
    condition — exempt, not the existence-vs-execution scar."""
    v = browser_run_verdict(
        _report(expected=0, skipped=3), _LIBRARY_ONLY_DIFF, config_present=True
    )
    assert v.state == "not_triggered"


def test_browser_gate_library_only_failing_run_still_blocks():
    """Trigger-scoping, NOT verdict-weakening: a browser run that actually
    executed and failed on a library-only diff is evidence and still blocks."""
    v = browser_run_verdict(
        _report(expected=2, unexpected=1), _LIBRARY_ONLY_DIFF, config_present=True
    )
    assert v.state == "ran_failed"
    assert v.blocks_delivery("flexible") is True


def test_diff_is_library_only_requires_frontend_paths():
    """A diff with NO frontend-matching path is not 'library-only' — it is
    simply not frontend; and non-frontend paths (stories, docs) never influence
    the decision."""
    assert diff_is_library_only(_BACKEND_DIFF) is False
    assert diff_is_library_only("") is False
    assert diff_is_library_only(_LIBRARY_ONLY_DIFF) is True
    assert diff_is_library_only(_MIXED_LIBRARY_AND_APP_DIFF) is False
