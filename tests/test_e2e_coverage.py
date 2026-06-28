"""E2E-coverage gate — mechanical, pure, no claude call.

Pins the diff-scanner contract so that a UI change without a corresponding
Playwright spec is blocked, while pure-backend / spec-only / hollow-spec dodges
are all handled correctly. Plus a task_queue integration test mirroring the
test_review_gate.py pattern.
"""

import pytest

from devclaw import task_queue
from devclaw.engine import EngineRequest
from devclaw.quality.e2e_coverage import (
    CoverageReport,
    format_feedback,
    scan_diff,
)
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


# ============================ pure scanner ============================

def _diff(*files: tuple[str, str, bool]) -> str:
    """Build a minimal unified diff. Each (path, body, is_new) tuple becomes
    one file. ``body`` is the content lines (without ``+`` markers — we add
    them); ``is_new`` toggles the ``--- /dev/null`` marker so the substance
    check knows it's a fresh file."""
    chunks: list[str] = []
    for path, body, is_new in files:
        chunks.append(f"diff --git a/{path} b/{path}")
        chunks.append("--- /dev/null" if is_new else f"--- a/{path}")
        chunks.append(f"+++ b/{path}")
        chunks.append("@@ -0,0 +1,1 @@" if is_new else "@@ -1,1 +1,1 @@")
        for line in body.splitlines() or [""]:
            chunks.append(f"+{line}")
    return "\n".join(chunks) + "\n"


def test_backend_only_diff_is_clean():
    r = scan_diff(_diff(
        ("server/api.py", "def handler():\n    return 1", False),
    ))
    assert r.ok
    assert r.summary() == "e2e-coverage: clean"


def test_ui_change_without_spec_blocks():
    r = scan_diff(_diff(
        ("app/dashboard/page.tsx", "export default function Dashboard() { return null }", False),
    ))
    assert not r.ok
    assert r.ui_files == ["app/dashboard/page.tsx"]
    assert r.spec_files == []


def test_ui_change_with_spec_passes():
    r = scan_diff(_diff(
        ("app/dashboard/page.tsx", "export default function Dashboard() { return null }", False),
        ("e2e/dashboard.spec.ts", "test('renders', async ({ page }) => { await page.goto('/'); });", True),
    ))
    assert r.ok
    assert r.ui_files == ["app/dashboard/page.tsx"]
    assert "e2e/dashboard.spec.ts" in r.spec_files


def test_hollow_new_spec_fails_substance():
    r = scan_diff(_diff(
        ("app/dashboard/page.tsx", "export default function Dashboard() { return null }", False),
        ("e2e/dashboard.spec.ts", "// TODO write the tests later", True),
    ))
    assert not r.ok
    assert r.hollow_specs == ["e2e/dashboard.spec.ts"]


def test_modified_spec_does_not_need_substance_recheck():
    """Editing an existing spec is fine — test-integrity guards against
    weakening; we don't double-enforce."""
    r = scan_diff(_diff(
        ("app/dashboard/page.tsx", "// some change", False),
        ("e2e/dashboard.spec.ts", "// added a helper", False),  # not new
    ))
    # is_new is False; substance check skipped; spec is present
    assert r.ok


def test_e2e_dot_suffix_also_counts():
    r = scan_diff(_diff(
        ("app/page.tsx", "// change", False),
        ("e2e/home.e2e.ts", "test('home', async ({ page }) => { await page.goto('/'); });", True),
    ))
    assert r.ok


def test_unit_test_dot_test_does_not_count_as_e2e_spec():
    """A `.test.ts` unit test is NOT a Playwright spec; the gate keeps blocking
    so the agent pushes a real E2E spec, not a button-component unit test."""
    r = scan_diff(_diff(
        ("app/page.tsx", "// change", False),
        ("button.test.ts", "test('button', () => { expect(1).toBe(1); });", True),
    ))
    assert not r.ok
    assert r.ui_files == ["app/page.tsx"]
    assert r.spec_files == []  # .test.ts is intentionally excluded


def test_css_only_change_also_requires_spec():
    r = scan_diff(_diff(
        ("app/dashboard/styles.css", ".foo { color: red }", False),
    ))
    assert not r.ok
    assert r.ui_files == ["app/dashboard/styles.css"]


def test_pure_ts_backend_file_does_not_require_spec():
    """The narrower UI heuristic deliberately excludes bare `.ts` files — an
    API route or util shouldn't drag a Playwright spec into the diff."""
    r = scan_diff(_diff(
        ("server/lib/util.ts", "export const x = 1", True),
    ))
    assert r.ok


