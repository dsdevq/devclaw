"""Grounded remote-checks — pure verdict-folding + URL parsing.

The subprocess boundary (gh) stays untested here, same as merge.py: the tick
injects a fake checker (see test_goal_tick.py), and these tests pin the pure
logic the real checker feeds — especially the closeloop-bench-2026-07-05
signature: an empty check-runs list while every Actions run is
``startup_failure``.
"""

from __future__ import annotations

from devclaw.goal.remote_checks import RemoteChecksResult, combine_states, parse_owner_repo


# ---- parse_owner_repo -------------------------------------------------------


def test_parse_https_url_with_git_suffix():
    assert parse_owner_repo("https://github.com/dsdevq/closeloop-bench.git") == "dsdevq/closeloop-bench"


def test_parse_https_url_without_suffix_and_trailing_slash():
    assert parse_owner_repo("https://github.com/o/r/") == "o/r"


def test_parse_ssh_url():
    assert parse_owner_repo("git@github.com:o/r.git") == "o/r"


def test_parse_non_github_is_none():
    assert parse_owner_repo("https://gitlab.com/o/r.git") is None
    assert parse_owner_repo("https://example.com/demo.git") is None
    assert parse_owner_repo("") is None


# ---- combine_states ---------------------------------------------------------


def _run(status: str = "completed", conclusion: str = "success") -> dict:
    return {"status": status, "conclusion": conclusion}


def test_benchmark_signature_startup_failures_is_infra_broken():
    # closeloop-bench-2026-07-05: `gh pr checks` said "no checks reported"
    # (empty check-runs) while 32 Actions runs were all startup_failure.
    # 2026-07-09 refinement: that signature means CI *infrastructure* never
    # executed (billing lock on a private repo) — it says nothing about the
    # code, so it only blocks under the strict ci-gate.
    runs = [_run(conclusion="startup_failure") for _ in range(32)]
    r = combine_states(runs, [], workflows_present=True)
    assert r.state == "infra_broken"
    assert r.blocks_done("strict")
    assert not r.blocks_done("flexible")
    assert "startup_failure" in r.detail


def test_startup_failure_mixed_with_real_failure_is_failing():
    # A real test failure alongside startup noise is still a failure — the
    # infra classification only applies when nothing else contradicts done.
    runs = [_run(conclusion="startup_failure"), _run(conclusion="failure")]
    r = combine_states(runs, [], workflows_present=True)
    assert r.state == "failing"
    assert r.blocks_done("flexible")


def test_startup_failure_with_pending_run_is_pending():
    runs = [_run(conclusion="startup_failure"), _run(status="in_progress", conclusion="")]
    r = combine_states(runs, [], workflows_present=True)
    assert r.state == "pending"
    assert r.blocks_done("flexible")


def test_zero_runs_with_workflows_present_is_none_and_blocks_only_strict():
    r = combine_states([], [], workflows_present=True)
    assert r.state == "none"
    assert r.blocks_done("strict")
    assert not r.blocks_done("flexible")
    assert "never ran" in r.detail


def test_zero_runs_without_workflows_does_not_block():
    r = combine_states([], [], workflows_present=False)
    assert r.state == "no_workflows"
    assert not r.blocks_done("strict")


def test_pending_run_blocks_as_pending():
    r = combine_states([_run(), _run(status="in_progress", conclusion="")], [], workflows_present=True)
    assert r.state == "pending"
    assert r.blocks_done("flexible")


def test_all_success_is_passing():
    r = combine_states([_run(), _run()], [_run()], workflows_present=True)
    assert r.state == "passing"
    assert not r.blocks_done("strict")


def test_skipped_and_neutral_do_not_fail_the_verdict():
    r = combine_states(
        [_run(), _run(conclusion="skipped"), _run(conclusion="neutral")],
        [], workflows_present=True,
    )
    assert r.state == "passing"


def test_single_failure_among_successes_is_failing():
    r = combine_states([_run(), _run(conclusion="failure")], [], workflows_present=True)
    assert r.state == "failing"


def test_cancelled_counts_as_failing():
    # a run that never finished proved nothing about THIS commit
    r = combine_states([_run(conclusion="cancelled")], [], workflows_present=True)
    assert r.state == "failing"


def test_both_queries_failed_is_unknown_and_fails_open():
    r = combine_states(None, None, workflows_present=True)
    assert r.state == "unknown"
    assert not r.blocks_done("strict")


def test_one_query_failed_other_empty_still_grounded():
    # Actions query failed but check-runs answered empty (workflows exist):
    # the grounded half is authoritative — CI never ran.
    r = combine_states(None, [], workflows_present=True)
    assert r.state == "none"


def test_result_is_plain_dataclass():
    r = RemoteChecksResult("passing", "ok")
    assert r.state == "passing" and r.detail == "ok" and not r.blocks_done("strict")
