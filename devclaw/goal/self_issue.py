"""Self-issue-filing — Stage 1: FILE + CLOSE (the self-improving cycle's safe half).

Proposal: ``docs/proposals/self-issue-filing.md`` (Stage 1 LOCKED 2026-07-22).

At run-cycle close — the same mechanical, ZERO-LLM edge that assembles the cycle
report — devclaw turns its OWN recurring failures into GitHub issues on its own
repo, and retires them so they don't accumulate:

- **FILE** — a problem that has survived across ``>= N`` distinct run-cycles and
  has produced at least one *terminal* failure (not a self-healed block) gets a
  labelled issue on the devclaw repo. One issue per problem fingerprint
  (idempotent); a problem that recurs after its issue was closed reopens it.
- **CLOSE (anti-accumulation)** — an open self-filed issue whose problem has gone
  quiet (not seen for ``K`` cycle-spans) is auto-closed as stale. The two exits
  (fixed → recurrence stops; aged-out → quiet) keep the board honest.

Design mirrors ``cycle_report.py``: the decisions are **pure functions over
primitives** (unit-testable with no DB, no clock, no network); the DB reads/writes
ride the store's single writer; the GitHub calls sit behind an injectable ``gh``
adapter so tests never shell out. This is Stage 1 only — it never edits code and
never merges anything (that is Stage 2, and §3 of the proposal is reopened). So
there is no self-modification here, and no cognition call: it is pure wiring.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from ..state_store.problems import PROBLEM_CATEGORIES

# ---- tunables (env-overridable) --------------------------------------------
#: distinct run-cycles a problem must survive before it earns an issue (O1 —
#: rescues the ops-agent O4 TREND_REPEAT_THRESHOLD=3). A one-night burst is one
#: cycle; three cycles running is a real, file-worthy problem.
RECURRENCE_THRESHOLD = int(os.environ.get("DEVCLAW_SELF_ISSUE_MIN_CYCLES", "3"))
#: quiet span after which an open self-filed issue auto-closes as stale (O2 /
#: backlog #259 age-out). Cycles are ~daily, so K cycles ≈ K days.
_QUIET_DAYS = int(os.environ.get("DEVCLAW_SELF_ISSUE_QUIET_DAYS", "3"))
QUIET_MS = _QUIET_DAYS * 24 * 3600 * 1000
#: cap on NEW issues opened per cycle (O4 noise budget); suppressed ones are
#: named in the cycle-report line, never silently dropped.
MAX_NEW_ISSUES_PER_CYCLE = int(os.environ.get("DEVCLAW_SELF_ISSUE_MAX_PER_CYCLE", "3"))

#: the self-filed marker label + the per-class label prefix (O3). Labels map from
#: ``problems.category`` (NOT eval_outcomes.failure_class — distinct taxonomies).
SELF_FILED_LABEL = "devclaw:self-filed"
_CLASS_PREFIX = "class:"

#: Stage 2 (P2 — FIX pickup) labels + concurrency. A human opt-in ``accepted``
#: label (O5) on a self-filed issue is the ONLY thing devclaw picks up — it never
#: starts modifying itself unprompted. ``devclaw:fixing`` marks an issue already
#: claimed by a self-fix goal: a visible, restart-safe concurrency signal. There is
#: NO auto-merge in P2 — the goal opens a PR a HUMAN merges (proposal §5A; the tiered
#: blast-radius classifier is deferred to P2.1/P2.2).
ACCEPTED_LABEL = "accepted"
FIXING_LABEL = "devclaw:fixing"
#: how many self-fix goals may be in flight at once. Concurrency 1 = serialize
#: self-modification: parallel self-fixes multiply the self-brick surface and muddy
#: failure attribution (proposal §5A). Tunable via env.
SELF_FIX_CONCURRENCY = int(os.environ.get("DEVCLAW_SELF_FIX_CONCURRENCY", "1"))


# ---- pure decisions (no DB, no clock, no network) ---------------------------

def labels_for(problem: dict) -> list[str]:
    """The GitHub labels for a problem: the self-filed marker + ``class:<cat>``.
    Falls back to ``other`` for an unknown category so a mis-typed category can
    never produce a bogus label."""
    cat = (problem.get("category") or "other").strip()
    if cat not in PROBLEM_CATEGORIES:
        cat = "other"
    return [SELF_FILED_LABEL, f"{_CLASS_PREFIX}{cat}"]


def should_file(problem: dict, cycle_count: int, *, threshold: int = RECURRENCE_THRESHOLD) -> bool:
    """FILE (open or reopen) iff the problem has survived ``>= threshold`` distinct
    cycles, has at least one terminal occurrence, and is not already tracked by an
    OPEN issue. A self-healed-only block (``terminal_count == 0``) never qualifies —
    the pause/heal machinery working is not a bug to file."""
    if cycle_count < threshold:
        return False
    if int(problem.get("terminal_count") or 0) <= 0:
        return False
    # Already open → nothing to file (idempotent; recurrence on an open issue is
    # a no-op, not a duplicate). None (never filed) or 'closed' (recurred after
    # close) both qualify.
    return (problem.get("issue_state") or None) != "open"


def should_close_stale(problem: dict, now_ms: int, *, quiet_ms: int = QUIET_MS) -> bool:
    """CLOSE iff the problem has an OPEN issue and has not been seen for
    ``quiet_ms`` (the age-out exit). Pure over ``last_seen_ms`` vs ``now_ms``."""
    if (problem.get("issue_state") or None) != "open":
        return False
    if problem.get("issue_number") is None:
        return False
    last_seen = int(problem.get("last_seen_ms") or 0)
    return (now_ms - last_seen) >= quiet_ms


def issue_title(problem: dict) -> str:
    cat = (problem.get("category") or "other").strip()
    kind = (problem.get("kind") or "").strip()
    summary = (problem.get("summary") or "").strip()
    tail = kind or summary or "recurring failure"
    return f"[self-filed] {cat}: {tail}"[:240]


def issue_body(problem: dict, cycle_count: int) -> str:
    """The grounded issue body — failure class, counts, first/last seen, the
    goals/tasks it hit, and the dedup fingerprint (the stable identity)."""
    fp = problem.get("fingerprint", "")
    return (
        "> Auto-filed by devclaw's self-issue-filing (Stage 1). This problem "
        f"recurred across **{cycle_count}** run-cycles.\n\n"
        f"- **Category:** `{problem.get('category', '')}`\n"
        f"- **Kind:** {problem.get('kind') or '—'}\n"
        f"- **Occurrences:** {problem.get('count', 0)} "
        f"(terminal {problem.get('terminal_count', 0)}, "
        f"recovered {problem.get('recovered_count', 0)})\n"
        f"- **First seen (ms):** {problem.get('first_seen_ms', 0)}  "
        f"**Last seen (ms):** {problem.get('last_seen_ms', 0)}\n"
        f"- **Last goal / task:** `{problem.get('last_goal_id') or '—'}` / "
        f"`{problem.get('last_task_id') or '—'}`\n\n"
        f"**Sample:**\n```\n{(problem.get('sample_message') or '').strip()[:1500]}\n```\n\n"
        f"<sub>fingerprint: `{fp}`</sub>"
    )


# ---- the injectable GitHub adapter (tests pass a fake) ----------------------

class GhAdapter(Protocol):
    async def ensure_label(self, repo: str, name: str) -> None: ...
    async def create_issue(self, repo: str, *, title: str, body: str, labels: list[str]) -> Optional[int]: ...
    async def reopen_issue(self, repo: str, number: int, *, comment: str) -> bool: ...
    async def close_issue(self, repo: str, number: int, *, comment: str) -> bool: ...
    # Stage 2 (P2 — FIX pickup):
    async def list_issues(self, repo: str, *, labels: list[str], state: str = "open") -> list[dict]: ...
    async def mark_fixing(self, repo: str, number: int, *, label: str, comment: str) -> bool: ...


async def _run(*args: str) -> tuple[int, str]:
    """Run a command, return (exit_code, combined output). Never raises. Mirrors
    ``delivery/repo.py`` — the subprocess boundary of the whole module."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        return 127, f"{args[0]} not runnable: {exc}"
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


