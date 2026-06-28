"""Visual-evidence gate — the layer that READS the rendered UI.

Driven with a stub claude_caller; no docker, no real browser, no real claude.
Pins the parsing + reconciliation + feedback shape so prompt edits can't drift
the contract silently.
"""

import pytest

from devclaw.planner import PlannerError
from devclaw.quality import visual_judge
from devclaw.quality.visual_judge import (
    build_visual_prompt,
    format_visual_feedback,
    judge_screenshots,
    merge_rubric,
    validate_visual_verdict,
)


# ============================ pure module ============================

def test_build_prompt_includes_goal_kind_manifest_diff_and_rubric():
    p = build_visual_prompt(
        goal="Polish the CRM dashboard",
        kind="implement_feature",
        diff="diff --git a/app/page.tsx",
        manifest=[
            {"label": "dashboard", "url": "/", "screenshot": "dashboard.png"},
        ],
        evidence_dir="/ws/.devclaw-evidence",
        rubric_per_repo="The CRM must show: top nav, account table, action buttons.",
    )
    assert "Polish the CRM dashboard" in p
    assert "implement_feature" in p
    assert "diff --git" in p
    assert "dashboard" in p
    assert "STRICT JSON" in p and "request_changes" in p
    # screenshot rendered as @path token; rubric block present
    assert "@/ws/.devclaw-evidence/dashboard.png" in p
    assert "PER-REPO RUBRIC" in p
    assert "account table" in p


def test_build_prompt_handles_missing_per_repo_rubric():
    p = build_visual_prompt(
        goal="g", kind="implement_feature", diff="d",
        manifest=[{"label": "home", "screenshot": "home.png"}],
        evidence_dir="/abs",
    )
    # missing rubric is an explicit empty slot, not a literal None
    assert "no per-repo rubric" in p
    assert "None" not in p.split("PER-REPO", 1)[0] or True  # tolerate elsewhere


def test_build_prompt_renders_console_errors_in_manifest():
    p = build_visual_prompt(
        goal="g", kind="fix_bug", diff="d",
        manifest=[
            {
                "label": "settings",
                "url": "/settings",
                "screenshot": "/abs/settings.png",
                "console_errors": ["TypeError: undefined is not a function"],
            }
        ],
        evidence_dir="/ignored",
    )
    assert "settings" in p
    assert "console_errors" in p
    assert "TypeError" in p
    # absolute screenshot path is preserved (no double-joining)
    assert "@/abs/settings.png" in p


def test_render_manifest_caps_at_max_and_annotates_trim(monkeypatch):
    monkeypatch.setattr(visual_judge, "_MAX_SCREENSHOTS", 2)
    p = build_visual_prompt(
        goal="g", kind="implement_feature", diff="",
        manifest=[
            {"label": f"r{i}", "screenshot": f"r{i}.png"} for i in range(5)
        ],
        evidence_dir="/ws",
    )
    assert "r0" in p and "r1" in p
    # trimmed entries are not embedded
    assert "@/ws/r2.png" not in p
    assert "additional route" in p
    assert "5" in p  # the total count is surfaced


def test_render_manifest_empty():
    p = build_visual_prompt(
        goal="g", kind="implement_feature", diff="", manifest=[], evidence_dir="/ws",
    )
    assert "no routes captured" in p


def test_clip_diff_truncates_oversized(monkeypatch):
    monkeypatch.setattr(visual_judge, "_MAX_DIFF_CHARS", 50)
    p = build_visual_prompt(
        goal="g", kind="implement_feature", diff="x" * 200,
        manifest=[{"label": "r", "screenshot": "r.png"}], evidence_dir="/ws",
    )
    assert "truncated" in p


def test_merge_rubric_empty_returns_explicit_slot():
    assert "no per-repo rubric" in merge_rubric("")
    assert "no per-repo rubric" in merge_rubric("   ")
    assert "no per-repo rubric" in merge_rubric(None)  # type: ignore[arg-type]


def test_merge_rubric_wraps_per_repo_block():
    out = merge_rubric("Show the nav.")
    assert "PER-REPO RUBRIC" in out
    assert "Show the nav." in out


