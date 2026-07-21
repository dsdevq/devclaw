"""eval_outcomes projection (ADR 0006, continuous-eval PR1).

Every live task settle materializes ONE row in the eval_outcomes projection —
written by the store itself inside the settle commit (single writer, exactly-
once), with failure_class derived by MECHANICAL string bucketing (never an LLM
call). Basket runs land in the same table via `devclaw evals ingest`,
idempotent on (source, report_ref, ticket). All stubbed — no docker, no claude.
"""

import json

import pytest

from devclaw import cli as devclaw_cli
from devclaw import task_queue
from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore, derive_failure_class
from devclaw.task_queue import TaskQueue


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


def _passed_gate_result() -> str:
    return json.dumps({
        "status": "ok",
        "verify": {"ran": True, "cmd": "pytest", "passed": True,
                   "exit_code": 0, "timed_out": False, "output": ""},
    })


# ---- live settle-hook rows -------------------------------------------------


def test_settle_done_with_passed_gate_writes_one_eval_outcomes_row(store):
    store.create_task(
        id="t1", kind="implement_feature", workspace_dir="/ws", goal="g",
        program_id="p1", parent_goal_id="goal-9",
    )
    store.claim_pending("t1")
    store.mark_done("t1", _passed_gate_result(), pr_url="https://x/pr/1")

    rows = store.list_eval_outcomes()
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "live"
    assert row["task_id"] == "t1"
    assert row["status"] == "done"
    assert row["verify_passed"] == 1
    assert row["pr_url"] == "https://x/pr/1"
    assert row["goal_id"] == "goal-9"
    assert row["program_id"] == "p1"
    assert row["kind"] == "implement_feature"
    assert row["workspace_dir"] == "/ws"
    assert row["failure_class"] is None and row["error"] is None
    assert row["wall_ms"] is not None and row["wall_ms"] >= 0
    assert row["settled_at"] > 0


def test_settle_done_without_gate_leaves_verify_passed_null(store):
    # NULL = no gate produced a verdict — distinct from a passed (1) gate.
    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    store.claim_pending("t1")
    store.mark_done("t1", json.dumps({"status": "ok"}))
    (row,) = store.list_eval_outcomes()
    assert row["status"] == "done" and row["verify_passed"] is None


def test_settle_failed_writes_failure_class_bucketed_from_error(store):
    store.create_task(id="t1", kind="fix_bug", workspace_dir="/ws", goal="g")
    store.claim_pending("t1")
    store.mark_failed(
        "t1",
        "verify gate failed (exit 1): `pytest`\nboom " + "x" * 600
        + " (failed after 3 attempts)",
    )
    (row,) = store.list_eval_outcomes()
    assert row["status"] == "failed"
    assert row["failure_class"] == "verify_failed"
    assert row["verify_passed"] == 0  # the gate ran and rejected
    assert row["attempts"] == 3  # parsed from the retry loop's terminal suffix
    assert len(row["error"]) <= 500  # raw error is truncated, not unbounded


def test_cancelled_task_writes_cancelled_outcome_row(store):
    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    store.claim_pending("t1")
    assert store.mark_task_cancelled("t1") is True
    (row,) = store.list_eval_outcomes()
    assert row["status"] == "cancelled"
    assert row["failure_class"] is None and row["error"] is None


def test_resettling_a_task_does_not_double_write_eval_outcomes(store):
    # Exactly-once: the projection insert only happens when the settle UPDATE
    # actually moved a row — a late second settle (done→done, done→failed,
    # done→cancelled) is a no-op on tasks AND on the projection.
    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    store.claim_pending("t1")
    store.mark_done("t1", _passed_gate_result())
    store.mark_done("t1", _passed_gate_result())
    store.mark_failed("t1", "late failure must not clobber the settled row")
    store.mark_task_cancelled("t1")
    rows = store.list_eval_outcomes()
    assert len(rows) == 1
    assert rows[0]["status"] == "done"


