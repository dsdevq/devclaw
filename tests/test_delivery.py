"""PR-delivery tests — a verified change comes back as a reviewable branch/PR.

The push + GitHub PR path needs a real remote + auth (live-validated, like the
sandbox runs); these cover the local, deterministic part: repo detection, the
no-changes / non-repo guards, branch+commit, graceful no-remote degradation, and
the TaskQueue wiring (a done open_pr task triggers delivery; a plain task doesn't).
"""

import os
import subprocess

import pytest

from devclaw import delivery
from devclaw.delivery import (
    _extract_pr_url,
    _pr_body,
    _pr_title,
    _scope_suffix,
    _slug,
    deliver_change,
)
from devclaw.engine import EngineRequest
from devclaw.state_store import StateStore
from devclaw.task_queue import TaskQueue


def _git(path, *args):
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def _init_repo(path) -> None:
    _git(path, "init", "-q")
    _git(path, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-q", "-m", "init")


def _branch(path) -> str:
    return subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path,
                          capture_output=True, text=True).stdout.strip()


# ---- pure helpers ----------------------------------------------------------


def test_slug():
    assert _slug("Add a GET /api/version endpoint!") == "add-a-get-api-version-endpoint"
    assert _slug("") == "change"
    # truncates on a word boundary, never mid-word
    long = _slug("Add a GET api crons id endpoint that returns the single cron")
    assert len(long) <= 40 and not long.endswith("-") and "-ret" not in long


def test_pr_title_is_clean_and_conventional():
    # conventional-commit prefix from kind; backticks stripped; word-boundary cut
    t = _pr_title("Add a `GET /api/crons/{id}` endpoint", kind="implement_feature")
    assert t.startswith("feat: ")
    assert "`" not in t
    assert _pr_title("Harden the reject path", kind="fix_bug").startswith("fix: ")
    # long goals are truncated on a word boundary with an ellipsis, within the cap
    longt = _pr_title("Add " + "word " * 40, kind="implement_feature")
    assert len(longt) <= 72 and longt.endswith("…")
    # no kind → no prefix, still cleaned
    assert _pr_title("just do the thing").startswith("just do the thing")


def test_pr_body_carries_ticket_gate_and_caveat():
    verify = {"ran": True, "cmd": "dotnet test", "passed": True, "exit_code": 0}
    body = _pr_body("Add an endpoint", "abcd1234", verify, " Program.cs | 6 +\n 1 file changed")
    assert "## What" in body and "Add an endpoint" in body
    assert "Gate `dotnet test` passed" in body
    assert "## Files changed" in body and "Program.cs" in body
    assert "review before merging" in body.lower()  # the honest caveat
    # degrades cleanly when there was no gate
    nogate = _pr_body("x", "id", None, None)
    assert "## Verification" not in nogate and "## Files changed" not in nogate


def test_scope_suffix_empty_or_missing():
    # No files_stat → no suffix. Graceful on the None/"" edges.
    assert _scope_suffix(None) == ""
    assert _scope_suffix("") == ""
    # A malformed stat with no `files changed` line — return "" rather than raise.
    assert _scope_suffix("some garbage output") == ""


def test_scope_suffix_narrow_diff_no_fire():
    # Narrow — 2 files, 30 lines — well under both thresholds → no suffix.
    stat = " foo.py | 20 ++++++++++++++++++++\n bar.py | 10 ++++++++++\n 2 files changed, 30 insertions(+), 0 deletions(-)"
    assert _scope_suffix(stat) == ""


def test_scope_suffix_wide_by_files_fires():
    # 6 files with a small line count → fires because file-count crosses.
    stat = (
        " a.py | 5 ++++-\n b.py | 5 ++++-\n c.py | 5 ++++-\n"
        " d.py | 5 ++++-\n e.py | 5 ++++-\n f.py | 5 ++++-\n"
        " 6 files changed, 24 insertions(+), 6 deletions(-)"
    )
    suf = _scope_suffix(stat)
    assert suf.startswith(" (spans 6 files")
    assert "30 lines)" in suf  # 24 + 6 = 30, below 1k threshold → raw int