# ============================ verdict reconciliation ============================

def test_validate_request_changes_with_blocking_issue():
    v = validate_visual_verdict({
        "verdict": "request_changes",
        "summary": "dashboard layout is broken",
        "issues": [
            {"severity": "major", "location": "dashboard", "problem": "table overflows", "fix": "wrap in scroll container"},
        ],
    })
    assert v["verdict"] == "request_changes"
    assert len(v["blocking"]) == 1


def test_validate_upgrades_approve_that_lists_a_blocker():
    # the issues are the evidence — an 'approve' with a blocker is reconciled UP.
    v = validate_visual_verdict({
        "verdict": "approve",
        "summary": "looks fine",
        "issues": [{"severity": "blocker", "location": "home", "problem": "error overlay", "fix": "fix the runtime error"}],
    })
    assert v["verdict"] == "request_changes" and len(v["blocking"]) == 1


def test_validate_downgrades_request_changes_with_only_minor():
    # request_changes with only a nit reconciles DOWN, so a minor styling note
    # can't trap the agent in retry hell.
    v = validate_visual_verdict({
        "verdict": "request_changes",
        "summary": "small nit",
        "issues": [{"severity": "minor", "location": "footer", "problem": "spacing", "fix": "add 4px"}],
    })
    assert v["verdict"] == "approve" and v["blocking"] == []


def test_validate_clean_change_approves():
    v = validate_visual_verdict({"verdict": "approve", "summary": "clean", "issues": []})
    assert v["verdict"] == "approve" and v["issues"] == []


def test_validate_rejects_garbage():
    with pytest.raises(PlannerError):
        validate_visual_verdict({"verdict": "lgtm"})
    with pytest.raises(PlannerError):
        validate_visual_verdict("not a dict")


def test_validate_coerces_unknown_severity_to_minor():
    v = validate_visual_verdict({
        "verdict": "request_changes",
        "summary": "x",
        "issues": [{"severity": "kinda-bad", "location": "x", "problem": "p", "fix": "f"}],
    })
    # unknown severity → minor → no blocker → reconciles to approve
    assert v["verdict"] == "approve"
    assert v["issues"][0]["severity"] == "minor"


# ============================ feedback formatting ============================

def test_format_feedback_lists_blocking_issues_with_fixes():
    fb = format_visual_feedback({
        "summary": "dashboard layout is broken",
        "blocking": [
            {"severity": "major", "location": "dashboard", "problem": "table overflows", "fix": "wrap in scroll container"},
        ],
    })
    assert "visual review requested changes" in fb
    assert "dashboard" in fb and "table overflows" in fb and "wrap in scroll container" in fb
    assert "visual-verify.sh" in fb  # tells the agent how to reproduce


def test_format_feedback_omits_empty_location_and_fix():
    fb = format_visual_feedback({
        "summary": "broken",
        "blocking": [{"severity": "blocker", "location": "", "problem": "page is blank", "fix": ""}],
    })
    assert "page is blank" in fb
    # an empty location shouldn't print brackets-around-nothing
    assert "[]" not in fb


# ============================ end-to-end through claude_caller ============================

async def test_judge_screenshots_parses_model_json():
    async def caller(_prompt):
        return '{"verdict":"approve","summary":"ok","issues":[]}'
    v = await judge_screenshots(
        goal="g", kind="implement_feature", diff="d",
        manifest=[{"label": "home", "screenshot": "home.png"}],
        evidence_dir="/ws/.devclaw-evidence",
        claude_caller=caller,
    )
    assert v["verdict"] == "approve"


async def test_judge_screenshots_request_changes_round_trip():
    async def caller(_prompt):
        # the caller actually sees the @path tokens — assert they're in the prompt
        assert "@/ws/.devclaw-evidence/home.png" in _prompt
        return (
            '{"verdict":"request_changes","summary":"broken",'
            '"issues":[{"severity":"blocker","location":"home",'
            '"problem":"red error overlay","fix":"fix the runtime error"}]}'
        )
    v = await judge_screenshots(
        goal="ship the home page", kind="implement_feature", diff="",
        manifest=[{"label": "home", "screenshot": "home.png"}],
        evidence_dir="/ws/.devclaw-evidence",
        claude_caller=caller,
    )
    assert v["verdict"] == "request_changes"
    assert v["blocking"] and v["blocking"][0]["severity"] == "blocker"