async def test_retry_path_settles_with_a_single_done_outcome_row(store, monkeypatch):
    # A gate-fail → retry → success run settles ONCE — one projection row, not
    # one per attempt (the retry loop only settles on the terminal outcome).
    monkeypatch.setattr(task_queue, "TASK_MAX_RETRIES", 1)
    calls: list = []

    async def flaky(req: EngineRequest):
        calls.append(req.goal)
        passed = len(calls) > 1
        return {"status": "ok", "workspaceDir": req.workspace_dir,
                "verify": {"ran": True, "cmd": "pytest", "passed": passed,
                           "exit_code": 0 if passed else 1, "timed_out": False,
                           "output": "boom"}}

    q = TaskQueue(store, runner=flaky)
    tid = q.submit(kind="implement_feature", workspace_dir="/ws", goal="do X",
                   verify_cmd="pytest")
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert len(calls) == 2
    rows = store.list_eval_outcomes()
    assert len(rows) == 1
    assert rows[0]["task_id"] == tid and rows[0]["status"] == "done"
    assert rows[0]["verify_passed"] == 1


def test_projection_hiccup_never_unsettles_the_task(store, monkeypatch, capsys):
    # Best-effort telemetry: a bug inside the projection logs and drops the
    # row — the settle itself still commits (a wedged settle path would be
    # worse than a lost telemetry row).
    from devclaw.state_store import core as store_core

    def boom(_error):
        raise RuntimeError("projection exploded")

    store.create_task(id="t1", kind="implement_feature", workspace_dir="/ws", goal="g")
    store.claim_pending("t1")
    monkeypatch.setattr(store_core, "derive_failure_class", boom)
    store.mark_failed("t1", "some terminal error")
    assert store.get_task("t1").status == "failed"
    assert store.list_eval_outcomes() == []
    assert "eval_outcomes projection failed task=t1" in capsys.readouterr().err


# ---- mechanical failure-class bucketing -------------------------------------


@pytest.mark.parametrize("error,expected", [
    ("worker reported BLOCKED: repo has no test framework", "blocked:worker"),
    ("review gate crashed (failing closed): PlannerError: claude --print timed "
     "out after 180000ms.", "review_crash"),
    ("code review requested changes before this can ship:\nThe diff smuggles "
     "in an unrelated TargetFramework bump.", "review_rejected"),
    ("browser gate (failing closed): UI change with no passing Playwright run",
     "browser_gate_failed"),
    ("test-integrity: 2 test(s) deleted", "test_integrity"),
    ("verify gate failed (exit 1): `dotnet test`", "verify_failed"),
    ("verify gate timed out: `npm test`", "verify_failed"),
    ("task exceeded the 3600s wall-clock timeout with no terminal result — "
     "sandbox torn down.", "timeout"),
    ("gate passed but delivery failed: push rejected", "delivery_failed"),
    ("claude-sdk: no result line emitted", "no_result_line"),
    ("exceeded 5 usage-limit pauses; last: usage limit reached", "rate_limited"),
    ("Failed to authenticate: OAuth session expired and could not be refreshed",
     "auth"),
    ("something entirely novel went sideways", "engine_error"),
    ("", "engine_error"),
    (None, "engine_error"),
])
def test_failure_class_buckets_real_settle_strings(error, expected):
    assert derive_failure_class(error) == expected


# ---- basket ingest (`devclaw evals ingest`) ---------------------------------


def _write_report(path, *, records):
    path.write_text(json.dumps({
        "image": "devclaw-sandbox", "exec_model": "claude-sonnet-4-6",
        "repo": "https://github.com/x/y.git", "verify_cmd": "pytest",
        "n": len(records), "pass_rate": 0.5, "done": 1, "prs": [],
        "records": records,
    }))