class GhCli:
    """Real adapter: shells ``gh`` service-side (O8 — a GITHUB_TOKEN credential,
    never ``ANTHROPIC_*``; the OAuth-only cognition invariant is untouched). Every
    method is fail-loud-not-fatal: a GitHub hiccup logs and returns a falsey
    result so the caller records it and moves on — a filing failure NEVER wedges
    the cycle edge."""

    async def ensure_label(self, repo: str, name: str) -> None:
        # Created-on-first-use (O3). --force makes it idempotent.
        await _run("gh", "label", "create", name, "--repo", repo, "--force")

    async def create_issue(self, repo: str, *, title: str, body: str, labels: list[str]) -> Optional[int]:
        args = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
        for lbl in labels:
            args += ["--label", lbl]
        rc, out = await _run(*args)
        if rc != 0:
            sys.stderr.write(f"self-issue: create failed on {repo}: {out}\n")
            return None
        return _parse_issue_number(out)

    async def reopen_issue(self, repo: str, number: int, *, comment: str) -> bool:
        rc, out = await _run("gh", "issue", "reopen", str(number), "--repo", repo)
        if rc != 0:
            sys.stderr.write(f"self-issue: reopen #{number} failed on {repo}: {out}\n")
            return False
        await _run("gh", "issue", "comment", str(number), "--repo", repo, "--body", comment)
        return True

    async def close_issue(self, repo: str, number: int, *, comment: str) -> bool:
        rc, out = await _run(
            "gh", "issue", "close", str(number), "--repo", repo, "--comment", comment
        )
        if rc != 0:
            sys.stderr.write(f"self-issue: close #{number} failed on {repo}: {out}\n")
            return False
        return True

    # ---- Stage 2 (P2 — FIX pickup) --------------------------------------------

    async def list_issues(self, repo: str, *, labels: list[str], state: str = "open") -> list[dict]:
        # ``--label`` repeated is AND semantics — the issue must carry ALL of them.
        args = ["gh", "issue", "list", "--repo", repo, "--state", state,
                "--json", "number,title,body,labels"]
        for lbl in labels:
            args += ["--label", lbl]
        rc, out = await _run(*args)
        if rc != 0:
            sys.stderr.write(f"self-fix: list issues failed on {repo}: {out}\n")
            return []
        try:
            data = json.loads(out or "[]")
        except ValueError:
            sys.stderr.write(f"self-fix: unparseable issue list on {repo}: {out[:200]}\n")
            return []
        return data if isinstance(data, list) else []

    async def mark_fixing(self, repo: str, number: int, *, label: str, comment: str) -> bool:
        # ``--add-label`` is idempotent (re-adding an existing label is a no-op).
        rc, out = await _run(
            "gh", "issue", "edit", str(number), "--repo", repo, "--add-label", label
        )
        if rc != 0:
            sys.stderr.write(f"self-fix: add-label #{number} failed on {repo}: {out}\n")
            return False
        await _run("gh", "issue", "comment", str(number), "--repo", repo, "--body", comment)
        return True