def test_scope_suffix_wide_by_lines_fires():
    # 2 files but 1800 lines → fires because line-count crosses.
    stat = " App.tsx | 1800 +++++++...\n types.ts | 200 ++++++...\n 2 files changed, 1800 insertions(+), 200 deletions(-)"
    suf = _scope_suffix(stat)
    assert suf.startswith(" (spans 2 files")
    assert "2.0k lines)" in suf


def test_scope_suffix_the_closeloop_pr_23_case():
    # The concrete regression this fix targets: closeloop PR #23 restructure —
    # ~10 feature-dir files, ~1800 net insertions + ~1600 deletions.
    stat = (
        " frontend/src/App.tsx | 1650 -----\n"
        " frontend/src/features/accounts/AccountsView.tsx | 320 +++++\n"
        " frontend/src/features/activities/ActivitiesView.tsx | 280 +++\n"
        " frontend/src/features/auth/LoginView.tsx | 90 +++\n"
        " frontend/src/features/contacts/ContactsView.tsx | 360 +++\n"
        " frontend/src/features/pipeline/PipelineView.tsx | 410 +++\n"
        " frontend/src/features/stats/StatsView.tsx | 120 +++\n"
        " frontend/src/features/today/TodayView.tsx | 130 +++\n"
        " frontend/src/hooks/useAppState.ts | 145 +++\n"
        " frontend/src/types.ts | 30 +++\n"
        " 10 files changed, 1885 insertions(+), 1650 deletions(-)"
    )
    suf = _scope_suffix(stat)
    assert suf.startswith(" (spans 10 files")
    assert "3.5k lines)" in suf  # 1885 + 1650 = 3535, formatted as 3.5k


def test_scope_suffix_tunable_thresholds():
    # Callers can dial the thresholds tighter or looser.
    tight_stat = " foo.py | 4 +++-\n bar.py | 4 +++-\n baz.py | 4 +++-\n 3 files changed, 12 insertions(+), 0 deletions(-)"
    assert _scope_suffix(tight_stat) == ""  # default: no fire
    assert _scope_suffix(tight_stat, min_files=3) != ""  # tightened: fires


def test_extract_pr_url():
    out = "https://github.com/dsdevq/lifekit-dashboard/pull/12\n"
    assert _extract_pr_url(out) == "https://github.com/dsdevq/lifekit-dashboard/pull/12"
    assert _extract_pr_url("nothing here") is None


# ---- deliver_change (real local git, no remote) ----------------------------


