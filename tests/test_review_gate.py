"""Pre-PR adversarial diff-review gate — the layer that READS the code.

Two halves:
  1. the pure module (devclaw/review_gate.py): prompt build, verdict validation +
     verdict/issue reconciliation, feedback formatting, JSON parsing.
  2. the queue integration: a request_changes verdict feeds back through the SAME
     retry loop as a gate failure; approve ships; the gate fails OPEN.

Driven with stub runners + a stub reviewer (no docker, no claude).
"""

import pytest

from devclaw import task_queue
from devclaw import quality as review_gate
from devclaw.engine import EngineRequest
from devclaw.planner import PLANNER_TIMEOUT_MS, PlannerError
from devclaw.quality import (
    build_review_prompt,
    format_feedback,
    validate_review,
)
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


# ============================ pure module ============================

def test_build_prompt_includes_ticket_diff_and_contract():
    p = build_review_prompt(goal="Add X", kind="implement_feature", diff="diff --git ...")
    assert "Add X" in p and "implement_feature" in p and "diff --git" in p
    assert "STRICT JSON" in p and "request_changes" in p


def test_build_prompt_reviews_along_two_axes_with_smell_baseline():
    """The review hunts spec-fidelity and standards as separate axes (one must
    not mask the other), carries the Fowler smell baseline as judgement calls,
    and keeps repo conventions authoritative over the baseline."""
    p = build_review_prompt(goal="Add X", kind="implement_feature", diff="d")
    assert "Spec axis" in p and "Standards axis" in p
    assert "scope creep" in p
    assert "speculative generality" in p and "shotgun surgery" in p
    assert "judgement call" in p
    assert "documented conventions" in p and "override" in p


def test_clip_diff_truncates_oversized(monkeypatch):
    monkeypatch.setattr(review_gate, "_MAX_DIFF_CHARS", 50)
    big = "x" * 200
    out = review_gate._clip_diff(big)
    assert len(out) < 200 and "truncated" in out


def test_validate_request_changes_with_blocking_issue():
    v = validate_review({
        "verdict": "request_changes",
        "summary": "has a dead-code line",
        "issues": [
            {"severity": "major", "location": "main.py:48", "problem": "no-op check", "fix": "remove it"},
        ],
    })
    assert v["verdict"] == "request_changes"
    assert len(v["blocking"]) == 1 and v["blocking"][0]["severity"] == "major"


def test_validate_upgrades_approve_that_lists_a_blocker():
    # the issues are the evidence — an 'approve' that nonetheless names a blocker
    # is reconciled UP to request_changes (the verdict can't contradict its list).
    v = validate_review({
        "verdict": "approve",
        "summary": "looks fine",
        "issues": [{"severity": "blocker", "location": "x", "problem": "broken", "fix": "y"}],
    })
    assert v["verdict"] == "request_changes" and len(v["blocking"]) == 1


def test_validate_downgrades_request_changes_with_only_minor():
    # request_changes with nothing but a nit is reconciled DOWN to approve, so a
    # style note can't trap the task in the retry loop forever.
    v = validate_review({
        "verdict": "request_changes",
        "summary": "tiny nit",
        "issues": [{"severity": "minor", "location": "x", "problem": "naming", "fix": "rename"}],
    })
    assert v["verdict"] == "approve" and v["blocking"] == []


def test_validate_clean_change_approves():
    v = validate_review({"verdict": "approve", "summary": "clean", "issues": []})
    assert v["verdict"] == "approve" and v["issues"] == []


def test_validate_rejects_garbage():
    with pytest.raises(PlannerError):
        validate_review({"verdict": "lgtm"})
    with pytest.raises(PlannerError):
        validate_review("not a dict")


def test_format_feedback_lists_blocking_issues_with_fixes():
    fb = format_feedback({
        "summary": "dead code present",
        "blocking": [
            {"severity": "major", "location": "main.py:48", "problem": "no-op check", "fix": "remove it"},
        ],
    })
    assert "requested changes" in fb and "main.py:48" in fb and "remove it" in fb
    assert "weaken tests" in fb or "re-verify" in fb


async def test_review_diff_parses_model_json():
    async def caller(_prompt):
        return '{"verdict":"approve","summary":"ok","issues":[]}'
    v = await review_gate.review_diff(
        goal="g", kind="implement_feature", diff="d", claude_caller=caller
    )
    assert v["verdict"] == "approve"