def _parse_issue_number(gh_output: str) -> Optional[int]:
    """``gh issue create`` prints the new issue URL; pull the trailing number."""
    tail = gh_output.strip().rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


# ---- orchestration (cycle-close edge) ---------------------------------------

@dataclass
class SelfIssueResult:
    filed: list[int] = field(default_factory=list)       # newly opened issue #s
    reopened: list[int] = field(default_factory=list)    # closed→recurred
    closed: list[int] = field(default_factory=list)      # aged-out stale
    suppressed: list[str] = field(default_factory=list)  # over the per-cycle cap (fingerprints)

    def report_line(self) -> str:
        """One line for the cycle-report body (O7 — the report links what it did)."""
        if not (self.filed or self.reopened or self.closed or self.suppressed):
            return ""
        parts = []
        if self.filed:
            parts.append("filed " + ", ".join(f"#{n}" for n in self.filed))
        if self.reopened:
            parts.append("reopened " + ", ".join(f"#{n}" for n in self.reopened))
        if self.closed:
            parts.append("closed " + ", ".join(f"#{n}" for n in self.closed))
        if self.suppressed:
            parts.append(f"suppressed {len(self.suppressed)} over cap")
        return "self-issues: " + "; ".join(parts)


def self_repo() -> Optional[str]:
    """The repo devclaw files against — itself (O6). Configured explicitly via
    ``DEVCLAW_SELF_REPO`` (``owner/name``); unset ⇒ the whole feature is a no-op
    (default + every test path shells nothing)."""
    slug = (os.environ.get("DEVCLAW_SELF_REPO") or "").strip()
    return slug or None


