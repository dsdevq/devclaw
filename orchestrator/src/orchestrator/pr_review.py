"""Autonomous PR review + merge loop for devclaw-authored atomic PRs.

The fourth daemon thread (alongside sweep + supervise + audit). Closes the
last manual link in the autonomy chain: PR → merged. Operates only on PRs
whose head branch matches `kit/<task_id>` AND whose `spec.yaml` is on disk
in ~/.life/.

Cognition is spec-grounded: the prompt assembled here compares the PR diff
against the originating spec's `verbatim_intent` + `acceptance_criteria`.
The question is "does this match what was asked?", not abstract code review.

Contract-class PRs (image, deploy, secrets-touching) are NEVER auto-merged;
they get a "surfaced for review" comment. Defense in depth: even if the spec
declares contract_class=atomic, a diff that touches a contract path overrides
back to contract-class.

A circuit breaker watches main-branch CI after each auto-merge. Two
consecutive failures pause the loop until an operator clears the breaker by
flipping `paused: false` in `~/.life/state/pr_review/circuit.json`.
"""

from __future__ import annotations

import datetime as dt
import fnmatch
import json
import logging
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from orchestrator.dispatch import load_spec, persist_spec
from orchestrator.runners._subprocess import run_claude

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).with_name("pr_review.yaml")
BRANCH_PATTERN = re.compile(r"^kit/(?P<task_id>.+)$")
CIRCUIT_STATUS_CAP = 3
CIRCUIT_FAILURE_THRESHOLD = 2


# ─── Types ──────────────────────────────────────────────────────────────────


@dataclass
class PrCandidate:
    number: int
    title: str
    head_ref: str
    base_ref: str
    author: str
    repo: str  # "org/name"
    body: str = ""


@dataclass
class PrVerdict:
    verdict: str  # APPROVE | REJECT | UNCERTAIN
    reasoning: str
    risk_flags: list[str] = field(default_factory=list)


@dataclass
class PrAction:
    pr_number: int
    repo: str
    action: str  # merge | comment | skip | surface
    reason: str
    verdict: str | None = None


@dataclass
class PrReviewReport:
    generated_at: str
    considered: list[dict] = field(default_factory=list)
    actions: list[PrAction] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    merged: list[int] = field(default_factory=list)
    circuit_paused: bool = False
    circuit_reason: str | None = None


# ─── Config ─────────────────────────────────────────────────────────────────


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    return yaml.safe_load(config_path.read_text())


# ─── Subprocess helpers (mockable in tests) ─────────────────────────────────


GhRunner = Callable[[list[str]], subprocess.CompletedProcess]


