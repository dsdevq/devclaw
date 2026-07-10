"""Deliver a completed task's change as a reviewable branch + PR.

After a task settles ``done`` (the verify gate passed), the agent's change is
sitting **uncommitted** in the workspace. Delivery turns that into something you
*review* instead of *produce*: a branch, a commit, a push, and — if the remote
is GitHub and ``gh`` is authed — a pull request whose URL is recorded on the task.

Design:
  * **Best-effort + non-fatal.** A delivery failure never un-does a ``done`` task;
    it records what it managed (``branch`` / ``pushed`` / ``pr_url`` / ``error``).
  * **Graceful degradation.** Not a git repo, or no changes, or no remote, or no
    auth → it does as much as it can (often: commit to a local branch) and stops.
  * **Auth** is a GitHub token (``GITHUB_TOKEN`` / ``GH_TOKEN``) or ``gh``'s own
    login — this is *repo push access*, separate from the Claude OAuth pillar
    (which is about cognition billing, not git).
"""

from __future__ import annotations

import asyncio
import os
import re


# conventional-commit type per task kind — so a delivered PR reads `feat: …` /
# `fix: …` instead of a raw goal string dumped into the title.
_KIND_TYPE = {
    "implement_feature": "feat",
    "fix_bug": "fix",
    "review_repository": "chore",
    "onboard": "docs",
}