async def run_self_issue_filing(
    store,
    *,
    cycle_key: str,
    start_ms: int,
    end_ms: int,
    now_ms: int,
    repo: Optional[str] = None,
    gh: Optional[GhAdapter] = None,
    threshold: int = RECURRENCE_THRESHOLD,
    quiet_ms: int = QUIET_MS,
    per_cycle_cap: int = MAX_NEW_ISSUES_PER_CYCLE,
) -> SelfIssueResult:
    """FILE recurring problems + CLOSE stale ones, at cycle close. Zero LLM;
    shells ``gh`` only when ``repo`` is set and there is real work. Best-effort
    per problem — one GitHub failure logs and is skipped, never wedges the edge.
    Returns what it did, for the cycle-report line."""
    result = SelfIssueResult()
    repo = repo or self_repo()
    if not repo:  # feature off (unset env) — no-op, no subprocess
        return result
    gh = gh or GhCli()

    # 1) record this cycle's membership for everything active in the window, then
    #    decide filing off the now-current cross-cycle count.
    active = store.problems_active_in_window(start_ms, end_ms)
    new_budget = per_cycle_cap
    for p in active:
        fp = p.get("fingerprint", "")
        store.mark_problem_cycle(fp, cycle_key)
        cycle_count = store.problem_cycle_count(fp)
        if not should_file(p, cycle_count, threshold=threshold):
            continue
        try:
            if p.get("issue_number") is None:
                # NEW issue — subject to the per-cycle noise cap.
                if new_budget <= 0:
                    result.suppressed.append(fp)
                    continue
                new_budget -= 1
                for lbl in labels_for(p):
                    await gh.ensure_label(repo, lbl)
                number = await gh.create_issue(
                    repo, title=issue_title(p), body=issue_body(p, cycle_count),
                    labels=labels_for(p),
                )
                if number is not None:
                    store.set_problem_issue(fp, issue_number=number, issue_state="open")
                    result.filed.append(number)
            else:
                # Recurred after a close → reopen (not capped; not new noise).
                number = int(p["issue_number"])
                if await gh.reopen_issue(
                    repo, number,
                    comment=f"Recurred — now across {cycle_count} run-cycles.",
                ):
                    store.set_problem_issue(fp, issue_number=number, issue_state="open")
                    result.reopened.append(number)
        except Exception as exc:  # noqa: BLE001 — one problem's failure never wedges the rest
            sys.stderr.write(f"self-issue: filing {fp} failed: {exc}\n")

    # 2) age-out: close open issues whose problem has gone quiet.
    for p in store.open_issue_problems():
        if not should_close_stale(p, now_ms, quiet_ms=quiet_ms):
            continue
        fp = p.get("fingerprint", "")
        number = int(p["issue_number"])
        try:
            if await gh.close_issue(
                repo, number,
                comment=(
                    f"Auto-closed as stale — not seen for ≥ {quiet_ms // (24*3600*1000)} "
                    "cycle-spans. Reopens automatically if it recurs."
                ),
            ):
                store.set_problem_issue(fp, issue_number=number, issue_state="closed")
                result.closed.append(number)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"self-issue: closing #{number} ({fp}) failed: {exc}\n")

    return result


# ============================================================================
# Stage 2 — FIX pickup (P2). Proposal §5A: at the SAME once-per-cycle edge, turn a
# human-``accepted`` self-filed issue into ONE ``one_shot`` self-fix goal that opens
# a PR for HUMAN review. NO auto-merge here (deferred to P2.1/P2.2). The decisions
# are pure functions over primitives; goal creation stays in ``GoalService`` (passed
# in as ``create_goal``, mirroring the injectable ``gh``) so this module never
# reaches into the goal store itself.
# ============================================================================

def _label_names(issue: dict) -> list[str]:
    """``gh issue list --json labels`` yields ``[{"name": ...}, ...]``; normalise to
    plain names so the selection logic works over primitives (also tolerates a fake
    that passes bare strings)."""
    out = []
    for lbl in issue.get("labels") or []:
        out.append(lbl.get("name", "") if isinstance(lbl, dict) else str(lbl))
    return out


def select_for_pickup(
    issues: list[dict],
    *,
    fixing_label: str = FIXING_LABEL,
    concurrency: int = SELF_FIX_CONCURRENCY,
) -> list[dict]:
    """Which accepted issues to spawn a self-fix goal for THIS cycle. Issues already
    carrying ``fixing_label`` are counted as in-flight; the remaining concurrency
    budget is filled from the fresh ones in list order. Pure — no DB, no network."""
    in_flight = sum(1 for i in issues if fixing_label in _label_names(i))
    fresh = [i for i in issues if fixing_label not in _label_names(i)]
    budget = max(0, concurrency - in_flight)
    return fresh[:budget]


def self_fix_goal_id(number: int) -> str:
    """Deterministic per-issue goal id → idempotent: a re-pick raises
    ``FileExistsError`` in the store instead of double-spawning (proposal §5A)."""
    return f"self-fix-issue-{number}"


def self_repo_url(slug: str) -> str:
    """Slug (``owner/name``, from ``DEVCLAW_SELF_REPO``) → the clone URL the engine
    needs (``git clone`` wants a real URL, not a slug; cf. ``delivery/repo.py``)."""
    return f"https://github.com/{slug}.git"


