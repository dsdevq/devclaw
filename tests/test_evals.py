"""Eval scoring tests — the deterministic core the live harness relies on."""

from devclaw.evals import aggregate, next_answer, score


def _task(status, milestone=None):
    return {"status": status, "milestone": milestone}


def test_score_milestone_partial_credit():
    # M1 fully done, M2 half done → 1 of 2 milestones complete
    tasks = [
        _task("done", "M1"),
        _task("done", "M1"),
        _task("done", "M2"),
        _task("running", "M2"),
    ]
    card = score(run=1, program={"id": "p", "status": "running"}, tasks=tasks, acceptance_passed=None, wall_ms=1000)
    assert card.milestone_total == 2
    assert card.milestone_done == 1
    assert card.milestone_pct == 50.0
    assert card.tasks_done == 3 and card.tasks_total == 4


def test_score_falls_back_to_task_pct_without_milestones():
    tasks = [_task("done"), _task("done"), _task("failed"), _task("pending")]
    card = score(run=1, program={"id": "p", "status": "running"}, tasks=tasks, acceptance_passed=None, wall_ms=0)
    assert card.milestone_total == 0
    assert card.milestone_pct == 50.0  # 2/4 done
    assert card.tasks_failed == 1


def test_score_done_build_passing_acceptance():
    tasks = [_task("done", "M1"), _task("done", "M2")]
    card = score(run=2, program={"id": "p", "status": "done"}, tasks=tasks, acceptance_passed=True, wall_ms=42000)
    assert card.program_status == "done"
    assert card.acceptance_passed is True
    assert card.milestone_pct == 100.0


def test_score_empty_program():
    card = score(run=1, program={"status": "no-spec"}, tasks=[], acceptance_passed=None, wall_ms=0, stuck=True)
    assert card.milestone_pct == 0.0 and card.tasks_total == 0 and card.stuck is True


def test_aggregate_pass_rate_and_completion():
    cards = [
        score(run=1, program={"status": "done"}, tasks=[_task("done", "M1")], acceptance_passed=True, wall_ms=1000),
        score(run=2, program={"status": "done"}, tasks=[_task("done", "M1")], acceptance_passed=False, wall_ms=3000),
        score(run=3, program={"status": "failed"}, tasks=[_task("failed", "M1")], acceptance_passed=None, wall_ms=2000),
    ]
    agg = aggregate(cards)
    assert agg["runs"] == 3
    assert agg["acceptance_passed"] == 1
    assert agg["acceptance_pass_rate"] == round(1 / 3, 3)
    assert agg["builds_completed"] == 2
    assert agg["avg_wall_ms"] == 2000


def test_aggregate_empty():
    assert aggregate([])["acceptance_pass_rate"] == 0.0


def test_next_answer_scripted_then_default():
    scripted = ["use python", "two subcommands"]
    assert next_answer(scripted, 0) == "use python"
    assert next_answer(scripted, 1) == "two subcommands"
    assert next_answer(scripted, 2) == "Use your recommended answer."
    assert next_answer([], 0) == "Use your recommended answer."
