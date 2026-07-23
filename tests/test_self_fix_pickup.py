"""Self-issue-filing Stage 2 (P2 — FIX pickup) — named regression tests.

Each pins one property the pickup exists to guarantee: pick up ONLY human-
``accepted`` self-filed issues, open exactly one ``one_shot`` self-fix goal per
issue and claim it with ``devclaw:fixing``, honour the concurrency cap, self-heal a
re-pick idempotently (``FileExistsError``), and — the zero-token / no-egress guard —
do nothing at all when the self-repo isn't configured. NO auto-merge is exercised
anywhere: P2 opens a PR a human merges (proposal §5A). The GitHub side and goal
creation are both fakes, so tests never shell out and never touch the goal store.
See ``devclaw/goal/self_issue.py`` (Stage 2 section) + the wiring in
``goal/service.py`` (asserted in ``test_cycle_report.py``).
"""

from __future__ import annotations

import asyncio

from devclaw.goal import self_issue as si


# ---- fakes ------------------------------------------------------------------

class FakeGh:
    """Records the two Stage-2 calls; returns a canned issue list."""

    def __init__(self, issues=None):
        self._issues = issues or []
        self.listed: list = []
        self.marked: list = []

    async def list_issues(self, repo, *, labels, state="open"):
        self.listed.append((repo, tuple(labels), state))
        return list(self._issues)

    async def mark_fixing(self, repo, number, *, label, comment):
        self.marked.append((number, label))
        return True


class SpyCreate:
    """Stand-in for ``GoalService.create_goal`` — captures kwargs; can raise
    ``FileExistsError`` for chosen ids to exercise the idempotent re-claim path."""

    def __init__(self, raise_exists=()):
        self.calls: list = []
        self._raise = set(raise_exists)

    def __call__(self, goal_id, **kw):
        self.calls.append((goal_id, kw))
        if goal_id in self._raise:
            raise FileExistsError(goal_id)
        return {"id": goal_id}


def _issue(number, *, title="a bug", body="", accepted=True, self_filed=True, fixing=False):
    labels = []
    if accepted:
        labels.append({"name": si.ACCEPTED_LABEL})
    if self_filed:
        labels.append({"name": si.SELF_FILED_LABEL})
    if fixing:
        labels.append({"name": si.FIXING_LABEL})
    return {"number": number, "title": title, "body": body, "labels": labels}


# ---- pure selection ---------------------------------------------------------

def test_select_for_pickup_respects_concurrency_and_fixing_label():
    issues = [_issue(1, fixing=True), _issue(2), _issue(3)]
    # one in-flight, concurrency 1 → budget 0 → nothing.
    assert si.select_for_pickup(issues, concurrency=1) == []
    # concurrency 2 → budget 1 → the first fresh issue only.
    assert [i["number"] for i in si.select_for_pickup(issues, concurrency=2)] == [2]
    # concurrency 3 → budget 2 → both fresh, in list order.
    assert [i["number"] for i in si.select_for_pickup(issues, concurrency=3)] == [2, 3]


def test_self_fix_workspace_honours_container_prefix(monkeypatch):
    monkeypatch.setenv("DEVCLAW_CONTAINER_PATH_PREFIX", "/var/lib/devclaw/workspaces")
    assert si.self_fix_workspace("self-fix-issue-9") == "/var/lib/devclaw/workspaces/self-fix-issue-9"
    monkeypatch.delenv("DEVCLAW_CONTAINER_PATH_PREFIX", raising=False)
    assert si.self_fix_workspace("self-fix-issue-9") == "/repos/self-fix-issue-9"


# ---- orchestration (fake gh + fake create_goal) -----------------------------

def test_pickup_spawns_one_shot_goal_and_claims_the_issue():
    gh = FakeGh([_issue(42, title="gate crashes on big diff", body="stack trace here")])
    spy = SpyCreate()
    res = asyncio.run(si.run_self_fix_pickup(spy, repo="dsdevq/devclaw", gh=gh))

    assert len(spy.calls) == 1
    gid, kw = spy.calls[0]
    assert gid == "self-fix-issue-42"
    assert kw["mode"] == "one_shot"            # bounded single-issue fix (ADR 0003 dial)
    assert kw["open_pr"] is True               # opens a PR — a human merges it
    assert kw["repo_url"] == "https://github.com/dsdevq/devclaw.git"  # URL, not slug
    assert kw["workspace_dir"].endswith("/self-fix-issue-42")
    assert "#42" in kw["objective"]
    # claimed on GitHub so concurrency accounting + visibility hold across restarts.
    assert gh.marked == [(42, si.FIXING_LABEL)]
    assert res.picked == [(42, "self-fix-issue-42")]


def test_generated_self_fix_params_pass_goal_admission():
    """The params the pickup synthesises must clear the real admission gate — else a
    self-fix goal would be rejected at create_goal. Pins objective/done_when/workspace
    against a future admission tightening."""
    from devclaw.goal.admission import verify_goal

    issue = _issue(99, title="planner drops repo context", body="")
    adm = verify_goal(
        objective=si.self_fix_objective(issue, "dsdevq/devclaw"),
        workspace_dir=si.self_fix_workspace("self-fix-issue-99"),
        done_when=si.self_fix_done_when(99, "dsdevq/devclaw"),
        repo_url=si.self_repo_url("dsdevq/devclaw"),
        backlog=None, verify_cmd=None, spec="",
    )
    assert adm.admitted


def test_pickup_concurrency_one_blocks_when_one_already_fixing():
    gh = FakeGh([_issue(1, fixing=True), _issue(2)])
    spy = SpyCreate()
    res = asyncio.run(si.run_self_fix_pickup(spy, repo="dsdevq/devclaw", gh=gh, concurrency=1))
    assert spy.calls == []                     # budget full — nothing new spawned
    assert res.picked == []


def test_pickup_reclaims_existing_goal_on_filexists_without_error():
    gh = FakeGh([_issue(7)])
    spy = SpyCreate(raise_exists={"self-fix-issue-7"})
    res = asyncio.run(si.run_self_fix_pickup(spy, repo="dsdevq/devclaw", gh=gh))
    assert len(spy.calls) == 1                  # attempted (idempotent create)
    assert gh.marked == [(7, si.FIXING_LABEL)]  # still claimed — self-heal, not error
    assert res.picked == [(7, "self-fix-issue-7")]


def test_no_op_and_no_egress_when_self_repo_unset(monkeypatch):
    monkeypatch.delenv("DEVCLAW_SELF_REPO", raising=False)
    gh = FakeGh([_issue(1)])
    spy = SpyCreate()
    res = asyncio.run(si.run_self_fix_pickup(spy, gh=gh))  # no repo passed → env gate
    assert spy.calls == [] and gh.listed == [] and res.picked == []


def test_pickup_gh_list_failure_is_swallowed():
    class BoomGh(FakeGh):
        async def list_issues(self, repo, *, labels, state="open"):
            raise RuntimeError("gh exploded")

    gh = BoomGh()
    spy = SpyCreate()
    res = asyncio.run(si.run_self_fix_pickup(spy, repo="dsdevq/devclaw", gh=gh))
    assert spy.calls == [] and res.picked == []  # logged + swallowed, edge intact