def self_fix_workspace(goal_id: str) -> str:
    """Where the self-fix goal's clone lands. In prod the sandbox only binds paths
    under ``DEVCLAW_CONTAINER_PATH_PREFIX`` (``engine/sandcastle.py``), so derive from
    it; unset (dev/host/tests) ⇒ the ``/repos/<id>`` convention the tests use."""
    base = (os.environ.get("DEVCLAW_CONTAINER_PATH_PREFIX") or "").rstrip("/") or "/repos"
    return f"{base}/{goal_id}"


def self_fix_objective(issue: dict, slug: str) -> str:
    number = issue.get("number")
    title = (issue.get("title") or "").strip()
    head = f"Fix {slug} issue #{number}: {title}".rstrip()
    body = (issue.get("body") or "").strip()
    if body:
        return f"{head}\n\nIssue body:\n{body[:1500]}"
    return head


def self_fix_done_when(number: int, slug: str) -> str:
    # ≥20 chars (admission's ``vague_done_when`` gate) and grounds the goal on a PR,
    # never an auto-merge — a human reviews and merges (proposal §5A).
    return (
        f"The root cause behind {slug} issue #{number} is fixed and a pull request is "
        f"open against {slug} with the full test suite and all quality gates passing; a "
        f"human reviews and merges it (no auto-merge)."
    )


@dataclass
class SelfFixResult:
    #: (issue_number, goal_id) claimed this cycle — new spawns AND re-claims of an
    #: already-existing goal whose label was missing (self-heal).
    picked: list = field(default_factory=list)

    def report_line(self) -> str:
        """One line for the cycle-report body (mirrors ``SelfIssueResult``)."""
        if not self.picked:
            return ""
        return "self-fix: picked up " + ", ".join(f"#{n}→{g}" for n, g in self.picked)


async def run_self_fix_pickup(
    create_goal: Callable[..., dict],
    *,
    repo: Optional[str] = None,
    gh: Optional[GhAdapter] = None,
    concurrency: int = SELF_FIX_CONCURRENCY,
) -> SelfFixResult:
    """Pick up human-``accepted`` self-filed issues → open ONE ``one_shot`` self-fix
    goal each (up to ``concurrency``), and claim them with ``FIXING_LABEL``. ZERO LLM
    to detect (a ``gh issue list`` + pure selection); env-gated on ``DEVCLAW_SELF_REPO``
    (unset ⇒ no-op, shells nothing — the default + every test path); best-effort —
    a GitHub or admission failure logs and is skipped, never wedges the cycle edge.

    ``create_goal`` is ``GoalService.create_goal`` (injected, so this module never
    touches the goal store): a re-pick of an issue whose goal already exists raises
    ``FileExistsError`` and is treated as an idempotent re-claim, not an error."""
    result = SelfFixResult()
    repo = repo or self_repo()
    if not repo:  # feature off — no-op, no subprocess
        return result
    gh = gh or GhCli()

    try:
        issues = await gh.list_issues(
            repo, labels=[ACCEPTED_LABEL, SELF_FILED_LABEL], state="open"
        )
    except Exception as exc:  # noqa: BLE001 — a list hiccup never wedges the edge
        sys.stderr.write(f"self-fix: list issues failed on {repo}: {exc}\n")
        return result

    for issue in select_for_pickup(issues, concurrency=concurrency):
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        goal_id = self_fix_goal_id(number)
        try:
            create_goal(
                goal_id,
                objective=self_fix_objective(issue, repo),
                workspace_dir=self_fix_workspace(goal_id),
                repo_url=self_repo_url(repo),
                done_when=self_fix_done_when(number, repo),
                mode="one_shot",
                open_pr=True,
            )
        except FileExistsError:
            # Goal already exists (a prior cycle spawned it but the claim label
            # didn't stick) — fall through to re-apply the label. Self-heal, not error.
            pass
        except Exception as exc:  # noqa: BLE001 — one bad issue never wedges the rest
            sys.stderr.write(f"self-fix: create_goal for #{number} failed: {exc}\n")
            continue
        try:
            await gh.mark_fixing(
                repo, number, label=FIXING_LABEL,
                comment=(
                    f"devclaw picked this up as goal `{goal_id}` (one-shot self-fix). "
                    "It will open a PR for human review — no auto-merge."
                ),
            )
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"self-fix: claim label on #{number} failed: {exc}\n")
        result.picked.append((number, goal_id))

    return result
