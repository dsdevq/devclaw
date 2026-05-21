"""Tests for the per-task run-summary JSONL writer + reader.

Pure-function tests. No Claude, no subprocess, no network.
"""

from __future__ import annotations

import builtins
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from orchestrator.run_summary import (
    DEFAULT_RUNS_PATH,
    RunSummary,
    append_summary,
    build_summary,
    default_runs_path,
    format_tail,
    read_summaries,
    record_run,
)
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    Result,
    TaskKind,
    TaskSpec,
    TaskStatus,
)


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _spec(**overrides) -> TaskSpec:
    base = dict(
        task_id="2026-05-20-run-summary-test-aaaa",
        created_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        created_by="test",
        requester_route=RequesterRoute(channel="test", to="tests"),
        verbatim_intent="exercise the run-summary writer",
        kind=TaskKind.code,
        target_repo="dsdevq/devclaw",
        acceptance_criteria=["x"],
        budget=Budget(max_runtime_seconds=900),
        status=TaskStatus.done,
        dispatched_at=datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 5, 20, 10, 5, 42, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return TaskSpec(**base)


def _result(**overrides) -> Result:
    base = dict(
        task_id="2026-05-20-run-summary-test-aaaa",
        status="done",
        completed_at=datetime(2026, 5, 20, 10, 5, 42, tzinfo=timezone.utc),
        pr_url="https://github.com/dsdevq/devclaw/pull/99",
        branch="kit/2026-05-20-run-summary-test-aaaa",
        files_changed=["foo.py"],
        tests_passed=True,
        notes="ok",
    )
    base.update(overrides)
    return Result(**base)


# ─── default_runs_path ───────────────────────────────────────────────────────


def test_default_runs_path_is_home_state_devclaw():
    path = default_runs_path()
    assert path.name == "runs.jsonl"
    assert path.parent.name == "devclaw"
    assert path.parent.parent.name == "state"
    # Tilde must be expanded so callers can use it as a real Path.
    assert "~" not in str(path)
    assert path == DEFAULT_RUNS_PATH.expanduser()


# ─── Schema: writer produces a valid JSON line per (status, kind) combo ──────


@pytest.mark.parametrize(
    "kind, run_status, has_result, verifier_result",
    [
        (TaskKind.code, "done", True, "passed"),
        (TaskKind.research, "done", True, "passed"),
        (TaskKind.draft, "done", True, "passed"),
        (TaskKind.code, "failed", True, "failed"),
        (TaskKind.research, "failed", True, "skipped"),
        (TaskKind.code, "watchdog_killed", False, None),
        (TaskKind.draft, "watchdog_killed", False, None),
    ],
)
def test_writer_produces_valid_json_line_per_combo(
    tmp_path: Path,
    kind: TaskKind,
    run_status: str,
    has_result: bool,
    verifier_result,
):
    spec = _spec(kind=kind)
    if has_result:
        if run_status == "done":
            res = _result()
        elif verifier_result == "failed":
            res = _result(status="blocked", blocker="verification_failed", pr_url=None)
        else:
            res = _result(status="blocked", blocker="runner_silent_past_deadline", pr_url=None)
    else:
        res = None

    out_path = tmp_path / "runs.jsonl"
    record_run(
        spec=spec,
        result=res,
        status=run_status,
        retries=0,
        path=out_path,
    )

    lines = out_path.read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])

    # All required keys present, no extras missing
    expected_keys = {
        "ts",
        "task_id",
        "kind",
        "target_repo",
        "status",
        "duration_seconds",
        "retries",
        "verifier_result",
        "pr_url",
        "tokens_total",
        "cost_usd",
    }
    assert set(row.keys()) == expected_keys

    assert row["task_id"] == spec.task_id
    assert row["kind"] == kind.value
    assert row["target_repo"] == "dsdevq/devclaw"
    assert row["status"] == run_status
    assert row["verifier_result"] == verifier_result
    assert isinstance(row["duration_seconds"], int)
    assert isinstance(row["retries"], int)
    assert row["tokens_total"] is None
    assert row["cost_usd"] is None
    if has_result and res and res.pr_url:
        assert row["pr_url"] == res.pr_url
    else:
        assert row["pr_url"] is None
    # ts must be ISO8601 — round-trip it.
    parsed_ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
    assert parsed_ts.tzinfo is not None


