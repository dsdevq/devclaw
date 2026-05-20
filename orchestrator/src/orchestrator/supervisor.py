"""Run supervisor — the project_curator equivalent.

Walks a single ~/.life/projects/<slug>/runs/<run>/dag.yaml on every cron tick:

  1. Reconcile pass — read each dispatched node's per-task spec.yaml; if
     spec.status is `done` flip dag node to `verified_done`; if `blocked`
     flip to `verification_failed`.
  2. Retry-or-escalate pass — for each verification_failed node, apply the
     curator's internally-resolvable retry-once rule (architecture §6.3)
     or escalate the Run.
  3. Dispatch pass — find nodes whose deps are all `verified_done` and
     who are themselves `pending`; for each, generate a per-task spec.yaml
     under `runs/<run>/tasks/<task_id>/` and subprocess.Popen the per-task
     dispatch CLI. Mark the dag node `dispatched`. Cap: 3 per tick.
  4. Run-complete check — if every node is `verified_done`, mark the Run
     `completed` and (TODO) emit a completion announce event.

Pure mechanism: no LLM calls, no Telegram I/O (announces go through a
callback so the caller decides where to send them). Honors the killswitch
at ~/.life/system/cron-paused.

Dispatch is async via subprocess.Popen — the supervisor returns immediately
after dispatching, the per-task graph runs in its own process, and the
SWEEP cron (~/.life/system/cron-paused-aware reap+watchdog) catches both
successful completions (via result.json on disk) and ghosts (via
watchdog_deadline). The supervisor reconciles on its NEXT heartbeat.
"""

from __future__ import annotations

import logging
import secrets
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from orchestrator.dispatch import (
    compute_watchdog_deadline,
    load_spec,
    now_utc,
    persist_spec,
)
from orchestrator.state.models import (
    Budget,
    DagNode,
    RequesterRoute,
    Result,
    Run,
    RunnerStatus,
    RunStatus,
    TaskKind,
    TaskSpec,
    TaskStatus,
    VerifierStatus,
)

logger = logging.getLogger(__name__)

DISPATCH_CAP_PER_TICK = 3

# Failure reasons the curator can resolve internally with one retry.
INTERNALLY_RESOLVABLE_BLOCKERS = {
    "tests_failed",
    "precommit_hook_failed",
    "merge_conflict",
    "time_budget_exceeded",
    "runner_silent_past_deadline",
    "verification_failed",
}


# ─── Telegram-announce abstraction (no I/O coupling) ─────────────────────────


AnnounceCallback = Callable[[str, str], None]
"""(channel, message) → None. Caller (e.g. CLI / OpenClaw glue) decides delivery."""


def _noop_announce(channel: str, message: str) -> None:  # noqa: ARG001
    logger.info("announce(%s): %s", channel, message)


# Lifecycle events use PR #21's (channel, target, message) shape — distinct from
# the supervisor's escalate-only `AnnounceCallback` above.
EventsAnnounce = Callable[[str, str, str], None]


def _noop_events_announce(channel: str, target: str, message: str) -> None:  # noqa: ARG001
    return None


# ─── Dispatch abstraction (so tests don't fork real processes) ───────────────


SpecDispatcher = Callable[[Path], Optional[str]]
"""Spec.yaml path in → (optional) child process identifier out. Default impl Popens the per-task CLI."""