_DONE_REC = {
    "id": "crons-by-id", "kind": "implement_feature", "task_id": "u-1",
    "status": "done", "verify_passed": True, "verify_exit": 0,
    "pr_url": "https://x/pr/10", "error": None, "wall_s": 194.4,
    "workspace": "/m/crons-by-id",
}
_FAILED_REC = {
    "id": "gaps-summary", "kind": "fix_bug", "task_id": "u-2",
    "status": "failed", "verify_passed": None, "verify_exit": None,
    "pr_url": None,
    "error": "code review requested changes before this can ship:\nno tests",
    "wall_s": 88.2, "workspace": "/m/gaps-summary",
}


def test_ingest_report_json_creates_basket_rows(tmp_path, capsys):
    report = tmp_path / "passrate-20260101-000000.json"
    _write_report(report, records=[_DONE_REC, _FAILED_REC])
    db = str(tmp_path / "ingest.db")
    assert devclaw_cli.main(["evals", "ingest", str(report), "--db", db]) == 0
    assert "2 new row(s), 0 already present" in capsys.readouterr().out

    s = StateStore(db)
    try:
        rows = {r["ticket"]: r for r in s.list_eval_outcomes(source="basket")}
    finally:
        s.close()
    assert set(rows) == {"crons-by-id", "gaps-summary"}
    done = rows["crons-by-id"]
    assert done["source"] == "basket"
    assert done["status"] == "done" and done["verify_passed"] == 1
    assert done["pr_url"] == "https://x/pr/10"
    assert done["wall_ms"] == 194400
    assert done["report_ref"] == "passrate-20260101-000000.json"
    failed = rows["gaps-summary"]
    assert failed["status"] == "failed"
    assert failed["failure_class"] == "review_rejected"  # same buckets as live


def test_ingesting_the_same_report_twice_creates_no_duplicates(tmp_path, capsys):
    report = tmp_path / "passrate-20260101-000000.json"
    _write_report(report, records=[_DONE_REC, _FAILED_REC])
    db = str(tmp_path / "ingest.db")
    assert devclaw_cli.main(["evals", "ingest", str(report), "--db", db]) == 0
    assert devclaw_cli.main(["evals", "ingest", str(report), "--db", db]) == 0
    assert "0 new row(s), 2 already present" in capsys.readouterr().out
    s = StateStore(db)
    try:
        assert len(s.list_eval_outcomes(source="basket")) == 2
    finally:
        s.close()


def test_ingest_directory_skips_incompatible_reports_without_crashing(tmp_path, capsys):
    # evals/runs/ mixes passrate reports with the June stub-e2e suite dirs
    # (scenario reports — no per-task outcomes) and unsettled 'pending'
    # records (a paused-queue artifact). The ingest takes what it can and
    # SKIPS the rest with a per-file/per-record reason — never a crash.
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_report(runs / "passrate-20260101-000000.json",
                  records=[_DONE_REC, dict(_DONE_REC, id="p1", status="pending")])
    suite = runs / "suite-2026-06-25T11-21-09"
    suite.mkdir()
    (suite / "report.json").write_text(json.dumps(
        {"started_at": "suite-2026-06-25T11-21-09", "cognition": "stub",
         "scenarios": [{"scenario": "blocked_planner", "passed": True}]}
    ))
    (runs / "broken.json").write_text("{not json at all")

    db = str(tmp_path / "ingest.db")
    assert devclaw_cli.main(["evals", "ingest", str(runs), "--db", db]) == 0
    out = capsys.readouterr().out
    assert "passrate-20260101-000000.json: 1 new row(s)" in out
    assert "status='pending' is not a settled outcome" in out
    assert "suite-2026-06-25T11-21-09/report.json: skipped (stub e2e scenario report" in out
    assert "broken.json: skipped (unreadable or invalid JSON" in out
    s = StateStore(db)
    try:
        rows = s.list_eval_outcomes(source="basket")
    finally:
        s.close()
    assert [r["ticket"] for r in rows] == ["crons-by-id"]