def _slug(text: str, n: int = 40) -> str:
    """Branch-safe slug, truncated on a word boundary (no mid-word cuts like
    `…-endpoint-that-ret`)."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(s) > n:
        s = s[:n].rsplit("-", 1)[0]  # drop the partial trailing segment
    return s.strip("-") or "change"


def _clean_summary(goal: str) -> str:
    """First line of the goal, stripped of markdown backticks and collapsed
    whitespace — the basis for a human-readable title."""
    first = goal.strip().splitlines()[0] if goal.strip() else "devclaw change"
    return re.sub(r"\s+", " ", first.replace("`", "")).strip() or "devclaw change"


def _truncate_words(s: str, limit: int) -> str:
    """Truncate to `limit` chars on a word boundary, adding an ellipsis if cut."""
    if len(s) <= limit:
        return s
    return s[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


def _pr_title(goal: str, kind: str | None = None, limit: int = 72) -> str:
    """A clean, conventional-commit-style title (e.g. `feat: add a /health
    endpoint`) — not the raw goal truncated mid-word."""
    summary = _clean_summary(goal)
    prefix = _KIND_TYPE.get(kind or "", "")
    if prefix:
        return f"{prefix}: {_truncate_words(summary, limit - len(prefix) - 2)}"
    return _truncate_words(summary, limit)


# A conventional-commit subject: `type(scope)?!: summary`.
_CC = re.compile(r"^([a-z]+)(\([^)]+\))?!?:\s*(.+)$", re.IGNORECASE)


def _cc_type(subject: str, kind: str | None) -> str:
    """The conventional-commit *type* for a branch prefix — taken from the agent's
    own commit subject if it wrote one (`fix(x): …` → `fix`), else mapped from the
    task kind. So the branch matches what the change actually is."""
    m = _CC.match(subject.strip())
    return (m.group(1).lower() if m else "") or _KIND_TYPE.get(kind or "", "chore")


def _cc_description(subject: str) -> str:
    """The subject with any leading `type(scope): ` stripped — the basis for a
    clean branch slug (`feat/add-deal-crud`, not `feat/feat-add-deal-crud`)."""
    m = _CC.match(subject.strip())
    return (m.group(3) if m else subject).strip()


def _looks_conventional(subject: str) -> bool:
    return bool(_CC.match(subject.strip()))


# git diff --stat prints a trailing summary like:
#   " 3 files changed, 30 insertions(+), 5 deletions(-)"
# We only need the trailing summary; per-file lines are ignored.
_STAT_FILES = re.compile(r"(\d+)\s+files?\s+changed")
_STAT_ADDS = re.compile(r"(\d+)\s+insertions?\(\+\)")
_STAT_DELS = re.compile(r"(\d+)\s+deletions?\(-\)")


def _scope_suffix(files_stat: str | None, *, min_files: int = 5, min_lines: int = 500) -> str:
    """Return a trailing ``(spans N files, K lines)`` PR-title suffix when the
    delivered diff is materially wider than a single-focus commit subject can
    convey; empty otherwise.

    Guards against the failure mode where the engineer writes a conventional-
    commit-shaped subject (e.g. ``refactor(frontend): extract shared type
    aliases into types.ts``) that describes ~5% of what actually shipped (an
    App.tsx 1827→181-line restructure across seven feature dirs, per
    closeloop PR #23). The suffix is grounded in the ACTUAL diffstat, so it
    catches both a narrow commit subject AND a future planner-authored title
    that drifts from what the executor built.

    TODO(c7-proper): plan.md §Production-ready criterion C7 prescribes a
    proper `title:` field on Action, threaded planner→delivery. This suffix is
    the diff-grounded fallback; the proper thread-through is a follow-up if
    this proves insufficient (a planner-authored title CAN still drift).
    """
    if not files_stat:
        return ""
    last = files_stat.strip().splitlines()[-1] if files_stat.strip() else ""
    m_files = _STAT_FILES.search(last)
    if not m_files:
        return ""
    files = int(m_files.group(1))
    adds = int(_STAT_ADDS.search(last).group(1)) if _STAT_ADDS.search(last) else 0
    dels = int(_STAT_DELS.search(last).group(1)) if _STAT_DELS.search(last) else 0
    total = adds + dels
    if files < min_files and total < min_lines:
        return ""
    lines_str = f"{total / 1000:.1f}k" if total >= 1000 else str(total)
    return f" (spans {files} files, {lines_str} lines)"


async def _agent_commit_msg(workspace_dir: str, base: str | None) -> tuple[str, str] | None:
    """The (subject, body) the AGENT committed for this change (HEAD of base..HEAD),
    or None if it didn't commit. The engineer writing its own commit is what makes
    the delivered PR describe WHAT CHANGED instead of pasting the task instruction."""
    rng = f"{base}..HEAD" if base else "HEAD~1..HEAD"
    rc, subj = await _run("git", "log", "-1", "--format=%s", rng, cwd=workspace_dir)
    if rc != 0 or not subj.strip():
        return None
    _, body = await _run("git", "log", "-1", "--format=%b", rng, cwd=workspace_dir)
    return subj.strip(), body.strip()


def _pr_body(
    goal: str, task_id: str, verify: dict | None, files_stat: str | None,
    *, changes: str | None = None,
) -> str:
    """A PR body a reviewer can actually use. When the engineer wrote its own commit
    (``changes``), lead with what CHANGED and keep the (long) task instruction as a
    collapsed Ticket; otherwise fall back to the instruction as What. Always carries
    the gate verdict, diffstat, and the honest green-gate-≠-correct caveat."""
    if changes is not None:
        parts = ["## Changes", changes.strip() or "(see commit)", ""]
        parts += ["<details><summary>📋 Ticket (what was asked + why)</summary>", "",
                  goal.strip(), "", "</details>", ""]
    else:
        parts = ["## What", goal.strip(), ""]
    if verify and verify.get("ran"):
        cmd = verify.get("cmd", "")
        if verify.get("passed"):
            code = verify.get("exit_code")
            verdict = f"Gate `{cmd}` passed" + (f" (exit {code})." if code is not None else ".")
        else:
            verdict = f"Gate `{cmd}` did **not** pass — see the task error."
        parts += ["## Verification", verdict, ""]
    if files_stat and files_stat.strip():
        parts += ["## Files changed", "```", files_stat.strip(), "```", ""]
    parts += [
        "---",
        f"🤖 Delivered by devclaw (task `{task_id}`). Verify-gated, but **review "
        "before merging** — a green gate means the tests pass, not that the code "
        "is right.",
    ]
    return "\n".join(parts)


async def _run(prog: str, *args: str, cwd: str) -> tuple[int, str]:
    """Run a command, return (exit_code, combined-output). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            prog, *args, cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"{prog} not runnable: {exc}"
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


def _extract_pr_url(text: str) -> str | None:
    m = re.search(r"https://github\.com/\S+/pull/\d+", text)
    return m.group(0) if m else None


async def _current_branch(workspace_dir: str) -> str | None:
    """The workspace's currently-checked-out branch, or None on detached HEAD /
    non-repo. Used to detect goal-branch mode: prepare_workspace(branch=...)
    puts the workspace on a ``goal/<goal_id>`` branch, and that's how delivery
    knows to PUSH to that branch (and reuse the goal's single PR) rather than
    creating a new task-scoped branch."""
    rc, out = await _run("git", "branch", "--show-current", cwd=workspace_dir)
    if rc != 0:
        return None
    name = out.strip()
    return name or None


async def _find_pr_for_branch(workspace_dir: str, branch: str) -> str | None:
    """The url of an existing open PR with ``--head <branch>``, or None.
    Used when delivering to a goal branch so the second + Nth item just push
    new commits to the same branch (the PR auto-updates) instead of trying
    to ``gh pr create`` over an existing PR (which fails)."""
    rc, out = await _run(
        "gh", "pr", "list", "--head", branch, "--state", "open",
        "--json", "url", "--jq", ".[0].url // empty",
        cwd=workspace_dir,
    )
    if rc != 0:
        return None
    return out.strip() or None


async def _default_base_ref(workspace_dir: str) -> str | None:
    """The remote's default branch as a local ref (e.g. 'origin/main'), or None
    if there's no usable origin tracking ref. Used to tell whether the agent
    committed its change to a branch (HEAD ahead of base)."""
    rc, out = await _run(
        "git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", cwd=workspace_dir
    )
    if rc == 0 and out.strip().startswith("refs/remotes/"):
        return out.strip()[len("refs/remotes/") :]
    for cand in ("origin/main", "origin/master"):
        rc, _ = await _run("git", "rev-parse", "--verify", "--quiet", cand, cwd=workspace_dir)
        if rc == 0:
            return cand
    return None


#: errors that mean "there was nothing to ship" or "shipping stopped by design"
#: (a local-only repo has no remote to push to — the local branch IS the
#: deliverable), as opposed to "shipping was attempted and broke".
_BENIGN_ERRORS = ("no changes to deliver", "no 'origin' remote")


def delivery_failed(result: dict) -> str | None:
    """The failure message when a delivery ATTEMPT broke (branch/commit/push/
    PR-create), else None. Benign no-op outcomes — nothing to ship, local-only
    repo — are not failures: a task carrying one still settles ``done``, just
    without a PR. Everything else means a verified change exists but never
    became the reviewable artifact the caller asked for, and the task must NOT
    settle ``done`` (a done-without-PR row reads as shipped to every poller
    upstream)."""
    err = result.get("error")
    if not err:
        return None
    if any(err.startswith(b) for b in _BENIGN_ERRORS):
        return None
    return err


async def deliver_change(
    *,
    workspace_dir: str,
    task_id: str,
    goal: str,
    kind: str | None = None,
    verify: dict | None = None,
    title: str | None = None,
) -> dict:
    """Commit the workspace's change to a branch and (best-effort) push + open a PR.
    Returns a verdict dict; never raises. ``kind`` shapes the conventional-commit
    title (feat/fix/…); ``verify`` (the gate verdict) goes into the PR body.
    ``title`` is the PLANNER's chosen PR title (see Action.title / plan.md
    §Production-ready C7). When present and non-empty it wins over the
    engineer's own commit subject and the goal-derived heuristic — the diff-
    scope suffix from ``_scope_suffix`` still applies for grounding."""
    result: dict = {"delivered": False, "branch": None, "committed": False,
                    "pushed": False, "pr_url": None, "error": None}

    rc, _ = await _run("git", "rev-parse", "--is-inside-work-tree", cwd=workspace_dir)
    if rc != 0:
        result["error"] = "workspace is not a git repository"
        return result

    rc, status = await _run("git", "status", "--porcelain", cwd=workspace_dir)
    dirty = rc == 0 and bool(status.strip())

    # The agent may have committed its change to its own branch, leaving a CLEAN
    # working tree — that is still a delivery. Detect commits ahead of the
    # remote's default branch and ship them, rather than reporting "no changes".
    base = await _default_base_ref(workspace_dir)
    ahead = 0
    if base:
        rc_a, cnt = await _run(
            "git", "rev-list", "--count", f"{base}..HEAD", cwd=workspace_dir
        )
        if rc_a == 0 and cnt.strip().isdigit():
            ahead = int(cnt.strip())

    if not dirty and ahead == 0:
        result["error"] = "no changes to deliver"
        return result

    # Detect goal-branch mode: prepare_workspace(branch="goal/<id>") put the
    # workspace on the goal branch BEFORE the agent ran. In that case all
    # commits the agent made are already on the goal branch — we push it
    # as-is (no new branch), and the goal's single PR is reused across items.
    # Legacy mode (workspace on the default branch or off-goal) creates a
    # per-task branch the way it always has.
    current = await _current_branch(workspace_dir)
    goal_mode = bool(current and current.startswith("goal/"))

    # Prefer the ENGINEER's own commit for the title / branch / PR body — so the
    # delivery reads as "what changed", not the task instruction. The _COMMIT_CODA
    # asks the agent to commit; when it did (clean tree, ahead of base) we derive
    # from its commit. The dirty-tree path (agent left it uncommitted) is the
    # fallback: devclaw commits with a goal-derived conventional title on a
    # devclaw/* branch (so an auto-committed change is visibly distinct from an
    # engineer-authored one).
    agent_msg = await _agent_commit_msg(workspace_dir, base) if (not dirty and ahead > 0) else None
    planner_title = (title or "").strip() or None
    if planner_title:
        # Planner's explicit title wins over the engineer's commit subject and the
        # goal-derived heuristic. Prefix the kind (`feat:`/`fix:`) if the planner
        # omitted the conventional-commit prefix — the branch derivation below
        # relies on `_cc_type` recovering a type from the subject either way.
        prefixed = planner_title if _looks_conventional(planner_title) else _pr_title(planner_title, kind)
        pr_title_derived = _truncate_words(prefixed, 72)
        derived_branch = f"{_cc_type(pr_title_derived, kind)}/{_slug(_cc_description(pr_title_derived))}"
        changes = (agent_msg[1] or agent_msg[0]) if agent_msg else None
        title_slot = pr_title_derived
    elif agent_msg:
        subject, body = agent_msg
        title_slot = _truncate_words(subject if _looks_conventional(subject) else _pr_title(subject, kind), 72)
        derived_branch = f"{_cc_type(subject, kind)}/{_slug(_cc_description(subject))}"
        changes = body or subject
    else:
        title_slot = _pr_title(goal, kind)
        derived_branch = f"devclaw/{task_id[:8]}-{_slug(goal)}"
        changes = None
    # ``title`` (the function parameter) has now been consumed; ``title_slot`` is
    # the PR-title string the rest of this function uses. Kept as ``title`` in the
    # commit-message path below so we don't churn the message shape.
    title = title_slot

    if goal_mode:
        # Stay on the goal branch — every item commits to it cumulatively.
        branch = current  # type: ignore[assignment]
        result["branch"] = branch
    else:
        branch = derived_branch
        result["branch"] = branch
        # Put the change on its branch. `checkout -b` carries HEAD — including
        # any commits the agent already made — onto the new branch. A feature
        # slug can repeat across tasks, so on collision disambiguate with a
        # short task suffix.
        rc, out = await _run("git", "checkout", "-b", branch, cwd=workspace_dir)
        if rc != 0:
            branch = f"{branch}-{task_id[:6]}"
            rc, out = await _run("git", "checkout", "-b", branch, cwd=workspace_dir)
            if rc != 0:
                result["error"] = f"branch failed: {out}"
                return result
            result["branch"] = branch

    if dirty:
        await _run("git", "add", "-A", cwd=workspace_dir)
        msg = f"{title}\n\nDelivered by devclaw (task {task_id})."
        rc, out = await _run(
            "git", "-c", "user.email=devclaw@local", "-c", "user.name=devclaw",
            "commit", "-m", msg, cwd=workspace_dir,
        )
        if rc != 0:
            result["error"] = f"commit failed: {out}"
            return result
    # else: the agent's own commits are already on this branch (ahead > 0).
    result["committed"] = True

    # Diffstat of the delivered change — for the PR body's "Files changed".
    diff_range = f"{base}..HEAD" if base else "HEAD~1..HEAD"
    _, files_stat = await _run("git", "diff", "--stat", diff_range, cwd=workspace_dir)

    # Push only if there's a remote. (Local-only repos — e.g. clones of a local
    # path — have no GitHub remote; we stop at the local commit, which is still
    # a reviewable artifact.)
    rc, remote = await _run("git", "remote", "get-url", "origin", cwd=workspace_dir)
    if rc != 0 or not remote.strip():
        result["error"] = "no 'origin' remote — left change on a local branch"
        result["delivered"] = True  # a local branch is still a reviewable result
        return result

    rc, out = await _run("git", "push", "-u", "origin", branch, cwd=workspace_dir)
    if rc != 0:
        result["error"] = f"push failed (check repo push auth): {out[-300:]}"
        result["delivered"] = True  # committed locally; push is what failed
        return result
    result["pushed"] = True

    # Open a PR only on a GitHub remote with gh available/authed. In goal-
    # branch mode, second-and-Nth items push to the SAME branch the first
    # item created a PR on — the existing PR auto-updates; reuse its URL
    # rather than calling `gh pr create` over it (which would fail).
    if "github.com" in remote:
        if goal_mode:
            existing = await _find_pr_for_branch(workspace_dir, branch)
            if existing:
                result["pr_url"] = existing
                result["delivered"] = True
                return result
        # Ground the PR title in the actual diff scope — an engineer commit
        # subject often describes ~5% of a wide restructure (see closeloop
        # PR #23). Suffix only fires when files/lines cross a threshold; skip
        # when the title already carries a scope tail so we never double-suffix
        # on updates or when the agent wrote its own scope indicator.
        pr_title = title
        if not pr_title.rstrip().endswith(")"):
            pr_title = pr_title + _scope_suffix(files_stat)
        rc, out = await _run(
            "gh", "pr", "create", "--head", branch,
            "--title", pr_title,
            "--body", _pr_body(goal, task_id, verify, files_stat, changes=changes),
            cwd=workspace_dir,
        )
        url = _extract_pr_url(out)
        if url:
            result["pr_url"] = url
        elif rc != 0:
            result["error"] = f"pushed, but gh pr create failed: {out[-300:]}"

    result["delivered"] = True
    return result