def _popen_per_task_cli(spec_path: Path) -> str:
    """Production dispatcher: Popen the `devclaw-orchestrator dispatch` CLI."""
    # We resolve `devclaw-orchestrator` from PATH; the cron must invoke us
    # under an environment where the venv bin is on PATH.
    proc = subprocess.Popen(
        [
            "devclaw-orchestrator",
            "dispatch",
            str(spec_path),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    return f"pid:{proc.pid}"


# ─── dag.yaml load + save ────────────────────────────────────────────────────


def load_run(dag_path: Path) -> Run:
    """Read + validate a dag.yaml into a Run."""
    return Run.model_validate(yaml.safe_load(dag_path.read_text()))


def persist_run(run: Run, dag_path: Path) -> None:
    """Atomic write of Run to dag.yaml."""
    payload = run.model_dump(mode="json", exclude_none=False)
    tmp = dag_path.with_suffix(dag_path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False, default_flow_style=False))
    tmp.replace(dag_path)


# ─── pure helpers ────────────────────────────────────────────────────────────


def deps_satisfied(node: DagNode, by_id: dict[str, DagNode]) -> bool:
    """All of `node.depends_on` must be in runner_status verified_done."""
    for dep_id in node.depends_on:
        dep = by_id.get(dep_id)
        if dep is None or dep.runner_status != RunnerStatus.verified_done:
            return False
    return True


def find_ready_nodes(run: Run) -> list[DagNode]:
    """Nodes whose deps are satisfied AND who are themselves pending."""
    by_id = {n.id: n for n in run.tasks}
    ready: list[DagNode] = []
    for node in run.tasks:
        if node.runner_status != RunnerStatus.pending:
            continue
        if deps_satisfied(node, by_id):
            ready.append(node)
    return ready


def find_dispatched_nodes(run: Run) -> list[DagNode]:
    return [n for n in run.tasks if n.runner_status == RunnerStatus.dispatched]


def find_verification_failed_nodes(run: Run) -> list[DagNode]:
    return [n for n in run.tasks if n.runner_status == RunnerStatus.verification_failed]


def all_verified_done(run: Run) -> bool:
    return all(n.runner_status == RunnerStatus.verified_done for n in run.tasks)


# ─── per-task spec generation ────────────────────────────────────────────────


def _short_hex(n: int = 4) -> str:
    return secrets.token_hex(n // 2)


def generate_task_spec(
    run: Run,
    node: DagNode,
    run_dir: Path,
    *,
    requester_route: RequesterRoute,
    project_settings_target_repo: str | None = None,
) -> tuple[TaskSpec, Path]:
    """Materialize a spec.yaml for one dag node and write it to disk.

    Returns (spec, spec_path). Caller is responsible for the subsequent dispatch step.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    task_id = f"{run.run_id}-{node.id}-{_short_hex(4)}"
    task_dir = run_dir / "tasks" / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    spec_path = task_dir / "spec.yaml"

    spec = TaskSpec(
        task_id=task_id,
        created_at=now_utc(),
        created_by="supervisor",
        requester_route=requester_route,
        verbatim_intent=(
            f"Run node `{node.id}` of {run.run_id}: {node.title}.\n\n"
            + "\n".join(f"Acceptance: {ac}" for ac in node.acceptance_criteria)
        ),
        kind=node.kind,
        acceptance_criteria=node.acceptance_criteria,
        budget=Budget(max_runtime_seconds=node.budget_seconds),
        target_repo=node.target_repo or project_settings_target_repo,
        target_branch=node.target_branch,
        project=run.project,
        run=run.run_id,
        run_node=node.id,
        proposal_path=run.proposal,
        context_files=[
            Path("~/.life/projects") / run.project / "plan.md",
            Path("~/.life/projects") / run.project / "recon.md",
            run_dir / "dag.yaml",
        ],
        status=TaskStatus.ready,
    )

    persist_spec(spec, spec_path)
    # Stamp the dag node's spec_path immediately so reconcile can find it next tick.
    node.spec_path = spec_path
    return spec, spec_path


# ─── one tick result ─────────────────────────────────────────────────────────


@dataclass
class SupervisorResult:
    run_id: str
    reconciled: list[str] = field(default_factory=list)  # node ids flipped to verified_done
    failed: list[str] = field(default_factory=list)  # node ids flipped to verification_failed
    retried: list[str] = field(default_factory=list)  # node ids requeued for retry
    escalated: list[str] = field(default_factory=list)  # node ids escalated
    dispatched: list[str] = field(default_factory=list)  # node ids newly dispatched
    completed: bool = False
    skipped_killswitch: bool = False

    def summary(self) -> str:
        if self.skipped_killswitch:
            return f"supervisor_paused {self.run_id}: killswitch present"
        parts = []
        if self.reconciled:
            parts.append(f"reconciled={len(self.reconciled)}")
        if self.failed:
            parts.append(f"failed={len(self.failed)}")
        if self.retried:
            parts.append(f"retried={len(self.retried)}")
        if self.escalated:
            parts.append(f"escalated={len(self.escalated)}")
        if self.dispatched:
            parts.append(f"dispatched={len(self.dispatched)}")
        if self.completed:
            parts.append("RUN_COMPLETE")
        return f"supervisor {self.run_id}: " + (", ".join(parts) if parts else "no-op")


# ─── reconcile + retry + dispatch ────────────────────────────────────────────


def _reconcile_dispatched_nodes(
    run: Run, *, result: SupervisorResult
) -> None:
    """For each dispatched node, peek at its spec.yaml status and flip dag accordingly."""
    for node in find_dispatched_nodes(run):
        if node.spec_path is None:
            continue
        try:
            spec = load_spec(Path(str(node.spec_path)).expanduser())
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001
            logger.warning("supervisor: cannot read spec for %s: %s", node.id, exc)
            continue

        if spec.status == TaskStatus.done:
            node.runner_status = RunnerStatus.verified_done
            node.verifier_status = VerifierStatus.passed
            node.completed_at = spec.completed_at
            node.verified_at = now_utc()
            node.evidence.result_summary = spec.result_summary
            result.reconciled.append(node.id)
        elif spec.status == TaskStatus.blocked:
            node.runner_status = RunnerStatus.verification_failed
            node.verifier_status = VerifierStatus.failed
            node.completed_at = spec.completed_at
            node.evidence.verification_failure_reason = (
                spec.result_summary or "unknown"
            )
            result.failed.append(node.id)


def _retry_or_escalate(
    run: Run,
    run_dir: Path,
    *,
    requester_route: RequesterRoute,
    result: SupervisorResult,
    announce: AnnounceCallback,
) -> None:
    """For each verification_failed node, apply retry-once or escalate."""
    for node in find_verification_failed_nodes(run):
        # Architecture §6.3: internally-resolvable failures get one retry; second failure escalates.
        reason = (node.evidence.verification_failure_reason or "").strip()
        # The reason may be wordy (e.g. "runner_silent_past_deadline — ..."), strip to leading token.
        blocker_token = reason.split(" ", 1)[0].rstrip(":,")

        if not node.retried and blocker_token in INTERNALLY_RESOLVABLE_BLOCKERS:
            # Retry: reset node to pending, mark retried, drop old spec_path (new one on next dispatch).
            node.runner_status = RunnerStatus.pending
            node.verifier_status = VerifierStatus.pending
            node.retried = True
            node.completed_at = None
            node.verified_at = None
            node.spec_path = None
            # Don't reset evidence — keeps the previous-failure context.
            result.retried.append(node.id)
            logger.info("supervisor: retrying %s after %s", node.id, blocker_token)
            continue

        # Otherwise escalate.
        run.status = RunStatus.blocked
        result.escalated.append(node.id)
        case = "5 (verification failed twice)" if node.retried else "1-or-6 (non-resolvable failure)"
        announce(
            requester_route.channel,
            f"⛔ Run blocked: {run.run_id}\n"
            f"   Node: {node.id}\n"
            f"   §6.3 case: {case}\n"
            f"   Reason: {reason[:200]}",
        )
        return  # one escalation per tick; remaining failed nodes wait for next heartbeat


def _dispatch_ready_nodes(
    run: Run,
    run_dir: Path,
    *,
    requester_route: RequesterRoute,
    dispatcher: SpecDispatcher,
    result: SupervisorResult,
    events_announce: EventsAnnounce = _noop_events_announce,
    events_chat_id: str = "default",
) -> None:
    """For each ready node (up to the per-tick cap), generate spec + dispatch."""
    ready = find_ready_nodes(run)
    for node in ready[:DISPATCH_CAP_PER_TICK]:
        spec, spec_path = generate_task_spec(
            run, node, run_dir, requester_route=requester_route
        )
        # Pre-write the watchdog_deadline + dispatch metadata on the spec so the sweep watchdog can pick it up if the child process ghosts.
        dispatched_at = now_utc()
        spec = spec.model_copy(
            update={
                "status": TaskStatus.dispatched_subagent,
                "dispatch_target": "subagent",
                "dispatched_at": dispatched_at,
                "watchdog_deadline": compute_watchdog_deadline(
                    dispatched_at, spec.budget.max_runtime_seconds
                ),
            }
        )
        persist_spec(spec, spec_path)

        run_id_str = dispatcher(spec_path) or ""
        node.runner_status = RunnerStatus.dispatched
        node.dispatched_at = dispatched_at
        if run_id_str:
            # Store the OS pid (or whatever id the dispatcher emits) in evidence for debugging.
            node.evidence.result_summary = (
                node.evidence.result_summary or ""
            ) + f" [dispatch_run={run_id_str}]"

        result.dispatched.append(node.id)
        try:
            from orchestrator.events import emit_dispatched

            emit_dispatched(
                task_id=spec.task_id,
                runner_kind=spec.dispatch_target or "subagent",
                chat_id=events_chat_id,
                announce=events_announce,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "events emit_dispatched failed for %s: %s", spec.task_id, exc
            )


# ─── one tick ────────────────────────────────────────────────────────────────


def is_killswitch_set(life_root: Path) -> bool:
    return (life_root / "system" / "cron-paused").exists()


def tick_run(
    dag_path: Path,
    *,
    life_root: Path | None = None,
    requester_route: RequesterRoute | None = None,
    dispatcher: SpecDispatcher = _popen_per_task_cli,
    announce: AnnounceCallback = _noop_announce,
    events_announce: EventsAnnounce = _noop_events_announce,
    events_chat_id: str = "default",
) -> SupervisorResult:
    """Run one supervisor heartbeat for one Run.

    Reconcile → retry/escalate → dispatch → run-complete-check, in that order.
    Honors the killswitch.
    """
    life_root = life_root or Path("~/.life").expanduser()
    requester_route = requester_route or RequesterRoute(channel="telegram", to="default")

    run = load_run(dag_path)
    out = SupervisorResult(run_id=run.run_id)

    if is_killswitch_set(life_root):
        out.skipped_killswitch = True
        return out

    if run.status not in (RunStatus.in_progress,):
        # Already terminal — nothing to do.
        return out

    run_dir = dag_path.parent

    _reconcile_dispatched_nodes(run, result=out)
    _retry_or_escalate(
        run, run_dir, requester_route=requester_route, result=out, announce=announce
    )
    if run.status == RunStatus.in_progress:  # may have been flipped to blocked by escalate
        _dispatch_ready_nodes(
            run,
            run_dir,
            requester_route=requester_route,
            dispatcher=dispatcher,
            result=out,
            events_announce=events_announce,
            events_chat_id=events_chat_id,
        )

    if all_verified_done(run):
        run.status = RunStatus.completed
        out.completed = True
        announce(
            requester_route.channel,
            f"🎉 Run complete: {run.run_id} ({len(run.tasks)}/{len(run.tasks)} nodes verified done)",
        )

    persist_run(run, dag_path)
    return out
