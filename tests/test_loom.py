"""loom — the reusable orchestration core (the neutral-named extraction seam).
Pins the curated public surface and proves the back-compat shims at the old
``devclaw.*`` import paths still resolve to the moved implementations."""
from __future__ import annotations

import devclaw.loom as loom


def test_public_surface_is_importable():
    # everything promised in __all__ actually resolves
    for name in loom.__all__:
        assert hasattr(loom, name), f"loom.__all__ names {name} but it's missing"


def test_core_symbols_are_usable():
    # a couple of real calls through the loom surface, not just imports
    assert loom.classify_failure("429 too many requests").is_pausing is True
    assert loom.scan_diff("").ok is True


def test_goal_domain_is_not_on_the_loom_surface():
    # The goal-domain re-exports (Goal, GoalStore, parse_duration) were trimmed
    # 2026-07-19: they made importing loom.trace execute this facade and drag
    # goal + state_store behind every consumer, breaking loom's leaf contract
    # (pinned by tests/test_llm_call_leaf.py). They live in devclaw.goal.
    for name in ("Goal", "GoalStore", "parse_duration"):
        assert not hasattr(loom, name), f"loom re-grew the goal re-export {name}"
    from devclaw.goal.store import parse_duration

    assert parse_duration("6h") == 21600


def test_shims_resolve_to_the_same_objects():
    # old paths must keep working AND be the very same objects (not copies)
    from devclaw.limits import classify_failure as shim_classify
    from devclaw.test_integrity import scan_diff as shim_scan
    assert shim_classify is loom.classify_failure
    assert shim_scan is loom.scan_diff


def test_physically_owned_modules_live_under_loom():
    import devclaw.loom.limits as l
    import devclaw.loom.test_integrity as ti
    assert l.classify_failure is loom.classify_failure
    assert ti.scan_diff is loom.scan_diff
