"""Self-issue-filing Stage 1 (FILE + CLOSE) — named regression tests.

Each pins one property the loop exists to guarantee: file only recurring +
terminal problems, one issue per fingerprint (idempotent), reopen on recurrence,
auto-close stale issues (anti-accumulation), cap new-issue noise (naming the
suppressed), and — the zero-token / no-egress guard — do nothing at all when the
self-repo isn't configured. The GitHub side is a fake adapter, so tests never
shell out. See ``devclaw/goal/self_issue.py`` + the wiring in
``goal/service.py`` and the schema in ``state_store/core.py``.
"""

from __future__ import annotations

import asyncio

from devclaw.goal import self_issue as si
from devclaw.state_store import StateStore
from devclaw.state_store.problems import normalize

DAY_MS = 24 * 3600 * 1000


# ---- pure decisions (no DB) -------------------------------------------------

def test_should_file_requires_threshold_cycles_and_terminal():
    base = {"terminal_count": 2, "issue_state": None}
    assert si.should_file(base, 3, threshold=3) is True
    assert si.should_file(base, 2, threshold=3) is False          # too few cycles
    assert si.should_file({**base, "terminal_count": 0}, 5) is False  # self-healed only
    assert si.should_file({**base, "issue_state": "open"}, 5) is False  # already open
    # recurred after a close → files again (reopen path)
    assert si.should_file({**base, "issue_state": "closed"}, 5) is True


def test_labels_map_from_category_with_other_fallback():
    assert si.labels_for({"category": "gate"}) == ["devclaw:self-filed", "class:gate"]
    assert si.labels_for({"category": "bogus"}) == ["devclaw:self-filed", "class:other"]


def test_should_close_stale_only_open_and_quiet():
    now = 100 * DAY_MS
    openp = {"issue_state": "open", "issue_number": 7, "last_seen_ms": now - 5 * DAY_MS}
    assert si.should_close_stale(openp, now, quiet_ms=3 * DAY_MS) is True
    fresh = {**openp, "last_seen_ms": now - 1 * DAY_MS}
    assert si.should_close_stale(fresh, now, quiet_ms=3 * DAY_MS) is False
    closed = {**openp, "issue_state": "closed"}
    assert si.should_close_stale(closed, now, quiet_ms=3 * DAY_MS) is False


# ---- orchestration (real store, fake gh) ------------------------------------

class FakeGh:
    def __init__(self) -> None:
        self.created: list[tuple] = []
        self.reopened: list[int] = []
        self.closed: list[int] = []
        self.labels: list[tuple] = []
        self._next = 100

    async def ensure_label(self, repo, name):
        self.labels.append((repo, name))

    async def create_issue(self, repo, *, title, body, labels):
        self._next += 1
        self.created.append((repo, title, tuple(labels)))
        return self._next

    async def reopen_issue(self, repo, number, *, comment):
        self.reopened.append(number)
        return True

    async def close_issue(self, repo, number, *, comment):
        self.closed.append(number)
        return True


def _store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "self_issue.db"))


def _seed(store, *, category, kind, message, terminal=True, last_seen_ms, prior_cycles=()):
    """Record a problem, pin its last_seen into a known window, and pre-mark it in
    ``prior_cycles`` so the current cycle tips it over the recurrence threshold."""
    store.record_problem(
        category=category, kind=kind, message=message, recovered=(not terminal)
    )
    fp = f"{category}|{kind}|{normalize(message)}"
    store._db.execute(
        "UPDATE problems SET last_seen_ms = ?, first_seen_ms = ? WHERE fingerprint = ?",
        (last_seen_ms, last_seen_ms, fp),
    )
    store._db.commit()
    for ck in prior_cycles:
        store.mark_problem_cycle(fp, ck)
    return fp


def test_files_recurring_terminal_problem_once_then_idempotent(tmp_path):
    store = _store(tmp_path)
    fp = _seed(
        store, category="gate", kind="review_crash", message="boom on task abc",
        last_seen_ms=1500, prior_cycles=("2026-07-01", "2026-07-02"),
    )
    gh = FakeGh()
    kw = dict(cycle_key="2026-07-03", start_ms=1000, end_ms=2000, now_ms=2000,
              repo="lifekit-hq/devclaw", gh=gh)

    res = asyncio.run(si.run_self_issue_filing(store, **kw))
    assert len(gh.created) == 1                        # filed exactly one issue
    assert res.filed == [101]
    row = store.problems_active_in_window(1000, 2000)[0]
    assert row["issue_number"] == 101 and row["issue_state"] == "open"

    # Second identical cycle-close → already open → no duplicate.
    res2 = asyncio.run(si.run_self_issue_filing(store, **{**kw, "cycle_key": "2026-07-04"}))
    assert len(gh.created) == 1                        # still one — idempotent
    assert res2.filed == []


def test_self_healed_only_problem_is_never_filed(tmp_path):
    store = _store(tmp_path)
    _seed(store, category="block", kind="mechanical:prep", message="prep not ready",
          terminal=False, last_seen_ms=1500, prior_cycles=("a", "b"))
    gh = FakeGh()
    res = asyncio.run(si.run_self_issue_filing(
        store, cycle_key="c", start_ms=1000, end_ms=2000, now_ms=2000,
        repo="lifekit-hq/devclaw", gh=gh))
    assert gh.created == [] and res.filed == []


def test_age_out_closes_stale_open_issue(tmp_path):
    store = _store(tmp_path)
    now = 100 * DAY_MS
    fp = _seed(store, category="cognition", kind="planner", message="old failure",
               last_seen_ms=now - 5 * DAY_MS)
    store.set_problem_issue(fp, issue_number=55, issue_state="open")
    gh = FakeGh()
    res = asyncio.run(si.run_self_issue_filing(
        store, cycle_key="d", start_ms=now, end_ms=now, now_ms=now,
        repo="lifekit-hq/devclaw", gh=gh, quiet_ms=3 * DAY_MS))
    assert gh.closed == [55] and res.closed == [55]
    row = store.open_issue_problems()
    assert row == []                                   # no longer open


def test_noise_cap_limits_new_issues_and_names_suppressed(tmp_path):
    store = _store(tmp_path)
    for i in range(5):
        _seed(store, category="task_fail", kind=f"k{i}", message=f"fail number {i}",
              last_seen_ms=1500, prior_cycles=("2026-07-01", "2026-07-02"))
    gh = FakeGh()
    res = asyncio.run(si.run_self_issue_filing(
        store, cycle_key="2026-07-03", start_ms=1000, end_ms=2000, now_ms=2000,
        repo="lifekit-hq/devclaw", gh=gh, per_cycle_cap=3))
    assert len(gh.created) == 3                         # capped
    assert len(res.filed) == 3 and len(res.suppressed) == 2   # rest named, not dropped


def test_no_op_and_no_egress_when_self_repo_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVCLAW_SELF_REPO", raising=False)
    store = _store(tmp_path)
    _seed(store, category="gate", kind="review_crash", message="boom",
          last_seen_ms=1500, prior_cycles=("2026-07-01", "2026-07-02"))
    gh = FakeGh()
    res = asyncio.run(si.run_self_issue_filing(
        store, cycle_key="2026-07-03", start_ms=1000, end_ms=2000, now_ms=2000, gh=gh))
    assert res.filed == [] and gh.created == [] and gh.labels == []
