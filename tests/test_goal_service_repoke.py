"""The heartbeat's immediate-re-tick predicate (``GoalService._loop``).

``advanced`` has re-poked since the lifecycle phases landed; ``conflict``
joined it post-T1/PR4-5: an abandoned tick's pending work (unconsumed
steering, a settled action's detail) must not wait out the full 900s
interval just because the conflicting writer wasn't one that pokes the
loop itself (e.g. a corrections-free ``evaluate_goal``, whose telemetry
write still bumps the status version)."""

from devclaw.goal.service import _should_repoke


def test_repokes_on_advanced():
    assert _should_repoke({"g1": "advanced", "g2": "idle"}) is True


def test_repokes_on_conflict():
    # regression: a CONFLICT outcome must retry immediately — the conflicted
    # tick's work (steering / finished detail) is still pending.
    assert _should_repoke({"g1": "conflict"}) is True


def test_no_repoke_on_quiet_sweep():
    assert _should_repoke({"g1": "idle", "g2": "slept", "g3": "in_flight"}) is False


def test_no_repoke_on_empty_sweep():
    assert _should_repoke({}) is False
