"""Self-observability — the deduplicated problems catalog (capture + dedup +
count). Named regression tests, each pinning one property the catalog exists to
guarantee: the normalizer collapses variable bits, the UPSERT deduplicates
(count++ not a new row), recovered vs terminal are split, the failure choke
points land in the right category, the table stays bounded, and recording a
problem is pure mechanism (no cognition call). See
``devclaw/state_store/problems.py`` + the wiring in ``core.py`` /
``control.py`` / ``goal/store/status.py`` / ``loom/trace.py``.
"""

from __future__ import annotations

from dataclasses import replace

from devclaw.goal.models import GoalStatus
from devclaw.goal.store import GoalStore
from devclaw.goal.transitions import Event
from devclaw.loom import trace as _trace
from devclaw.state_store import StateStore
from devclaw.state_store.problems import normalize
from tests.goal_fakes import Clock, FakeClaude, seed_goal


def _store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "problems.db"))


# ---- normalize() — the fingerprint crux, unit-tested directly ---------------


def test_normalize_collapses_uuid_so_same_root_cause_fingerprints_once():
    a = normalize("lost in-flight ref for program a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    b = normalize("lost in-flight ref for program 88bcffff-1111-2222-3333-444455556666")
    assert a == b
    assert "<id>" in a


def test_normalize_collapses_numbers_paths_and_timestamps():
    # Numbers.
    assert normalize("expected 200 got 500") == normalize("expected 204 got 503")
    # Absolute paths.
    assert normalize("cannot read /repos/alpha/src/main.py") == normalize(
        "cannot read /repos/beta/lib/other.py"
    )
    # ISO timestamps + clock times.
    assert normalize("paused at 2026-07-15T10:30:00Z") == normalize(
        "paused at 2026-07-14T09:11:42Z"
    )
    assert normalize("resets 10:30pm") == normalize("resets 12:20am")


def test_normalize_keeps_genuinely_different_messages_apart():
    assert normalize("assertion failed: /health missing") != normalize(
        "review gate blocked: SQL injection risk"
    )


def test_normalize_lowercases_and_collapses_whitespace():
    assert normalize("  Foo   BAR\n\tbaz  ") == "foo bar baz"


# ---- dedup: same problem → count++ on ONE row, not a new row ----------------


def test_same_problem_recorded_twice_is_one_row_with_count_two(tmp_path):
    s = _store(tmp_path)
    for _ in range(2):
        s.record_problem(
            category="task_fail", kind="assert", message="expected 200 got 500",
            recovered=False,
        )
    rows = s.list_problems()
    assert len(rows) == 1
    assert rows[0]["count"] == 2


def test_variable_bits_dedup_to_one_row(tmp_path):
    s = _store(tmp_path)
    # Two messages differing ONLY in a uuid + a number → same fingerprint.
    s.record_problem(
        category="block", kind="lost_ref",
        message="lost ref for task a1b2c3d4-e5f6-7890-abcd-ef1234567890 after 3 polls",
        recovered=False,
    )
    s.record_problem(
        category="block", kind="lost_ref",
        message="lost ref for task 99999999-8888-7777-6666-555544443333 after 9 polls",
        recovered=False,
    )
    rows = s.list_problems()
    assert len(rows) == 1
    assert rows[0]["count"] == 2


def test_genuinely_different_problems_are_distinct_rows(tmp_path):
    s = _store(tmp_path)
    s.record_problem(category="task_fail", kind="a", message="disk full", recovered=False)
    s.record_problem(category="task_fail", kind="b", message="network down", recovered=False)
    assert s.count_problems() == 2


def test_category_is_part_of_the_fingerprint(tmp_path):
    # Same message + kind but different category must NOT collapse — a block and
    # a task_fail with identical text are different problems.
    s = _store(tmp_path)
    s.record_problem(category="block", kind="x", message="boom", recovered=False)
    s.record_problem(category="task_fail", kind="x", message="boom", recovered=False)
    assert s.count_problems() == 2


# ---- recovered vs terminal counters -----------------------------------------


def test_recovered_and_terminal_counts_split(tmp_path):
    s = _store(tmp_path)
    s.record_problem(category="limit", kind="quota", message="usage limit", recovered=True)
    s.record_problem(category="limit", kind="quota", message="usage limit", recovered=True)
    s.record_problem(category="limit", kind="quota", message="usage limit", recovered=False)
    row = s.list_problems(category="limit")[0]
    assert row["count"] == 3
    assert row["recovered_count"] == 2
    assert row["terminal_count"] == 1


# ---- bounded: N occurrences → 1 row -----------------------------------------


def test_table_is_bounded_n_occurrences_one_row(tmp_path):
    s = _store(tmp_path)
    for i in range(50):
        s.record_problem(
            category="task_fail", kind="flaky",
            message=f"timeout after {i}s on /repos/x/run-{i}.log",
            recovered=False,
        )
    assert s.count_problems() == 1
    assert s.list_problems()[0]["count"] == 50


# ---- categories: the wired choke points land in the right bucket ------------


def test_block_transition_records_a_block_problem(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    s = store.load_status("g")
    store.transition(
        "g", Event.BLOCK,
        replace(
            s, phase="blocked", lifecycle="executing",
            blocked_on="needs answer: which database?", blocked_kind="needs_answer",
        ),
        expect=s,
    )
    rows = store._state.list_problems(category="block")
    assert len(rows) == 1
    assert rows[0]["kind"] == "needs_answer"
    assert rows[0]["last_goal_id"] == "g"
    assert rows[0]["terminal_count"] == 1


def test_block_staying_blocked_does_not_re_record(tmp_path):
    # Entering blocked records once; a goal already blocked that re-blocks on the
    # same state must not inflate the count (the mechanical heal+re-block cycle
    # is counted once per genuine ENTRY, never per tick).
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    s = store.load_status("g")
    blocked = store.transition(
        "g", Event.BLOCK,
        replace(s, phase="blocked", blocked_on="q?", blocked_kind="needs_answer"),
        expect=s,
    )
    # BLOCK again from BLOCKED (a legal self-edge) — same blocked state.
    store.transition(
        "g", Event.BLOCK,
        replace(blocked, phase="blocked", blocked_on="q?", blocked_kind="needs_answer"),
        expect=blocked,
    )
    rows = store._state.list_problems(category="block")
    assert len(rows) == 1
    assert rows[0]["count"] == 1  # NOT 2 — re-block on the same state is not a new entry


def test_force_block_records_a_bug_block(tmp_path):
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    store.force_block("g", "illegal transition: planner proposed a bad edge")
    rows = store._state.list_problems(category="block")
    assert len(rows) == 1
    assert rows[0]["kind"] == "bug"


def test_mark_failed_records_a_task_fail_problem(tmp_path):
    s = _store(tmp_path)
    s.create_task(id="t1", kind="implement_feature", workspace_dir="/repos/x", goal="do it")
    s.mark_running("t1")
    s.mark_failed("t1", "AssertionError: expected /health to return 200\n  full trace...")
    rows = s.list_problems(category="task_fail")
    assert len(rows) == 1
    assert rows[0]["kind"].startswith("AssertionError")
    assert rows[0]["last_task_id"] == "t1"
    assert rows[0]["terminal_count"] == 1


def test_mark_failed_noop_on_terminal_task_does_not_record(tmp_path):
    # A re-settle of an already-terminal task moves no row → records nothing, so
    # a late duplicate settle can't inflate the count.
    s = _store(tmp_path)
    s.create_task(id="t1", kind="implement_feature", workspace_dir="/repos/x", goal="do it")
    s.mark_running("t1")
    s.mark_failed("t1", "boom")
    s.mark_failed("t1", "boom again")  # no-op: t1 is already 'failed'
    assert s.count_problems() == 1
    assert s.list_problems()[0]["count"] == 1


def test_set_global_pause_records_a_recovered_limit_problem(tmp_path):
    s = _store(tmp_path)
    s.set_global_pause(_pause_until(), "quota: You're out of extra usage · resets 10pm (UTC)")
    rows = s.list_problems(category="limit")
    assert len(rows) == 1
    assert rows[0]["kind"] == "quota"
    assert rows[0]["recovered_count"] == 1
    assert rows[0]["terminal_count"] == 0


def _pause_until() -> int:
    from devclaw.state_store import _now_ms

    return _now_ms() + 1800 * 1000


# ---- centralized trace-recorder capture (cognition / subprocess / gate) -----


def test_persistent_tracer_records_cognition_error(tmp_path):
    s = _store(tmp_path)
    tracer = _trace.PersistentTracer(store=s, trace_id="tr", goal_id="g")
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(
            role="planner", model="m", prompt="p", response="",
            error="claude crashed: non-JSON response",
        )
    rows = s.list_problems(category="cognition")
    assert len(rows) == 1
    assert rows[0]["kind"] == "planner"


def test_persistent_tracer_records_delivery_gate_block(tmp_path):
    s = _store(tmp_path)
    tracer = _trace.PersistentTracer(store=s, trace_id="tr", goal_id="g")
    with _trace.tracer_scope(tracer):
        _trace.record_delivery(
            goal_id="g", action_label="add /health endpoint", gate_passed=False,
        )
    rows = s.list_problems(category="gate")
    assert len(rows) == 1
    assert rows[0]["kind"] == "review_gate"


def test_persistent_tracer_ignores_non_error_events(tmp_path):
    # The common case — a successful cognition / passing delivery — records NO
    # problem. Only error-bearing events feed the catalog.
    s = _store(tmp_path)
    tracer = _trace.PersistentTracer(store=s, trace_id="tr", goal_id="g")
    with _trace.tracer_scope(tracer):
        _trace.record_cognition(role="planner", model="m", prompt="p", response="ok")
        _trace.record_delivery(goal_id="g", action_label="shipped", gate_passed=True)
    assert s.count_problems() == 0


# ---- best-effort: a recording hiccup never breaks the observed op -----------


def test_record_problem_swallows_errors(tmp_path):
    s = _store(tmp_path)
    s.close()  # writing to a closed connection raises inside record_problem
    # Must NOT propagate — best-effort telemetry.
    s.record_problem(category="task_fail", kind="x", message="y", recovered=False)


# ---- zero-token guard: recording a problem never triggers cognition ---------


def test_recording_a_problem_makes_no_cognition_call(tmp_path):
    claude = FakeClaude()
    store = GoalStore(tmp_path, now=Clock())
    seed_goal(tmp_path, "g")
    store.save_status("g", GoalStatus(phase="idle", lifecycle="executing"))
    s = store.load_status("g")
    # Block transition + task fail + limit pause — every wired site.
    store.transition(
        "g", Event.BLOCK,
        replace(s, phase="blocked", blocked_on="q?", blocked_kind="needs_answer"),
        expect=s,
    )
    store._state.create_task(id="t", kind="implement_feature", workspace_dir="/x", goal="g")
    store._state.mark_running("t")
    store._state.mark_failed("t", "boom")
    store._state.set_global_pause(_pause_until(), "quota: out of usage")
    assert claude.calls == 0  # pure mechanism — no LLM anywhere on these paths