def _default_gh(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


# ─── PR discovery ───────────────────────────────────────────────────────────


def discover_prs(repo: str, gh: GhRunner = _default_gh) -> list[PrCandidate]:
    """List open PRs in `repo` whose head branch is `kit/...`.

    Filters at the JSON-parse step; we deliberately do not pass `--author`
    here because the author filter belongs to config (watched_authors) and is
    applied later by the caller.
    """
    cp = gh([
        "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json", "number,title,headRefName,baseRefName,author,body",
        "--limit", "50",
    ])
    if cp.returncode != 0:
        logger.warning("gh pr list failed for %s: %s", repo, cp.stderr.strip())
        return []
    try:
        data = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return []
    out: list[PrCandidate] = []
    for item in data:
        head = item.get("headRefName", "") or ""
        if not BRANCH_PATTERN.match(head):
            continue
        author = (item.get("author") or {}).get("login", "") or ""
        out.append(
            PrCandidate(
                number=item["number"],
                title=item.get("title", "") or "",
                head_ref=head,
                base_ref=item.get("baseRefName", "main") or "main",
                author=author,
                repo=repo,
                body=item.get("body", "") or "",
            )
        )
    return out


def author_allowed(author: str, watched_authors: list[str]) -> bool:
    """A watched_authors of [] or ["*"] means branch-prefix-only filtering."""
    if not watched_authors or "*" in watched_authors:
        return True
    return author in watched_authors


def find_spec_for_task(life_root: Path, task_id: str) -> Path | None:
    """Locate spec.yaml for `task_id` anywhere under ~/.life/**/tasks/."""
    for glob in (
        f"tasks/{task_id}/spec.yaml",
        f"projects/*/tasks/{task_id}/spec.yaml",
        f"projects/*/runs/*/tasks/{task_id}/spec.yaml",
    ):
        for hit in life_root.glob(glob):
            return hit
    return None


def task_id_from_head(head_ref: str) -> str | None:
    m = BRANCH_PATTERN.match(head_ref)
    return m.group("task_id") if m else None


# ─── Pre-flight gates ───────────────────────────────────────────────────────


def get_pr_status(repo: str, number: int, gh: GhRunner = _default_gh) -> dict:
    cp = gh([
        "pr", "view", str(number),
        "--repo", repo,
        "--json", "mergeable,mergeStateStatus,statusCheckRollup,files",
    ])
    if cp.returncode != 0:
        return {}
    try:
        return json.loads(cp.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def is_mergeable(status: dict) -> bool:
    return status.get("mergeable") == "MERGEABLE"


def ci_green(status: dict, known_noise: list[dict]) -> bool:
    """All CI checks SUCCESS, or any failure matches the known-noise allowlist."""
    rollup = status.get("statusCheckRollup") or []
    for check in rollup:
        # GitHub returns either CheckRun or StatusContext shapes.
        conclusion = (check.get("conclusion") or check.get("state") or "").upper()
        name = check.get("name") or check.get("context") or ""
        if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED", ""):
            continue
        # Anything else (FAILURE, ERROR, CANCELLED, TIMED_OUT, PENDING …) must
        # be on the allowlist to count as green. PENDING is treated as not-green.
        if _matches_noise(name, conclusion, check, known_noise):
            continue
        return False
    return True


def _matches_noise(name: str, conclusion: str, check: dict, known_noise: list[dict]) -> bool:
    if conclusion == "PENDING":
        return False
    text = (check.get("text") or check.get("description") or "")
    for noise in known_noise:
        if noise.get("check_name") and noise["check_name"] != name:
            continue
        sub = noise.get("failure_substring") or ""
        if sub == "" or sub in text:
            return True
    return False


# ─── Contract-class determination ───────────────────────────────────────────


def diff_touches_contract(files_changed: list[str], contract_paths: list[str]) -> bool:
    for path in files_changed:
        for pattern in contract_paths:
            if fnmatch.fnmatch(path, pattern):
                return True
            # Also match the bare-basename case (e.g. pattern "Dockerfile" on path
            # "subdir/Dockerfile") which fnmatch alone won't catch.
            if "/" not in pattern and fnmatch.fnmatch(Path(path).name, pattern):
                return True
    return False


def determine_contract_class(
    spec_declared: str | None,
    files_changed: list[str],
    contract_paths: list[str],
) -> str:
    """Resolve the effective contract class of a PR.

    Spec declaration is honored *except* the defense-in-depth override: if the
    spec says atomic but the diff touches a contract path, treat as contract.
    """
    if diff_touches_contract(files_changed, contract_paths):
        return "contract"
    if spec_declared in ("contract", "architecture"):
        return spec_declared
    return "atomic"


# ─── Cognition ──────────────────────────────────────────────────────────────


def build_review_prompt(spec_yaml_text: str, pr_diff: str, pr_title: str, pr_body: str) -> str:
    """Assemble the spec-grounded review prompt.

    The prompt asks the cognition runner to emit a single-line JSON object on
    its last line — same shape as every other claude --print invocation in
    the orchestrator.
    """
    return f"""You are reviewing a pull request against the spec that requested it.

The spec is the source of truth. Your job is to decide whether the PR diff
satisfies the spec's verbatim_intent and acceptance_criteria — NOT abstract
code-quality review.

=== SPEC (spec.yaml) ===
{spec_yaml_text}

=== PR TITLE ===
{pr_title}

=== PR BODY ===
{pr_body}

=== PR DIFF ===
{pr_diff}

Emit your verdict as a single-line JSON object on the LAST line of output
(and nothing after it) with this exact shape:

  {{"verdict": "APPROVE" | "REJECT" | "UNCERTAIN",
    "reasoning": "<one short paragraph>",
    "risk_flags": ["<short flag>", ...]}}

APPROVE = the diff satisfies the spec and is safe to merge.
REJECT = the diff does not satisfy the spec, or introduces a clear bug.
UNCERTAIN = you cannot tell from the diff + spec; leave open for human review.

Include risk_flags for any contract-class signals the mechanism may have
missed (e.g. "touches deployment", "modifies secrets") — these will surface
the PR for human review regardless of verdict.
"""


def get_pr_diff(repo: str, number: int, gh: GhRunner = _default_gh) -> str:
    cp = gh(["pr", "diff", str(number), "--repo", repo])
    return cp.stdout if cp.returncode == 0 else ""


def render_verdict(prompt: str, *, timeout_seconds: int = 600) -> PrVerdict:
    """Run cognition and parse a verdict. UNCERTAIN on any parse failure."""
    sub = run_claude(prompt, timeout_seconds=timeout_seconds, permission_mode="plan")
    parsed = sub.parsed_json or {}
    verdict = str(parsed.get("verdict", "UNCERTAIN")).upper()
    if verdict not in ("APPROVE", "REJECT", "UNCERTAIN"):
        verdict = "UNCERTAIN"
    return PrVerdict(
        verdict=verdict,
        reasoning=str(parsed.get("reasoning", sub.blocker or "no reasoning returned")),
        risk_flags=list(parsed.get("risk_flags", []) or []),
    )


# ─── Acting on verdicts ─────────────────────────────────────────────────────


def merge_pr(repo: str, number: int, gh: GhRunner = _default_gh) -> bool:
    cp = gh([
        "pr", "merge", str(number),
        "--repo", repo,
        "--squash",
        "--delete-branch",
    ])
    if cp.returncode != 0:
        logger.warning("gh pr merge failed for %s#%d: %s", repo, number, cp.stderr.strip())
        return False
    return True


def comment_pr(repo: str, number: int, body: str, gh: GhRunner = _default_gh) -> bool:
    cp = gh([
        "pr", "comment", str(number),
        "--repo", repo,
        "--body", body,
    ])
    return cp.returncode == 0


def record_merge_on_spec(spec_path: Path, *, when: dt.datetime | None = None) -> None:
    """Stamp merged_at and extend result_summary on the spec (auto-merge path)."""
    from orchestrator.dispatch import stamp_merged_at

    stamp_merged_at(spec_path, when=when, source="auto")


# ─── Circuit breaker ────────────────────────────────────────────────────────


def _circuit_path(life_root: Path) -> Path:
    return life_root / "state" / "pr_review" / "circuit.json"


def load_circuit(life_root: Path) -> dict:
    path = _circuit_path(life_root)
    if not path.exists():
        return {"last_main_status": [], "paused": False, "paused_at": None}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"last_main_status": [], "paused": False, "paused_at": None}


def save_circuit(life_root: Path, state: dict) -> None:
    path = _circuit_path(life_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2))