def test_format_feedback_lists_ui_files_and_warns_against_dodges():
    r = CoverageReport(
        ui_files=["app/dashboard/page.tsx", "app/settings/page.tsx"],
        spec_files=[],
        hollow_specs=[],
    )
    fb = format_feedback(r)
    assert "E2E coverage gate blocked" in fb
    assert "dashboard" in fb and "settings" in fb
    assert "navigate" in fb and "submit" in fb
    assert "weaken" in fb


def test_format_feedback_explains_hollow_spec():
    r = CoverageReport(
        ui_files=["app/page.tsx"],
        spec_files=["e2e/x.spec.ts"],
        hollow_specs=["e2e/x.spec.ts"],
    )
    fb = format_feedback(r)
    assert "empty spec does not satisfy" in fb
    assert "e2e/x.spec.ts" in fb


def test_summary_clean_when_no_ui():
    r = scan_diff("")
    assert r.summary() == "e2e-coverage: clean"


# ====================== task_queue integration ======================

@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _ok_gate_runner(calls: list):
    """Agent ok + verify_cmd passes. Coverage gate is the only thing that can
    send the task back."""
    async def runner(req: EngineRequest):
        calls.append(req.goal)
        gate = {"ran": True, "cmd": "pytest", "passed": True, "exit_code": 0,
                "timed_out": False, "output": ""}
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": gate}
    return runner


_UI_NO_SPEC = (
    "diff --git a/app/page.tsx b/app/page.tsx\n"
    "--- a/app/page.tsx\n"
    "+++ b/app/page.tsx\n"
    "@@ -1,1 +1,1 @@\n"
    "+code\n"
)
_UI_WITH_SPEC = _UI_NO_SPEC + (
    "diff --git a/e2e/x.spec.ts b/e2e/x.spec.ts\n"
    "--- /dev/null\n"
    "+++ b/e2e/x.spec.ts\n"
    "@@ -0,0 +1,1 @@\n"
    "+test('home', async ({ page }) => { await page.goto('/'); });\n"
)
_BACKEND_ONLY = (
    "diff --git a/server/api.py b/server/api.py\n"
    "--- a/server/api.py\n"
    "+++ b/server/api.py\n"
    "@@ -1,1 +1,1 @@\n"
    "+code\n"
)


@pytest.fixture(autouse=True)
def _enable_coverage_gate(monkeypatch):
    monkeypatch.setattr(task_queue, "E2E_COVERAGE_GATE_ENABLED", True)


async def test_ui_change_without_spec_blocks_and_retries(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)

    # First call returns UI-only diff (blocks). Second returns UI+spec (passes).
    diffs = iter([_UI_NO_SPEC, _UI_WITH_SPEC])

    async def fake_diff(_host_dir):
        return next(diffs)
    monkeypatch.setattr(task_queue, "_git_diff", fake_diff)

    calls: list = []
    q = TaskQueue(store, runner=_ok_gate_runner(calls))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="ship dashboard", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert len(calls) == 2
    assert "E2E coverage gate blocked" in calls[1]
    assert "ship dashboard" in calls[1]


async def test_persistent_no_coverage_escalates(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)

    async def fake_diff(_host_dir):
        return _UI_NO_SPEC
    monkeypatch.setattr(task_queue, "_git_diff", fake_diff)

    calls: list = []
    q = TaskQueue(store, runner=_ok_gate_runner(calls))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert "E2E coverage gate blocked" in t.error


async def test_backend_only_diff_does_not_invoke_coverage_gate(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)

    async def fake_diff(_host_dir):
        return _BACKEND_ONLY
    monkeypatch.setattr(task_queue, "_git_diff", fake_diff)

    calls: list = []
    q = TaskQueue(store, runner=_ok_gate_runner(calls))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert len(calls) == 1


async def test_coverage_skipped_when_disabled(store, monkeypatch):
    monkeypatch.setattr(task_queue, "E2E_COVERAGE_GATE_ENABLED", False)
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)

    async def fake_diff(_host_dir):
        return _UI_NO_SPEC
    monkeypatch.setattr(task_queue, "_git_diff", fake_diff)

    calls: list = []
    q = TaskQueue(store, runner=_ok_gate_runner(calls))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    # gate off → UI-only diff still ships
    assert store.get_task(tid).status == "done"
    assert len(calls) == 1


async def test_coverage_skipped_for_non_code_kind(store, monkeypatch):
    """review_repository is read-only — no diff to gate."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)

    async def fake_diff(_host_dir):
        return _UI_NO_SPEC
    monkeypatch.setattr(task_queue, "_git_diff", fake_diff)

    calls: list = []
    q = TaskQueue(store, runner=_ok_gate_runner(calls))
    tid = q.submit(kind="review_repository", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
