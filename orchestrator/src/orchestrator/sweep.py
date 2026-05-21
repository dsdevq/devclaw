"""Periodic sweep: dispatch + reap + watchdog passes over all in-flight specs on disk.

Intended to be cron-fired every 15 minutes (the same cadence as the markdown `task_dispatch_15m`). Pure mechanism — no LLM calls, no LangGraph invocation in the sweep itself; the dispatch pass fires the per-task graph as a subprocess.

Honors the killswitch at `~/.life/system/cron-paused`.

Caps per invocation (architecture §6.2 spirit):
  - dispatch at most 3 ready atomic specs
  - reap at most 5 dispatched-with-artifact specs
  - watchdog at most 5 ghosted specs
  - reconcile-merges at most 5 done-but-unstamped specs per tick

Order: reap → watchdog → reconcile-merges → dispatch.

Reap first gives credit to late-but-complete runners before their deadline trips. Watchdog kills ghosts. Reconcile-merges runs BEFORE dispatch so a parent whose PR was just manually merged stamps `merged_at` in the same tick that releases its DAG-gated children (see `_ready_to_dispatch` for the merged_at gate added 2026-05-19).
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import json
import re

from orchestrator.dispatch import (
    compute_watchdog_deadline,
    find_completion_artifact,
    is_ghosted,
    load_spec,
    mark_ghosted,
    now_utc,
    persist_spec,
    reap_spec,
    stamp_merged_at,
)
from orchestrator.notify import notify_telegram
from orchestrator.run_summary import record_run
from orchestrator.state.models import Result, TaskSpec, TaskStatus

# Events-callback signature is intentionally identical to PR #21's
# daemon.AnnounceCallback. Inlined as a Callable here to avoid a daemon→sweep
# import cycle. The daemon passes its `events_announce` field down through
# `sweep_once`'s kwargs.
EventsAnnounce = Callable[[str, str, str], None]


def _noop_events_announce(channel: str, target: str, message: str) -> None:  # noqa: ARG001
    return None

DISPATCH_CAP_PER_TICK = 3
REAP_CAP_PER_TICK = 5
WATCHDOG_CAP_PER_TICK = 5
RECONCILE_CAP_PER_TICK = 5

# Defense-in-depth cap on simultaneous claude subprocesses across all sweep
# ticks. The VPS has ~3.7 GiB RAM and a single claude --print with 1M context
# peaks at 1–1.5 GiB; two or three in parallel push us into swap-thrash + OOM
# territory. The container memory cap was bumped to 2 GiB on 2026-05-21
# (separate TaskSpec against dsdevq/lifekit-stack), but we want this
# orchestrator-level guard so a future config slip can't bring back the freeze
# risk. Applies to ALL task kinds since every kind spawns claude.
DEFAULT_MAX_CONCURRENT_CLAUDES = 1

_PR_URL_RE = re.compile(r"https?://[^/\s]+/([^/\s]+/[^/\s]+)/pull/(\d+)")

DISPATCHED_STATUSES = {
    TaskStatus.dispatched_subagent,
    TaskStatus.dispatched_build,
}


# Dispatch abstraction so tests don't fork real processes.
SpecDispatcher = Callable[[Path], Optional[str]]

# `gh` shell-out abstraction for the reconciliation pass. Same shape as
# pr_review.GhRunner but kept local to avoid importing pr_review into sweep.
GhRunner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _default_gh(args: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _popen_dispatch_cli(spec_path: Path) -> str:
    """Production dispatcher: Popen the `devclaw-orchestrator dispatch` CLI.

    Per-task output (claude --print stdout/stderr) is appended to a `dispatch.log` next to the spec, so post-mortem debugging can recover what the runner actually said. The fd is intentionally not closed here — Popen retains it for the lifetime of the child.
    """
    log_path = spec_path.parent / "dispatch.log"
    log_fh = open(log_path, "ab")
    proc = subprocess.Popen(
        ["devclaw-orchestrator", "dispatch", str(spec_path)],
        stdout=log_fh,
        stderr=log_fh,
        close_fds=True,
    )
    return f"pid:{proc.pid}"

logger = logging.getLogger(__name__)


@dataclass
class SweepResult:
    """What a single sweep tick did. Useful for logs + tests."""

    scanned: int = 0
    dispatched: list[str] = field(default_factory=list)
    reaped: list[str] = field(default_factory=list)
    ghosted: list[str] = field(default_factory=list)
    reconciled_merges: list[str] = field(default_factory=list)
    skipped_killswitch: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.skipped_killswitch:
            return "sweep_paused: killswitch present"
        return (
            f"scanned={self.scanned} dispatched={len(self.dispatched)} "
            f"reaped={len(self.reaped)} ghosted={len(self.ghosted)} "
            f"reconciled_merges={len(self.reconciled_merges)} "
            f"errors={len(self.errors)}"
        )


def find_dispatched_specs(life_root: Path) -> list[Path]:
    """Locate every spec.yaml under ~/.life/ whose status looks dispatched-*.

    Scans BOTH atomic (`tasks/*/spec.yaml`) and run-bound (`projects/*/runs/*/tasks/*/spec.yaml`) paths.
    """
    candidates: list[Path] = []
    for glob in (
        "tasks/*/spec.yaml",
        "projects/*/tasks/*/spec.yaml",
        "projects/*/runs/*/tasks/*/spec.yaml",
    ):
        candidates.extend(life_root.glob(glob))
    return candidates


def find_all_specs(life_root: Path) -> list[Path]:
    """Locate every spec.yaml under ~/.life/ (any status).

    Used by the dispatch pass to find `status: ready` specs that need a runner.
    """
    candidates: list[Path] = []
    for glob in (
        "tasks/*/spec.yaml",
        "projects/*/tasks/*/spec.yaml",
        "projects/*/runs/*/tasks/*/spec.yaml",
    ):
        candidates.extend(life_root.glob(glob))
    return candidates


def is_killswitch_set(life_root: Path) -> bool:
    return (life_root / "system" / "cron-paused").exists()


def sweep_once(
    life_root: Path,
    *,
    dispatcher: SpecDispatcher = _popen_dispatch_cli,
    gh: GhRunner = _default_gh,
    events_announce: EventsAnnounce = _noop_events_announce,
    events_chat_id: str = "default",
    max_concurrent_claudes: int = DEFAULT_MAX_CONCURRENT_CLAUDES,
) -> SweepResult:
    """Run one sweep tick: reap → watchdog → reconcile-merges → dispatch.

    Order rationale:
      - Reap first: a late-but-complete runner gets credit before its deadline trips.
      - Watchdog second: kill anything past deadline with no artifact.
      - Reconcile merges third: stamp `merged_at` on specs whose PRs were
        merged manually (or by anything outside pr_review_loop). This must
        happen BEFORE dispatch so newly-stamped parents unblock their
        DAG-gated children in the same tick.
      - Dispatch last: fire new `ready` specs; if a spec just-reaped is `ready` again (it isn't, but logically) it could be redispatched in the same tick.
    """
    result = SweepResult()

    if is_killswitch_set(life_root):
        result.skipped_killswitch = True
        return result

    all_specs = find_all_specs(life_root)

    # Pass 1: reap completed-but-unflipped specs.
    for spec_path in all_specs:
        if len(result.reaped) >= REAP_CAP_PER_TICK:
            break
        try:
            spec = load_spec(spec_path)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"load failed: {spec_path} — {exc}")
            continue

        result.scanned += 1
        if spec.status not in DISPATCHED_STATUSES or spec.completed_at is not None:
            continue

        artifact = find_completion_artifact(spec_path.parent, spec.kind.value)
        if artifact is None:
            continue

        try:
            reaped = reap_spec(spec, artifact)
            persist_spec(reaped, spec_path)
            result.reaped.append(spec.task_id)
            logger.info("reaped %s via %s", spec.task_id, artifact.name)
            _emit_summary_for_reap(reaped, artifact)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"reap failed: {spec.task_id} — {exc}")

    # Pass 2: watchdog ghosted specs.
    for spec_path in all_specs:
        if len(result.ghosted) >= WATCHDOG_CAP_PER_TICK:
            break
        try:
            spec = load_spec(spec_path)
        except Exception:  # noqa: BLE001
            continue

        if not is_ghosted(spec):
            continue

        try:
            blocked = mark_ghosted(spec)
            persist_spec(blocked, spec_path)
            result.ghosted.append(spec.task_id)
            logger.info("watchdog ghosted %s", spec.task_id)
            record_run(
                spec=blocked,
                result=None,
                status="watchdog_killed",
                retries=0,
                verifier_result=None,
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"watchdog failed: {spec.task_id} — {exc}")

    # Pass 3: reconcile manual merges — for any spec.status==done with
    # target_repo set, merged_at null, and a discoverable PR URL, ask GitHub
    # whether the PR is merged; if so, stamp merged_at locally.
    for spec_path in all_specs:
        if len(result.reconciled_merges) >= RECONCILE_CAP_PER_TICK:
            break
        try:
            spec = load_spec(spec_path)
        except Exception:  # noqa: BLE001
            continue

        if not _needs_merge_reconciliation(spec):
            continue

        pr_info = _extract_pr_url(spec, spec_path.parent)
        if pr_info is None:
            continue
        repo, number = pr_info

        try:
            merged_at = _gh_pr_merged_at(repo, number, gh=gh)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(
                f"reconcile failed: {spec.task_id} — {exc}"
            )
            continue

        if merged_at is None:
            continue

        try:
            stamp_merged_at(spec_path, when=merged_at, source="reconcile")
            result.reconciled_merges.append(spec.task_id)
            logger.info(
                "reconciled merge for %s (gh said merged at %s)",
                spec.task_id,
                merged_at.isoformat(timespec="seconds"),
            )
        except Exception as exc:  # noqa: BLE001
            result.errors.append(
                f"reconcile stamp failed: {spec.task_id} — {exc}"
            )

    # Pass 4: dispatch ready atomic specs.
    # Run-bound specs are dispatched by the supervisor, not here, so this pass only fires for specs whose `run` field is null.
    # Build an id→spec map once so the readiness gate can look up dep status without re-reading disk per spec.
    specs_by_id: dict[str, TaskSpec] = {}
    for spec_path in all_specs:
        try:
            s = load_spec(spec_path)
        except Exception:  # noqa: BLE001
            continue
        specs_by_id[s.task_id] = s

    # Count claudes already in flight from earlier ticks. The cap below is a
    # global ceiling, not per-tick: if two long-running specs are still
    # dispatched-* on disk, this tick must not start a third.
    in_flight = sum(1 for s in specs_by_id.values() if s.status in DISPATCHED_STATUSES)

    unknown_dep_warning_emitted = False
    concurrency_skip_logged = False
    for spec_path in all_specs:
        if len(result.dispatched) >= DISPATCH_CAP_PER_TICK:
            break
        if in_flight >= max_concurrent_claudes:
            # Defense-in-depth memory cap reached. Leave the spec untouched —
            # do NOT flip to dispatched, do NOT flip to blocked. Next sweep
            # tick picks it up once an in-flight one finishes (reap or
            # watchdog flips the dispatched-* spec away).
            if not concurrency_skip_logged:
                logger.info(
                    "sweep: max_concurrent_claudes=%d reached (in_flight=%d); "
                    "deferring remaining ready specs to next tick",
                    max_concurrent_claudes,
                    in_flight,
                )
                concurrency_skip_logged = True
            break
        try:
            spec = load_spec(spec_path)
        except Exception:  # noqa: BLE001
            continue

        if spec.status != TaskStatus.ready or spec.run is not None:
            continue

        ready, reason = _ready_to_dispatch(spec, specs_by_id)
        if not ready:
            if reason == "unknown_dep" and not unknown_dep_warning_emitted:
                logger.warning(
                    "sweep: one or more ready specs reference unknown deps; leaving in ready"
                )
                unknown_dep_warning_emitted = True
            continue

        try:
            dispatched = _mark_atomic_dispatched(spec)
            persist_spec(dispatched, spec_path)
            specs_by_id[spec.task_id] = dispatched  # keep map current within the tick
            in_flight += 1  # newly-dispatched spec counts against the cap
            run_id_str = dispatcher(spec_path) or ""
            result.dispatched.append(spec.task_id)
            logger.info(
                "dispatched ready atomic spec %s (popen=%s)",
                spec.task_id,
                run_id_str,
            )
            notify_telegram(
                spec.requester_route.to,
                f"🚀 dispatched {spec.task_id} (kind={spec.kind.value})",
            )
            # Lifecycle event — see orchestrator.events. Wrapped in its own
            # try-block so an announce failure can never abort the dispatch loop.
            try:
                from orchestrator.events import emit_dispatched

                emit_dispatched(
                    task_id=spec.task_id,
                    runner_kind=dispatched.dispatch_target or "subagent",
                    chat_id=events_chat_id,
                    announce=events_announce,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("events emit_dispatched failed for %s: %s", spec.task_id, exc)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"dispatch failed: {spec.task_id} — {exc}")

    return result


def _emit_summary_for_reap(reaped_spec: TaskSpec, artifact: Path) -> None:
    """Append a runs.jsonl row for a spec that the reap pass just flipped.

    If the artifact is a `result.json` we can recover pr_url + verifier hint
    from it; otherwise (findings.md / run.log.jsonl) we fall back to a
    None-Result row whose verifier_result is "skipped".
    """
    parsed: Result | None = None
    if artifact.name == "result.json":
        try:
            parsed = Result.model_validate(json.loads(artifact.read_text()))
        except (json.JSONDecodeError, OSError, ValueError):
            parsed = None

    run_status = "done" if reaped_spec.status == TaskStatus.done else "failed"
    record_run(
        spec=reaped_spec,
        result=parsed,
        status=run_status,
        retries=0,
    )


def _ready_to_dispatch(
    spec: TaskSpec, all_specs_by_id: dict[str, TaskSpec]
) -> tuple[bool, str | None]:
    """Decide whether a `ready` spec is dispatch-eligible given current dep state.

    Returns (True, None) iff every `depends_on` entry resolves to a known spec
    whose status is `done` AND — for code-bearing parents (those with a
    `target_repo` set) — whose `merged_at` is non-null. The merged_at gate
    closes the window where a parent's runner has finished and opened a PR
    but the PR isn't on `main` yet, which previously caused children's
    runners to fail because their assumed-existing code wasn't there.

    Otherwise returns (False, reason) where reason is one of:
      - "unknown_dep": listed dep id has no matching spec on disk
      - "dep_not_done": dep exists but hasn't reached `done` yet
      - "dep_not_merged": dep is done but its PR hasn't been merged (only
        applies when the parent has `target_repo` set — research / chore
        parents with no code output skip this check)

    Empty depends_on trivially passes — backward-compatible with pre-DAG specs.
    """
    for dep_id in spec.depends_on:
        dep = all_specs_by_id.get(dep_id)
        if dep is None:
            return (False, "unknown_dep")
        if dep.status != TaskStatus.done:
            return (False, "dep_not_done")
        if dep.target_repo and dep.merged_at is None:
            return (False, "dep_not_merged")
    return (True, None)


def _needs_merge_reconciliation(spec: TaskSpec) -> bool:
    """A code-bearing spec that is done but whose PR isn't stamped as merged."""
    return (
        spec.status == TaskStatus.done
        and spec.target_repo is not None
        and spec.merged_at is None
    )