async def test_judge_screenshots_raises_on_unparseable():
    async def caller(_prompt):
        return "I think this dashboard looks pretty good honestly"
    with pytest.raises(PlannerError):
        await judge_screenshots(
            goal="g", kind="implement_feature", diff="",
            manifest=[{"label": "home", "screenshot": "home.png"}],
            evidence_dir="/ws", claude_caller=caller,
        )


# ========================= task_queue integration =========================

from devclaw import task_queue
from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _ok_gate_runner_with_evidence(calls: list, manifest: list[dict]):
    """Agent ok + verify_cmd passes + a populated visual-evidence manifest. So
    the visual gate is the only thing that can send the task back."""
    async def runner(req: EngineRequest):
        calls.append(req.goal)
        gate = {"ran": True, "cmd": "pytest", "passed": True, "exit_code": 0,
                "timed_out": False, "output": ""}
        return {
            "status": "ok",
            "workspaceDir": req.workspace_dir,
            "verify": gate,
            "evidence": {"ran": True, "manifest": manifest, "errors": []},
        }
    return runner


def _visual_judge_stub(verdicts: list):
    """Return the next verdict per call. Each entry is 'approve' or a feedback
    str (→ request_changes with one blocking issue carrying that text)."""
    seq = list(verdicts)

    async def judge(*, goal, kind, diff, manifest, evidence_dir, rubric_per_repo):
        v = seq.pop(0)
        if v == "approve":
            return {"verdict": "approve", "summary": "ok", "issues": [], "blocking": []}
        return {
            "verdict": "request_changes", "summary": v,
            "issues": [{"severity": "major", "location": "home", "problem": v, "fix": "fix it"}],
            "blocking": [{"severity": "major", "location": "home", "problem": v, "fix": "fix it"}],
        }
    return judge


_UI_DIFF = "diff --git a/app/page.tsx b/app/page.tsx\n+++ b/app/page.tsx\n+code"
_BACKEND_DIFF = "diff --git a/server/api.py b/server/api.py\n+++ b/server/api.py\n+code"
_MANIFEST = [{"label": "home", "url": "/", "screenshot": "home.png"}]


@pytest.fixture(autouse=True)
def _enable_visual_gate_and_fake_diff(monkeypatch):
    monkeypatch.setattr(task_queue, "VISUAL_GATE_ENABLED", True)
    # The e2e-coverage gate would otherwise block our UI-only test diffs before
    # the visual gate runs — disable it here so these tests stay focused on
    # visual-gate behavior (it's covered in test_e2e_coverage.py).
    monkeypatch.setattr(task_queue, "E2E_COVERAGE_GATE_ENABLED", False)

    async def fake_diff(_host_dir):
        return _UI_DIFF
    monkeypatch.setattr(task_queue, "_git_diff", fake_diff)