async def test_review_diff_raises_on_unparseable():
    async def caller(_prompt):
        return "I think this looks pretty good honestly"
    with pytest.raises(PlannerError):
        await review_gate.review_diff(
            goal="g", kind="implement_feature", diff="d", claude_caller=caller
        )


async def test_review_caller_carries_timeout_above_the_90s_ceiling(monkeypatch):
    """Regression: the review reads a diff up to 60 KB and reasons over the whole
    thing on Sonnet — it was the one large-input cognition role left on the global
    90s ceiling (PLANNER_TIMEOUT_MS), so a big benchmark diff timed out, failed the
    gate closed, burned the retry budget, and escalated to the owner. The review
    role must carry an explicit timeout above that ceiling (like decomposer/grill
    already do) AND actually thread it through to the cognition call — not just
    define a constant nobody passes."""
    assert review_gate.REVIEW_TIMEOUT_MS > PLANNER_TIMEOUT_MS

    seen = {}

    class _RecordingCognition:
        async def __call__(self, prompt, *, role="unknown", model=None, timeout_ms=None):
            seen["timeout_ms"] = timeout_ms
            return "{}"

    import devclaw.cognition as cognition
    monkeypatch.setattr(cognition, "get_cognition", lambda: _RecordingCognition())

    await review_gate.review_caller("review this diff")
    assert seen["timeout_ms"] == review_gate.REVIEW_TIMEOUT_MS


# ========================= queue integration =========================

@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _ok_gate_runner(calls: list):
    """Agent ok + gate passes every time — so the review gate is the only thing
    that can send the task back."""
    async def runner(req: EngineRequest):
        calls.append(req.goal)
        gate = {"ran": True, "cmd": "pytest", "passed": True, "exit_code": 0,
                "timed_out": False, "output": ""}
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": gate}
    return runner


def _reviewer(verdicts: list):
    """Return the next verdict per call. Each entry is 'approve' or a feedback str
    (→ request_changes with one blocking issue carrying that text)."""
    seq = list(verdicts)

    async def reviewer(*, goal, kind, diff):
        v = seq.pop(0)
        if v == "approve":
            return {"verdict": "approve", "summary": "ok", "issues": [], "blocking": []}
        return {
            "verdict": "request_changes", "summary": v,
            "issues": [{"severity": "major", "location": "f.py", "problem": v, "fix": "fix it"}],
            "blocking": [{"severity": "major", "location": "f.py", "problem": v, "fix": "fix it"}],
        }
    return reviewer


@pytest.fixture(autouse=True)
def _enable_gate_and_fake_diff(monkeypatch):
    # Force the gate on, and make the shared git-diff return a non-empty diff so
    # the review path is reached (the test workspaces aren't real repos).
    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", True)

    async def fake_diff(_host_dir, _base=""):
        return "diff --git a/f.py b/f.py\n+code"
    monkeypatch.setattr(task_queue, "_git_diff", fake_diff)