def _extract_pr_url(spec: TaskSpec, task_dir: Path) -> tuple[str, int] | None:
    """Find the (repo, pr_number) for `spec` if recorded anywhere on disk.

    Looks first at `result.json` next to the spec (the canonical runner output),
    then falls back to scanning `spec.result_summary` for a GitHub PR URL.
    Returns None if no URL is recoverable.
    """
    result_json = task_dir / "result.json"
    if result_json.is_file():
        try:
            data = json.loads(result_json.read_text())
            pr_url = data.get("pr_url")
            if isinstance(pr_url, str):
                m = _PR_URL_RE.search(pr_url)
                if m:
                    return (m.group(1), int(m.group(2)))
        except (json.JSONDecodeError, OSError):
            pass

    if spec.result_summary:
        m = _PR_URL_RE.search(spec.result_summary)
        if m:
            return (m.group(1), int(m.group(2)))

    return None


def _gh_pr_merged_at(repo: str, number: int, *, gh: GhRunner) -> datetime | None:
    """Return the merge timestamp from `gh pr view`, or None if not merged.

    Raises RuntimeError on a non-zero gh exit so the caller records it as an error
    (a transient gh failure should be surfaced, not silently ignored).
    """
    cp = gh(["pr", "view", str(number), "--repo", repo, "--json", "mergedAt"])
    if cp.returncode != 0:
        raise RuntimeError(
            f"gh pr view {repo}#{number} failed: {(cp.stderr or '').strip()[:200]}"
        )
    try:
        data = json.loads(cp.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh pr view {repo}#{number} produced unparseable JSON") from exc
    merged_at = data.get("mergedAt")
    if not merged_at:
        return None
    # gh returns RFC3339 with trailing 'Z'; datetime.fromisoformat handles that on 3.11+.
    try:
        return datetime.fromisoformat(merged_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"gh pr view {repo}#{number} bad mergedAt: {merged_at!r}") from exc


def detect_cycle(
    spec: TaskSpec, all_specs_by_id: dict[str, TaskSpec]
) -> list[str] | None:
    """If adding `spec` to the DAG introduces a cycle, return the cycle path; else None.

    The returned list reads as the offending chain (e.g. ["a", "b", "a"]). Used by
    `task_intake` (and any other writer) to reject a spec before it lands on disk.
    """
    graph = dict(all_specs_by_id)
    graph[spec.task_id] = spec

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {tid: WHITE for tid in graph}

    def visit(node: str, stack: list[str]) -> list[str] | None:
        color[node] = GRAY
        stack.append(node)
        for dep in graph[node].depends_on:
            if dep not in graph:
                continue  # unknown deps aren't a cycle; sweep handles them
            if color[dep] == GRAY:
                idx = stack.index(dep)
                return stack[idx:] + [dep]
            if color[dep] == WHITE:
                found = visit(dep, stack)
                if found is not None:
                    return found
        stack.pop()
        color[node] = BLACK
        return None

    # Only need to search from the newly-introduced node — the existing graph
    # is assumed acyclic (each prior insertion went through this check).
    return visit(spec.task_id, [])


def _mark_atomic_dispatched(spec: TaskSpec) -> TaskSpec:
    """Flip a ready atomic spec to dispatched-subagent with a watchdog deadline.

    Pure function; caller persists. We do this BEFORE Popening the dispatch CLI so a race with another concurrent sweep can't double-dispatch (next tick will see status=dispatched-* and skip).
    """
    dispatched_at = now_utc()
    return spec.model_copy(
        update={
            "status": TaskStatus.dispatched_subagent,
            "dispatch_target": "subagent",
            "dispatched_at": dispatched_at,
            "watchdog_deadline": compute_watchdog_deadline(
                dispatched_at, spec.budget.max_runtime_seconds
            ),
        }
    )


# Backwards-compatible alias — older callers use `find_dispatched_specs`.
find_dispatched_specs = find_all_specs