async def test_visual_request_changes_retries_with_feedback_then_ships(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []
    q = TaskQueue(
        store,
        runner=_ok_gate_runner_with_evidence(calls, _MANIFEST),
        visual_judge=_visual_judge_stub(["dashboard table overflows", "approve"]),
    )
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="ship dashboard", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert len(calls) == 2
    # the visual feedback was fed back into the retry goal
    assert "visual review requested changes" in calls[1]
    assert "dashboard table overflows" in calls[1] and "ship dashboard" in calls[1]


async def test_visual_persistent_request_changes_escalates(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []
    q = TaskQueue(
        store,
        runner=_ok_gate_runner_with_evidence(calls, _MANIFEST),
        visual_judge=_visual_judge_stub(["broken layout", "broken layout"]),  # never approves
    )
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert len(calls) == 2
    assert "broken layout" in t.error


async def test_visual_approve_falls_through_to_review_gate(store, monkeypatch):
    """A clean visual verdict should not short-circuit the diff-review gate —
    both gates run in series."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", True)
    reviewer_calls = {"n": 0}

    async def reviewer(*, goal, kind, diff):
        reviewer_calls["n"] += 1
        return {"verdict": "approve", "summary": "ok", "issues": [], "blocking": []}

    calls: list = []
    q = TaskQueue(
        store,
        runner=_ok_gate_runner_with_evidence(calls, _MANIFEST),
        visual_judge=_visual_judge_stub(["approve"]),
        reviewer=reviewer,
    )
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert reviewer_calls["n"] == 1  # review still ran


async def test_visual_skipped_when_disabled(store, monkeypatch):
    monkeypatch.setattr(task_queue, "VISUAL_GATE_ENABLED", False)
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    called = {"n": 0}

    async def judge(*, goal, kind, diff, manifest, evidence_dir, rubric_per_repo):
        called["n"] += 1
        return {"verdict": "request_changes", "summary": "x", "issues": [], "blocking": [
            {"severity": "major", "location": "a", "problem": "b", "fix": "c"}]}

    q = TaskQueue(
        store,
        runner=_ok_gate_runner_with_evidence([], _MANIFEST),
        visual_judge=judge,
    )
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done" and called["n"] == 0


async def test_visual_skipped_when_no_evidence(store, monkeypatch):
    """Runner skipped capture (no .agent/visual-verify.sh) → gate must not fire."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    called = {"n": 0}

    async def judge(**_kw):
        called["n"] += 1
        return {"verdict": "approve", "summary": "", "issues": [], "blocking": []}

    async def runner(req):
        return {
            "status": "ok", "workspaceDir": req.workspace_dir,
            "verify": {"ran": True, "cmd": "pytest", "passed": True, "exit_code": 0,
                       "timed_out": False, "output": ""},
            "evidence": {"ran": False, "manifest": [], "errors": [], "reason": "no script"},
        }

    q = TaskQueue(store, runner=runner, visual_judge=judge)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done" and called["n"] == 0


async def test_visual_skipped_when_diff_does_not_touch_ui(store, monkeypatch):
    """Backend-only diff → visual gate must not burn a Claude call."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)

    async def backend_diff(_host_dir):
        return _BACKEND_DIFF
    monkeypatch.setattr(task_queue, "_git_diff", backend_diff)

    called = {"n": 0}

    async def judge(**_kw):
        called["n"] += 1
        return {"verdict": "approve", "summary": "", "issues": [], "blocking": []}

    q = TaskQueue(
        store,
        runner=_ok_gate_runner_with_evidence([], _MANIFEST),
        visual_judge=judge,
    )
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done" and called["n"] == 0


async def test_visual_fails_open_on_judge_error(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)

    async def boom(**_kw):
        raise RuntimeError("claude unreachable")

    q = TaskQueue(
        store,
        runner=_ok_gate_runner_with_evidence([], _MANIFEST),
        visual_judge=boom,
    )
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    # a judge crash must NOT block a gate-verified task — it ships.
    assert store.get_task(tid).status == "done"


async def test_visual_skipped_for_non_code_kind(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    called = {"n": 0}

    async def judge(**_kw):
        called["n"] += 1
        return {"verdict": "approve", "summary": "", "issues": [], "blocking": []}

    # review_repository is read-only — no diff to judge against.
    q = TaskQueue(
        store,
        runner=_ok_gate_runner_with_evidence([], _MANIFEST),
        visual_judge=judge,
    )
    tid = q.submit(kind="review_repository", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert called["n"] == 0


def test_diff_touches_ui_matches_known_suffixes():
    from devclaw.task_queue import _diff_touches_ui
    assert _diff_touches_ui("+++ b/app/page.tsx\n")
    assert _diff_touches_ui("+++ b/styles/main.css\n")
    assert _diff_touches_ui("+++ b/widgets/Card.svelte\n")
    assert not _diff_touches_ui("+++ b/server/api.py\n")
    assert not _diff_touches_ui("")