async def test_request_changes_retries_with_feedback_then_ships(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []
    q = TaskQueue(
        store, runner=_ok_gate_runner(calls),
        reviewer=_reviewer(["needs a real edge case test", "approve"]),
    )
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="do X", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert len(calls) == 2  # first review requested changes, retried, second approved
    # the review feedback was fed back into the retry goal
    assert "code review requested changes" in calls[1]
    assert "needs a real edge case test" in calls[1] and "do X" in calls[1]


async def test_persistent_request_changes_escalates(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []
    q = TaskQueue(
        store, runner=_ok_gate_runner(calls),
        reviewer=_reviewer(["dead code", "dead code"]),  # never approves
    )
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert len(calls) == 2  # 1 + 1 retry
    assert "dead code" in t.error and "failed after 2 attempts" in t.error


async def test_approve_ships_first_try(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []
    q = TaskQueue(store, runner=_ok_gate_runner(calls), reviewer=_reviewer(["approve"]))
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert len(calls) == 1  # a clean review doesn't trigger a needless retry


async def test_review_crash_fails_closed(store, monkeypatch):
    """A reviewer CRASH is not an approval (T0.2): the crash text feeds the
    retry loop like a request_changes, and with retries exhausted the task
    FAILS — it must never ship unreviewed on the gate's silence. (The old
    behavior shipped it with one stderr line nobody reads.)"""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []

    async def boom(*, goal, kind, diff):
        raise RuntimeError("reviewer exploded")

    q = TaskQueue(store, runner=_ok_gate_runner(calls), reviewer=boom)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert "review gate crashed" in (t.error or "") and "reviewer exploded" in (t.error or "")
    assert len(calls) == 2  # crash fed back like request_changes → one retry, then escalate


async def test_review_quota_crash_pauses_instead_of_failing(store, monkeypatch):
    """A reviewer crash whose text is a usage-limit means the reviewer is
    UNAVAILABLE, not that the change is bad: failing closed feeds the text to
    the caller's quota guard, which classifies it, requeues the task, and
    pauses dispatch — resume re-runs the task INCLUDING its review. The old
    fail-open shipped the change unreviewed precisely when quota ran out."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)

    async def quota_boom(*, goal, kind, diff):
        raise RuntimeError("Internal error: You're out of extra usage · resets 10pm (UTC)")

    q = TaskQueue(store, runner=_ok_gate_runner([]), reviewer=quota_boom)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "pending"   # requeued, NOT failed
    until, reason = store.global_pause()
    assert until > 0 and "quota" in reason


async def test_review_skipped_when_disabled(store, monkeypatch):
    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", False)
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    called = {"n": 0}

    async def reviewer(*, goal, kind, diff):
        called["n"] += 1
        return {"verdict": "request_changes", "summary": "x", "issues": [], "blocking": [
            {"severity": "major", "location": "a", "problem": "b", "fix": "c"}]}

    q = TaskQueue(store, runner=_ok_gate_runner([]), reviewer=reviewer)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done" and called["n"] == 0


async def test_project_review_gate_override_off_skips_even_when_global_on(store, monkeypatch, tmp_path):
    """A project pinning review_gate=off skips the gate even though the
    devclaw-wide REVIEW_GATE_ENABLED default is on (forced on by the autouse
    fixture) — the per-project override wins."""
    from devclaw.project_registry import ProjectRegistry

    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    reg = ProjectRegistry(str(tmp_path / "devclaw.db"))
    reg.create(id="p", name="P", workspace_dir="/ws", review_gate=False)

    called = {"n": 0}

    async def reviewer(*, goal, kind, diff):
        called["n"] += 1
        return {"verdict": "request_changes", "summary": "x", "issues": [], "blocking": [
            {"severity": "major", "location": "a", "problem": "b", "fix": "c"}]}

    q = TaskQueue(store, runner=_ok_gate_runner([]), reviewer=reviewer)
    q.set_registry(reg)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done" and called["n"] == 0


async def test_project_review_gate_override_on_runs_even_when_global_off(store, monkeypatch, tmp_path):
    """Inverse: global default off, but the project pins review_gate=on — the
    gate runs. Proves the override can turn the gate ON against an off fleet,
    not just off."""
    from devclaw.project_registry import ProjectRegistry

    monkeypatch.setattr(task_queue, "REVIEW_GATE_ENABLED", False)
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    reg = ProjectRegistry(str(tmp_path / "devclaw.db"))
    reg.create(id="p", name="P", workspace_dir="/ws", review_gate=True)

    q = TaskQueue(store, runner=_ok_gate_runner([]), reviewer=_reviewer(["approve"]))
    q.set_registry(reg)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    # gate ran (approve verdict) and the task shipped.
    assert store.get_task(tid).status == "done"


async def test_diff_uses_workspace_path_verbatim_not_host_translation(store, monkeypatch):
    # Regression: the post-gate git diff (shared by the test-integrity guard AND
    # the review gate) runs in THIS process, so it must use the workspace path as
    # we see it — NOT the docker-bind host path. Translating container→host pointed
    # git at a `/srv/...` path that doesn't exist in our mount namespace → empty
    # diff → BOTH guards silently no-op'd in the deployed container. The suite
    # missed it because test workspaces don't start with the container prefix, so
    # the translation was a harmless no-op locally. This pins the contract.
    monkeypatch.setenv("DEVCLAW_CONTAINER_PATH_PREFIX", "/var/lib/devclaw/workspaces")
    monkeypatch.setenv("DEVCLAW_HOST_PATH_PREFIX", "/srv/devclaw/workspaces")
    seen: dict = {}

    async def capture_diff(path, _base=""):
        seen["path"] = path
        return ""  # empty → guards pass; we only assert WHICH path git was given

    monkeypatch.setattr(task_queue, "_git_diff", capture_diff)
    ws = "/var/lib/devclaw/workspaces/abc/due-dates"
    q = TaskQueue(store, runner=_ok_gate_runner([]))
    tid = q.submit(kind="implement_feature", workspace_dir=ws, goal="g", verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    # verbatim container path — NOT "/srv/devclaw/workspaces/abc/due-dates"
    assert seen["path"] == ws


async def test_review_skipped_for_non_code_kind(store, monkeypatch):
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    called = {"n": 0}

    async def reviewer(*, goal, kind, diff):
        called["n"] += 1
        return {"verdict": "approve", "summary": "", "issues": [], "blocking": []}

    # review_repository is read-only — no diff to review.
    q = TaskQueue(store, runner=_ok_gate_runner([]), reviewer=reviewer)
    tid = q.submit(kind="review_repository", workspace_dir="/ws", goal="g", verify_cmd="pytest")
    await q.drain()
    assert called["n"] == 0


# ==================== scaffold tasks (L3, #222) ======================
# A scaffold task (generated boilerplate, e.g. `ng new` / `dotnet new`) skips
# ONLY the adversarial review gate. The verify/build gate + test-integrity scan
# STILL run — so an over-tagged real code task is at worst "unreviewed but must
# still build + pass tests", never "ships broken or untested."


def _failing_gate_runner(calls: list):
    """Agent ok, but the verify/build gate FAILS every time — the structural
    check a scaffold item must still satisfy (does it build?)."""
    async def runner(req: EngineRequest):
        calls.append(req.goal)
        gate = {"ran": True, "cmd": "dotnet build", "passed": False, "exit_code": 1,
                "timed_out": False, "output": "error CS1002: ; expected"}
        return {"status": "ok", "workspaceDir": req.workspace_dir, "verify": gate}
    return runner


async def test_scaffold_task_skips_adversarial_review(store, monkeypatch):
    """A scaffold task with a PASSING gate ships done WITHOUT ever invoking the
    reviewer — the review gate is the only thing scaffold bypasses, and here it
    is bypassed even though the reviewer would have requested changes."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    called = {"n": 0}

    async def reviewer(*, goal, kind, diff):
        called["n"] += 1  # if ever called it would block — proves the skip
        return {"verdict": "request_changes", "summary": "x", "issues": [], "blocking": [
            {"severity": "major", "location": "a", "problem": "b", "fix": "c"}]}

    q = TaskQueue(store, runner=_ok_gate_runner([]), reviewer=reviewer)
    tid = q.submit(
        kind="implement_feature", workspace_dir="/ws",
        goal="Scaffold an xUnit test project", verify_cmd="dotnet build",
        scaffold=True,
    )
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert called["n"] == 0  # reviewer NEVER invoked for a scaffold task
    assert store.get_task(tid).scaffold is True  # flag persisted on the row


async def test_scaffold_task_still_fails_failing_verify_gate(store, monkeypatch):
    """THE SAFETY PROPERTY: scaffold skips review but NOT the verify gate. A
    scaffold task whose build gate fails still fails — the flag never rescues a
    change that doesn't compile, and the reviewer is never reached either."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    called = {"n": 0}

    async def reviewer(*, goal, kind, diff):
        called["n"] += 1
        return {"verdict": "approve", "summary": "", "issues": [], "blocking": []}

    q = TaskQueue(store, runner=_failing_gate_runner([]), reviewer=reviewer)
    tid = q.submit(
        kind="implement_feature", workspace_dir="/ws",
        goal="Scaffold an Angular workspace", verify_cmd="dotnet build",
        scaffold=True,
    )
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert "verify gate failed" in (t.error or "")  # the build gate stopped it
    assert called["n"] == 0  # never even reached the review gate


async def test_scaffold_task_still_runs_test_integrity(store, monkeypatch):
    """A scaffold task does NOT bypass the test-integrity scan either: a diff
    that deletes a test fails the task (retries exhausted → failed), and the
    reviewer is still never invoked."""
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 0)
    called = {"n": 0}

    async def reviewer(*, goal, kind, diff):
        called["n"] += 1
        return {"verdict": "approve", "summary": "", "issues": [], "blocking": []}

    # Override the autouse benign diff with one that removes a test declaration.
    async def gutting_diff(_host_dir, _base=""):
        return (
            "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -1,2 +1,0 @@\n"
            "-def test_it_works():\n"
            "-    assert True\n"
        )
    monkeypatch.setattr(task_queue, "_git_diff", gutting_diff)

    q = TaskQueue(store, runner=_ok_gate_runner([]), reviewer=reviewer)
    tid = q.submit(
        kind="implement_feature", workspace_dir="/ws",
        goal="Scaffold a test project", verify_cmd="pytest", scaffold=True,
    )
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "failed"
    assert "test-integrity" in (t.error or "")  # integrity scan still enforced
    assert called["n"] == 0  # review skipped, but integrity was not