async def test_deliver_commits_to_a_branch_and_degrades_without_remote(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _init_repo(repo)
    (tmp_path / "repo" / "new.txt").write_text("the agent's change\n")  # dirty tree

    r = await deliver_change(workspace_dir=repo, task_id="abcd1234ef", goal="add new file")

    assert r["committed"] is True
    assert r["branch"] == "devclaw/abcd1234-add-new-file"
    assert r["pushed"] is False and r["pr_url"] is None
    assert r["delivered"] is True
    assert "no 'origin' remote" in r["error"]
    assert _branch(repo) == "devclaw/abcd1234-add-new-file"  # change is on the branch


async def test_deliver_rejects_non_git_dir(tmp_path):
    r = await deliver_change(workspace_dir=str(tmp_path), task_id="x", goal="g")
    assert r["committed"] is False and "not a git repository" in r["error"]


async def test_deliver_noop_when_clean(tmp_path):
    repo = str(tmp_path / "clean")
    os.makedirs(repo)
    _init_repo(repo)
    r = await deliver_change(workspace_dir=repo, task_id="x", goal="g")
    assert r["committed"] is False and "no changes to deliver" in r["error"]


async def test_deliver_uses_the_engineer_commit_for_branch_not_the_goal(tmp_path):
    # The agent committed its own change with a conventional-commit message. The
    # delivered branch (and title/body) must describe WHAT CHANGED — derived from
    # the agent's commit — NOT the raw task instruction.
    origin = str(tmp_path / "origin.git")
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    repo = str(tmp_path / "repo")
    subprocess.run(["git", "clone", "-q", origin, repo], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    # establish a base on the remote default branch
    (tmp_path / "repo" / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD")
    # simulate the agent: a commit with a real conventional message, clean tree
    (tmp_path / "repo" / "feature.txt").write_text("agent change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat(api): add the widget endpoint")

    # the GOAL is a long instruction; it must NOT shape the branch.
    r = await deliver_change(
        workspace_dir=repo, task_id="abcd1234ef",
        goal="Read PRD.md first, then implement the widget API in one coherent change...",
        kind="implement_feature",
    )

    assert r["committed"] is True and r["pushed"] is True and r["delivered"] is True
    # branch is derived from the engineer's commit subject, not the goal slug
    assert r["branch"] == "feat/add-the-widget-endpoint"
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", origin], capture_output=True, text=True
    ).stdout
    assert "feat/add-the-widget-endpoint" in refs and "devclaw/" not in refs


async def test_deliver_goal_branch_mode_does_not_create_per_task_branch(tmp_path):
    """Pillar 2: when the workspace is on a ``goal/<id>`` branch (because
    prepare_workspace put it there), deliver_change keeps the change ON that
    branch — no per-task devclaw/* branch. The goal branch becomes the
    durable thing every item commits to so the cumulative work stacks into
    ONE PR. Closes the 2026-06-26 PR-fan-out failure where 11 separate PRs
    each rebuilt the foundation in conflicting paths."""
    origin = str(tmp_path / "origin.git")
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    repo = str(tmp_path / "repo")
    subprocess.run(["git", "clone", "-q", origin, repo], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (tmp_path / "repo" / "base.txt").write_text("base\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD")
    # Simulate prepare_workspace having checked out the goal branch.
    _git(repo, "checkout", "-b", "goal/my-goal")
    # Agent makes its commit on the goal branch.
    (tmp_path / "repo" / "feature.txt").write_text("agent change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat(api): add the widget endpoint")

    r = await deliver_change(
        workspace_dir=repo, task_id="abcd1234ef",
        goal="implement the widget", kind="implement_feature",
    )

    assert r["committed"] is True and r["pushed"] is True and r["delivered"] is True
    # CRITICAL: branch stays on the goal branch — no devclaw/* fork.
    assert r["branch"] == "goal/my-goal"
    assert _branch(repo) == "goal/my-goal"
    # The push landed the change on origin/goal/my-goal (the durable target).
    refs = subprocess.run(
        ["git", "ls-remote", "--heads", origin], capture_output=True, text=True,
    ).stdout
    assert "goal/my-goal" in refs and "devclaw/" not in refs and "feat/" not in refs


async def test_deliver_uses_explicit_planner_title_over_engineer_commit(tmp_path):
    """C7 climb: when the planner emits a `title:` on the Action, it wins over
    the engineer's own commit subject and the goal-derived heuristic. Closes
    the failure mode where a mid-work commit subject describes only part of
    what was asked (planner has full intent; commit describes latest step)."""
    origin = str(tmp_path / "origin.git")
    subprocess.run(["git", "init", "--bare", "-q", origin], check=True)
    repo = str(tmp_path / "repo")
    subprocess.run(["git", "clone", "-q", origin, repo], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (tmp_path / "repo" / "base.txt").write_text("base\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "push", "-q", "origin", "HEAD")
    # engineer's commit describes only their last atomic step.
    (tmp_path / "repo" / "feature.txt").write_text("agent change\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "wip: iterate on the parser")

    r = await deliver_change(
        workspace_dir=repo, task_id="abcd1234ef",
        goal="Rewrite the parser to support nested groups...",
        kind="implement_feature",
        title="feat(parser): support nested groups",
    )

    assert r["committed"] is True and r["pushed"] is True and r["delivered"] is True
    # branch is derived from the planner's title, not from `wip: iterate…`.
    assert r["branch"] == "feat/support-nested-groups"


async def test_deliver_explicit_title_prefixes_kind_when_bare(tmp_path):
    """A planner-supplied title without a conventional-commit prefix still gets
    the kind-derived prefix, so the delivered PR reads as feat: / fix: even
    when the planner didn't remember the shape."""
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _init_repo(repo)
    (tmp_path / "repo" / "new.txt").write_text("change\n")

    r = await deliver_change(
        workspace_dir=repo, task_id="abcd1234ef",
        goal="do the thing", kind="implement_feature",
        title="add /health endpoint",
    )

    # local-only (no remote) — but the branch was derived from the title.
    assert r["committed"] is True
    assert r["branch"] == "feat/add-health-endpoint"


async def test_current_branch_helper_returns_branch_or_none(tmp_path):
    from devclaw.delivery import _current_branch

    # Non-repo → None (graceful, not crash).
    assert await _current_branch(str(tmp_path)) is None

    # Real repo with a checked-out branch → returns its name.
    repo = str(tmp_path / "r")
    os.makedirs(repo)
    _init_repo(repo)
    assert (await _current_branch(repo)) in ("main", "master")


def test_cc_helpers_and_changes_body():
    from devclaw.delivery import _cc_type, _cc_description, _looks_conventional
    assert _looks_conventional("feat(api): add x") and not _looks_conventional("add x")
    assert _cc_type("fix(db): y", "implement_feature") == "fix"      # from the subject
    assert _cc_type("just a subject", "fix_bug") == "fix"            # falls back to kind
    assert _cc_description("feat(api): add the widget") == "add the widget"
    # the changes-path body leads with what changed + collapses the ticket
    body = _pr_body("the long ticket instruction", "id", None, None, changes="Added a widget endpoint + tests")
    assert "## Changes" in body and "Added a widget endpoint" in body
    assert "Ticket" in body and "the long ticket instruction" in body
    assert "## What" not in body  # the instruction is the ticket, not the headline


# ---- TaskQueue wiring ------------------------------------------------------


def _writing_runner(filename: str):
    async def runner(req: EngineRequest):
        with open(os.path.join(req.workspace_dir, filename), "w") as f:
            f.write("change\n")
        return {"status": "ok", "workspaceDir": req.workspace_dir,
                "verify": {"ran": True, "cmd": "x", "passed": True,
                           "exit_code": 0, "timed_out": False, "output": ""}}
    return runner


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "t.db"))
    yield s
    s.close()


async def test_open_pr_task_triggers_delivery(store, tmp_path):
    repo = str(tmp_path / "ws")
    os.makedirs(repo)
    _init_repo(repo)
    q = TaskQueue(store, runner=_writing_runner("feature.txt"))
    tid = q.submit(kind="implement_feature", workspace_dir=repo, goal="add feature", deliver=True)
    await q.drain()
    t = store.get_task(tid)
    assert t.status == "done"
    assert _branch(repo).startswith("devclaw/")        # delivery branched the change
    assert t.pr_url is None                              # no remote → local branch, recorded as None


async def test_done_is_not_observable_before_delivery(store, tmp_path, monkeypatch):
    """The pr_url close-out invariant: a deliver task must never be observable as
    'done' before its PR is recorded — else a poller (goalclaw) reads
    done-without-PR and re-dispatches. So delivery runs while the task is still
    'running', and 'done' + pr_url land in the same write."""
    repo = str(tmp_path / "ws3")
    os.makedirs(repo)
    _init_repo(repo)

    seen = {}
    pr = "https://github.com/dsdevq/lifekit-dashboard/pull/99"

    async def fake_deliver(*, workspace_dir, task_id, goal, kind=None, verify=None, title=None):
        # While delivery runs, the task must still be 'running' (not yet 'done').
        seen["status_during_delivery"] = store.get_task(task_id).status
        seen["pr_url_during_delivery"] = store.get_task(task_id).pr_url
        return {"delivered": True, "pr_url": pr, "branch": "devclaw/x", "pushed": True}

    monkeypatch.setattr("devclaw.task_queue.deliver_change", fake_deliver)

    q = TaskQueue(store, runner=_writing_runner("feature.txt"))
    tid = q.submit(kind="implement_feature", workspace_dir=repo, goal="add feature", deliver=True)
    await q.drain()

    assert seen["status_during_delivery"] == "running"   # not 'done' yet
    assert seen["pr_url_during_delivery"] is None         # PR not recorded yet
    t = store.get_task(tid)
    assert t.status == "done" and t.pr_url == pr          # both land together


async def test_plain_task_does_not_deliver(store, tmp_path):
    repo = str(tmp_path / "ws2")
    os.makedirs(repo)
    _init_repo(repo)
    start_branch = _branch(repo)
    q = TaskQueue(store, runner=_writing_runner("x.txt"))
    tid = q.submit(kind="implement_feature", workspace_dir=repo, goal="g")  # deliver defaults False
    await q.drain()
    assert store.get_task(tid).status == "done"
    assert _branch(repo) == start_branch                 # no delivery branch created


# ---- broken delivery must not settle done (T0.1) ----------------------------


def test_delivery_failed_classifies_benign_vs_broken():
    from devclaw.delivery import delivery_failed

    # benign: nothing to ship / local-only repo → not a failure
    assert delivery_failed({"error": None, "pr_url": "x"}) is None
    assert delivery_failed({"error": "no changes to deliver"}) is None
    assert delivery_failed({"error": "no 'origin' remote — left change on a local branch"}) is None
    # broken: the attempt itself failed at a step it tried
    assert delivery_failed({"error": "workspace is not a git repository"})
    assert delivery_failed({"error": "branch failed: fatal: ..."})
    assert delivery_failed({"error": "commit failed: ..."})
    assert delivery_failed({"error": "push failed (check repo push auth): remote rejected"})
    assert delivery_failed({"error": "pushed, but gh pr create failed: auth"})


async def test_broken_delivery_settles_failed_not_done(store, tmp_path, monkeypatch):
    """The false-green closure: a verified change whose push/PR BROKE must
    settle 'failed' with the delivery error — never 'done' with pr_url=None,
    which every poller upstream (the goal layer) reads as shipped."""
    repo = str(tmp_path / "ws4")
    os.makedirs(repo)
    _init_repo(repo)

    async def broken_deliver(*, workspace_dir, task_id, goal, kind=None, verify=None, title=None):
        return {"delivered": True, "branch": "devclaw/x", "committed": True,
                "pushed": False, "pr_url": None,
                "error": "push failed (check repo push auth): remote rejected"}

    monkeypatch.setattr("devclaw.task_queue.deliver_change", broken_deliver)

    q = TaskQueue(store, runner=_writing_runner("feature.txt"))
    tid = q.submit(kind="implement_feature", workspace_dir=repo, goal="add feature", deliver=True)
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "failed"
    assert "delivery failed" in (t.error or "") and "push failed" in (t.error or "")
    assert t.pr_url is None


async def test_delivery_exception_settles_failed_not_done(store, tmp_path, monkeypatch):
    """deliver_change promises never to raise; if it does anyway, the task must
    fail loudly — not settle 'done' with the error swallowed to stderr."""
    repo = str(tmp_path / "ws5")
    os.makedirs(repo)
    _init_repo(repo)

    async def raising_deliver(*, workspace_dir, task_id, goal, kind=None, verify=None, title=None):
        raise RuntimeError("gh exploded")

    monkeypatch.setattr("devclaw.task_queue.deliver_change", raising_deliver)

    q = TaskQueue(store, runner=_writing_runner("feature.txt"))
    tid = q.submit(kind="implement_feature", workspace_dir=repo, goal="add feature", deliver=True)
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "failed"
    assert "gh exploded" in (t.error or "")


async def test_no_changes_delivery_still_settles_done(store, tmp_path):
    """Benign no-PR outcome: the gate passed but the workspace has no changes
    (e.g. the requirement already held). Nothing was shipped because nothing
    existed to ship — that is a 'done' without a PR, not a failure. (The
    no-remote sibling case is covered by test_open_pr_task_triggers_delivery.)"""
    repo = str(tmp_path / "ws6")
    os.makedirs(repo)
    _init_repo(repo)

    async def clean_runner(req: EngineRequest):
        # writes nothing — clean tree at delivery time
        return {"status": "ok", "workspaceDir": req.workspace_dir,
                "verify": {"ran": True, "cmd": "x", "passed": True,
                           "exit_code": 0, "timed_out": False, "output": ""}}

    q = TaskQueue(store, runner=clean_runner)
    tid = q.submit(kind="implement_feature", workspace_dir=repo, goal="g", deliver=True)
    await q.drain()

    t = store.get_task(tid)
    assert t.status == "done" and t.pr_url is None
    # the delivery verdict rides along in the persisted result as evidence
    import json as _json
    assert "no changes to deliver" in _json.loads(t.result_json)["delivery"]["error"]