def test_writer_emits_one_line_per_call_append_only(tmp_path: Path):
    out_path = tmp_path / "runs.jsonl"
    spec = _spec()
    record_run(spec=spec, result=_result(), status="done", path=out_path)
    record_run(spec=spec, result=None, status="watchdog_killed", path=out_path)
    record_run(
        spec=spec,
        result=_result(status="blocked", blocker="verification_failed"),
        status="failed",
        path=out_path,
    )

    lines = out_path.read_text().splitlines()
    assert len(lines) == 3
    statuses = [json.loads(ln)["status"] for ln in lines]
    assert statuses == ["done", "watchdog_killed", "failed"]


# ─── Missing parent dir is created ───────────────────────────────────────────


def test_writer_creates_missing_parent_dir(tmp_path: Path):
    nested = tmp_path / "does" / "not" / "exist" / "yet" / "runs.jsonl"
    assert not nested.parent.exists()
    record_run(spec=_spec(), result=_result(), status="done", path=nested)
    assert nested.parent.is_dir()
    assert nested.is_file()
    assert len(nested.read_text().splitlines()) == 1


# ─── File opened in append-binary mode ───────────────────────────────────────


def test_append_summary_opens_file_in_append_binary_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """O_APPEND in binary mode is what gives POSIX-atomic sub-PIPE_BUF writes,
    so concurrent task completions can never produce a torn line."""
    out_path = tmp_path / "runs.jsonl"
    summary = build_summary(
        spec=_spec(), result=_result(), status="done"
    )

    seen: list[tuple] = []
    real_open = builtins.open

    def spy_open(file, mode="r", *args, **kwargs):
        if str(file) == str(out_path):
            seen.append((str(file), mode))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    append_summary(summary, path=out_path)

    # Exactly one open against the JSONL path, with mode "ab"
    appends = [call for call in seen if call[0] == str(out_path)]
    assert appends, "expected the writer to open the JSONL path"
    assert all(mode == "ab" for _, mode in appends), (
        f"writer must open in append-binary mode for POSIX atomic appends; got {appends}"
    )


def test_concurrent_appends_produce_no_torn_lines(tmp_path: Path):
    """Stress: many threads append simultaneously; every line must be a valid JSON object.

    This is the practical end-of-the-promise check that the open("ab") +
    single-write design from the docstring actually delivers sane on-disk shape.
    """
    out_path = tmp_path / "runs.jsonl"
    N_THREADS = 16
    N_PER_THREAD = 25

    def worker(i: int) -> None:
        for j in range(N_PER_THREAD):
            spec = _spec(task_id=f"task-{i:02d}-{j:03d}")
            record_run(spec=spec, result=_result(), status="done", path=out_path)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = out_path.read_text().splitlines()
    assert len(lines) == N_THREADS * N_PER_THREAD

    # Every line must parse as a JSON object with the full schema.
    expected_keys = {
        "ts",
        "task_id",
        "kind",
        "target_repo",
        "status",
        "duration_seconds",
        "retries",
        "verifier_result",
        "pr_url",
        "tokens_total",
        "cost_usd",
    }
    seen_ids: set[str] = set()
    for raw in lines:
        row = json.loads(raw)  # raises on torn / interleaved write
        assert set(row.keys()) == expected_keys
        seen_ids.add(row["task_id"])
    assert len(seen_ids) == N_THREADS * N_PER_THREAD


# ─── duration_seconds / retries inference ───────────────────────────────────


def test_duration_seconds_computed_from_dispatched_to_completed():
    spec = _spec(
        dispatched_at=datetime(2026, 5, 20, 10, 0, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 5, 20, 10, 7, 30, tzinfo=timezone.utc),
    )
    summary = build_summary(
        spec=spec,
        result=_result(),
        status="done",
        ts=datetime(2026, 5, 20, 10, 7, 30, tzinfo=timezone.utc),
    )
    assert summary.duration_seconds == 7 * 60 + 30


def test_duration_seconds_zero_when_dispatched_at_missing():
    spec = _spec(dispatched_at=None)
    summary = build_summary(spec=spec, result=_result(), status="done")
    assert summary.duration_seconds == 0


def test_retries_passed_through():
    summary = build_summary(spec=_spec(), result=_result(), status="done", retries=2)
    assert summary.retries == 2


def test_tokens_and_cost_default_to_null_when_unknown(tmp_path: Path):
    out_path = tmp_path / "runs.jsonl"
    record_run(spec=_spec(), result=_result(), status="done", path=out_path)
    row = json.loads(out_path.read_text().splitlines()[0])
    assert row["tokens_total"] is None
    assert row["cost_usd"] is None