def record_main_status(life_root: Path, status: str) -> dict:
    """Append a main-branch CI status, cap the rolling window, and trip the
    breaker if the tail is two-or-more consecutive failures."""
    state = load_circuit(life_root)
    history = list(state.get("last_main_status") or [])
    history.append(status)
    history = history[-CIRCUIT_STATUS_CAP:]
    state["last_main_status"] = history

    tail = history[-CIRCUIT_FAILURE_THRESHOLD:]
    if len(tail) >= CIRCUIT_FAILURE_THRESHOLD and all(s == "failure" for s in tail):
        state["paused"] = True
        state["paused_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    save_circuit(life_root, state)
    return state


def poll_main_ci(repo: str, gh: GhRunner = _default_gh) -> str | None:
    """Return 'success' | 'failure' | None for the latest completed main run."""
    cp = gh([
        "run", "list",
        "--repo", repo,
        "--branch", "main",
        "--limit", "1",
        "--json", "status,conclusion",
    ])
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    item = data[0]
    if item.get("status") != "completed":
        return None
    return "success" if item.get("conclusion") == "success" else "failure"


# ─── Telemetry ──────────────────────────────────────────────────────────────


def write_report(report: PrReviewReport, life_root: Path) -> Path:
    today = dt.date.today().isoformat()
    audits_dir = life_root / "audits"
    audits_dir.mkdir(parents=True, exist_ok=True)
    md_path = audits_dir / f"{today}-pr-review.md"

    lines = [
        f"## pr_review tick {report.generated_at}",
        "",
        f"- considered: {len(report.considered)}",
        f"- merged: {len(report.merged)} ({', '.join(map(str, report.merged)) or 'none'})",
        f"- actions: {len(report.actions)}",
        f"- skipped: {len(report.skipped)}",
    ]
    if report.circuit_paused:
        lines.append(f"- **CIRCUIT PAUSED**: {report.circuit_reason or 'see circuit.json'}")
    if report.actions:
        lines.append("")
        lines.append("### Actions")
        for a in report.actions:
            verdict = f" [{a.verdict}]" if a.verdict else ""
            lines.append(f"- `{a.repo}#{a.pr_number}` — **{a.action}**{verdict} — {a.reason}")
    if report.skipped:
        lines.append("")
        lines.append("### Skipped")
        for s in report.skipped:
            lines.append(f"- {s.get('repo', '?')}#{s.get('number', '?')} — {s.get('reason', '')}")
    lines.append("")

    with md_path.open("a") as fh:
        fh.write("\n".join(lines))
    return md_path


# ─── Main pipeline ──────────────────────────────────────────────────────────


def run_pr_review(
    life_root: Path,
    *,
    config_path: Path = CONFIG_PATH,
    gh: GhRunner = _default_gh,
    verdict_fn: Callable[[str], PrVerdict] | None = None,
) -> PrReviewReport:
    """One pr_review tick: discover → pre-flight → cognition → act."""
    cfg = load_config(config_path)
    watched_authors = cfg.get("watched_authors") or []
    known_noise = cfg.get("known_ci_noise") or []
    contract_paths = (cfg.get("contract_class_heuristics") or {}).get("contract_paths") or []
    cap = int(cfg.get("review_cap_per_tick", 3))
    repos = cfg.get("repos") or _discover_repos(life_root)

    report = PrReviewReport(
        generated_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
    )

    circuit = load_circuit(life_root)
    if circuit.get("paused"):
        report.circuit_paused = True
        report.circuit_reason = "operator-clear required"
        logger.warning(
            "pr_review: circuit paused (since %s); skipping tick",
            circuit.get("paused_at"),
        )
        write_report(report, life_root)
        return report

    reviewed = 0
    for repo in repos:
        if reviewed >= cap:
            break
        for pr in discover_prs(repo, gh=gh):
            if reviewed >= cap:
                break
            if not author_allowed(pr.author, watched_authors):
                report.skipped.append({"repo": repo, "number": pr.number, "reason": "author not watched"})
                continue
            task_id = task_id_from_head(pr.head_ref)
            if not task_id:
                continue
            spec_path = find_spec_for_task(life_root, task_id)
            if spec_path is None:
                report.skipped.append({"repo": repo, "number": pr.number, "reason": "no spec on disk"})
                continue
            try:
                spec = load_spec(spec_path)
            except Exception as exc:  # noqa: BLE001
                report.skipped.append({"repo": repo, "number": pr.number, "reason": f"spec load failed: {exc}"})
                continue
            if spec.merged_at is not None:
                report.skipped.append({"repo": repo, "number": pr.number, "reason": "already merged"})
                continue

            status = get_pr_status(repo, pr.number, gh=gh)
            files_changed = [f.get("path", "") for f in (status.get("files") or [])]
            cls = determine_contract_class(spec.contract_class, files_changed, contract_paths)
            report.considered.append({
                "repo": repo, "number": pr.number, "task_id": task_id, "contract_class": cls,
            })

            if cls != "atomic":
                comment_pr(
                    repo, pr.number,
                    "Surfaced for review by pr_review_loop: contract-class diff, manual merge required.",
                    gh=gh,
                )
                report.actions.append(PrAction(
                    pr_number=pr.number, repo=repo, action="surface",
                    reason=f"contract_class={cls}",
                ))
                reviewed += 1
                continue

            if not is_mergeable(status):
                report.skipped.append({"repo": repo, "number": pr.number, "reason": "not mergeable"})
                continue
            if not ci_green(status, known_noise):
                report.skipped.append({"repo": repo, "number": pr.number, "reason": "CI not green"})
                continue

            diff = get_pr_diff(repo, pr.number, gh=gh)
            prompt = build_review_prompt(
                spec_path.read_text(), diff, pr.title, pr.body,
            )
            verdict = (verdict_fn or render_verdict)(prompt)
            reviewed += 1

            if verdict.verdict == "APPROVE" and not verdict.risk_flags:
                if merge_pr(repo, pr.number, gh=gh):
                    record_merge_on_spec(spec_path)
                    report.merged.append(pr.number)
                    report.actions.append(PrAction(
                        pr_number=pr.number, repo=repo, action="merge",
                        reason=verdict.reasoning, verdict="APPROVE",
                    ))
                    main_status = poll_main_ci(repo, gh=gh)
                    if main_status:
                        new_state = record_main_status(life_root, main_status)
                        if new_state.get("paused"):
                            report.circuit_paused = True
                            report.circuit_reason = (
                                f"{CIRCUIT_FAILURE_THRESHOLD} consecutive main failures"
                            )
                            logger.error(
                                "pr_review: circuit tripped — pausing further auto-merges"
                            )
                            write_report(report, life_root)
                            return report
                else:
                    report.actions.append(PrAction(
                        pr_number=pr.number, repo=repo, action="merge_failed",
                        reason="gh pr merge non-zero exit", verdict="APPROVE",
                    ))
            elif verdict.verdict == "REJECT":
                comment_pr(repo, pr.number, verdict.reasoning, gh=gh)
                report.actions.append(PrAction(
                    pr_number=pr.number, repo=repo, action="comment",
                    reason=verdict.reasoning, verdict="REJECT",
                ))
            else:  # UNCERTAIN, or APPROVE with risk_flags
                report.actions.append(PrAction(
                    pr_number=pr.number, repo=repo, action="leave_open",
                    reason=verdict.reasoning or "uncertain", verdict=verdict.verdict,
                ))

    write_report(report, life_root)
    return report


def _discover_repos(life_root: Path) -> list[str]:
    """Find devclaw-managed repos by reading projects/*/settings.yaml's
    github_repo field. Returns a de-duplicated list."""
    seen: set[str] = set()
    for settings in life_root.glob("projects/*/settings.yaml"):
        try:
            data = yaml.safe_load(settings.read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        repo = data.get("github_repo")
        if isinstance(repo, str) and repo:
            seen.add(repo)
    return sorted(seen)
