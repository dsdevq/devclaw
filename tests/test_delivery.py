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
from devclaw.delivery import _extract_pr_url, _pr_body, _pr_title, _slug, deliver_change
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