def test_tokens_and_cost_passed_through_when_known(tmp_path: Path):
    out_path = tmp_path / "runs.jsonl"
    record_run(
        spec=_spec(),
        result=_result(),
        status="done",
        path=out_path,
        tokens_total=12345,
        cost_usd=0.42,
    )
    row = json.loads(out_path.read_text().splitlines()[0])
    assert row["tokens_total"] == 12345
    assert row["cost_usd"] == 0.42


# ─── read_summaries ─────────────────────────────────────────────────────────


def test_read_summaries_returns_empty_when_missing(tmp_path: Path):
    assert read_summaries(path=tmp_path / "missing.jsonl") == []


def test_read_summaries_filters_and_limits(tmp_path: Path):
    out_path = tmp_path / "runs.jsonl"
    record_run(spec=_spec(task_id="a", kind=TaskKind.code), result=_result(), status="done", path=out_path)
    record_run(
        spec=_spec(task_id="b", kind=TaskKind.research),
        result=_result(status="blocked", blocker="x"),
        status="failed",
        path=out_path,
    )
    record_run(
        spec=_spec(task_id="c", kind=TaskKind.code),
        result=None,
        status="watchdog_killed",
        path=out_path,
    )

    all_rows = read_summaries(path=out_path)
    assert [r["task_id"] for r in all_rows] == ["a", "b", "c"]

    code_only = read_summaries(path=out_path, kind="code")
    assert [r["task_id"] for r in code_only] == ["a", "c"]

    failed_only = read_summaries(path=out_path, status="failed")
    assert [r["task_id"] for r in failed_only] == ["b"]

    last_two = read_summaries(path=out_path, limit=2)
    assert [r["task_id"] for r in last_two] == ["b", "c"]


def test_read_summaries_skips_garbage_lines(tmp_path: Path):
    out_path = tmp_path / "runs.jsonl"
    record_run(spec=_spec(), result=_result(), status="done", path=out_path)
    with open(out_path, "ab") as fh:
        fh.write(b"this is not json\n")
        fh.write(b"\n")
    record_run(spec=_spec(task_id="ok"), result=_result(), status="done", path=out_path)

    rows = read_summaries(path=out_path)
    assert len(rows) == 2
    assert rows[1]["task_id"] == "ok"


def test_format_tail_includes_header_and_rows(tmp_path: Path):
    out_path = tmp_path / "runs.jsonl"
    record_run(spec=_spec(), result=_result(), status="done", path=out_path)
    rendered = format_tail(read_summaries(path=out_path))
    lines = rendered.splitlines()
    assert lines[0].split("\t")[0] == "ts"
    assert "task_id" in lines[0]
    assert _spec().task_id in lines[1]


# ─── verifier_result inference ───────────────────────────────────────────────


def test_verifier_result_inferred_done_is_passed():
    s = build_summary(spec=_spec(), result=_result(), status="done")
    assert s.verifier_result == "passed"


def test_verifier_result_inferred_verification_failed():
    s = build_summary(
        spec=_spec(),
        result=_result(status="blocked", blocker="verification_failed"),
        status="failed",
    )
    assert s.verifier_result == "failed"


def test_verifier_result_inferred_skipped_for_other_blockers():
    s = build_summary(
        spec=_spec(),
        result=_result(status="blocked", blocker="claude_cli_exit_1"),
        status="failed",
    )
    assert s.verifier_result == "skipped"


def test_verifier_result_inferred_none_for_watchdog():
    s = build_summary(spec=_spec(), result=None, status="watchdog_killed")
    assert s.verifier_result is None


def test_verifier_result_explicit_override():
    s = build_summary(
        spec=_spec(), result=_result(), status="done", verifier_result=None
    )
    assert s.verifier_result is None


# ─── Resilience: OSError on write must not raise ────────────────────────────


def test_record_run_swallows_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A failing JSONL write must NOT crash a completed task — runs.jsonl is
    advisory observability, not a critical-path artifact."""
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("orchestrator.run_summary.append_summary", boom)
    # Should not raise.
    out = record_run(
        spec=_spec(),
        result=_result(),
        status="done",
        path=tmp_path / "runs.jsonl",
    )
    assert out is not None


# ─── RunSummary dataclass surface ────────────────────────────────────────────


def test_run_summary_is_frozen():
    s = RunSummary(
        ts=datetime(2026, 5, 20, tzinfo=timezone.utc),
        task_id="t",
        kind="code",
        target_repo=None,
        status="done",
        duration_seconds=10,
        retries=0,
        verifier_result="passed",
        pr_url=None,
    )
    with pytest.raises((AttributeError, Exception)):
        s.task_id = "other"  # type: ignore[misc]
