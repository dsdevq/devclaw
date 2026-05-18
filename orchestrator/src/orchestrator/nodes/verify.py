"""verify_node — deterministic acceptance-criterion runner.

Pure mechanism. Each criterion gets best-effort interpreted as a shell command; failures route to the retry-or-escalate edge. Fuzzy ACs that genuinely need cognition (e.g. "PR body explains the rationale") would be handled by a separate `verify_cognitive_node` that shells to Claude — out of scope for the v0.0.1 slice.

The criterion grammar we support today is intentionally narrow — exactly what the current `verify-task` SKILL.md ran via bash:

  - "<bash command> exits 0"      → run it, check returncode
  - URL-based ones                 → curl + check status
  - file-existence ones            → Python os.path checks

For the v0.0.1 slice, we accept any string ending in `exits 0` as a bash command and run it. Everything else passes through as "manually-verified-by-runner" — the runner's result.json is authoritative.
"""

from __future__ import annotations

import shlex
import subprocess
from typing import Literal

from orchestrator.dispatch import now_utc
from orchestrator.state.models import GraphState, Result, RunnerStatus


def _run_bash_criterion(criterion: str) -> tuple[bool, str]:
    """Best-effort: extract a bash command from a criterion and run it.

    Returns (passed, evidence). Conservative: if we can't parse it cleanly, we pass it (the runner already self-verified).
    """
    lowered = criterion.lower()
    if "exits 0" not in lowered:
        return True, "criterion not bash-shaped; trusting runner self-verification"

    cmd_part = criterion.rsplit("exits 0", 1)[0].strip()
    cmd_part = cmd_part.split(" — ")[-1].strip()
    try:
        result = subprocess.run(
            cmd_part, shell=True, capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        return False, f"criterion check timed out (60s): {cmd_part[:120]}"

    if result.returncode == 0:
        return True, f"command exited 0: {cmd_part[:120]}"
    return False, f"command exited {result.returncode}: {cmd_part[:120]}\nstderr: {result.stderr[-200:]}"


def verify_node(state: GraphState) -> dict:
    """Run each acceptance criterion. Annotate state.result with verifier outcome."""
    if state.result is None:
        return {
            "error": "verify_node called with no result on state",
        }
    if state.result.status == "blocked":
        # runner self-reported blocked; no point verifying
        return {}

    spec = state.spec
    failures: list[str] = []

    for criterion in spec.acceptance_criteria:
        passed, evidence = _run_bash_criterion(criterion)
        if not passed:
            failures.append(evidence)

    if failures:
        updated = state.result.model_copy(
            update={
                "status": "blocked",
                "blocker": "verification_failed",
                "notes": (state.result.notes or "")
                + "\nverifier failures:\n"
                + "\n".join(f"  - {f}" for f in failures),
            }
        )
        return {"result": updated}

    return {}


# ─── Conditional routing edges ───────────────────────────────────────────────


def route_after_verify(state: GraphState) -> Literal["complete", "retry", "escalate"]:
    """After verify_node, decide where to go next.

    Mirrors the curator's internal-vs-escalate decision table:
      - result.status == done                  → complete
      - blocker is internally-resolvable AND retry_count == 0 → retry once
      - otherwise (non-resolvable, or already retried)        → escalate
    """
    if state.result is None or state.result.status == "done":
        return "complete"

    internally_resolvable = {
        "verification_failed",
        "tests_failed",
        "precommit_hook_failed",
        "merge_conflict",
        "time_budget_exceeded",
        "runner_silent_past_deadline",
    }

    if state.result.blocker in internally_resolvable and state.retry_count == 0:
        return "retry"
    return "escalate"


def increment_retry(state: GraphState) -> dict:
    """Pure node: bumps retry_count, clears the failed result so the runner re-runs."""
    return {
        "retry_count": state.retry_count + 1,
        "result": None,
    }


def complete_node(state: GraphState) -> dict:
    """Terminal success node — nothing to do but mark the spec done."""
    spec = state.spec
    if state.result is None:
        return {}
    updated = spec.model_copy(
        update={
            "status": "done",
            "completed_at": state.result.completed_at,
            "result_summary": state.result.notes
            or (f"PR: {state.result.pr_url}" if state.result.pr_url else "done"),
        }
    )
    return {"spec": updated}


def escalate_node(state: GraphState) -> dict:
    """Terminal escalation node — mark the spec blocked, leave the announce to the caller."""
    spec = state.spec
    if state.result is None:
        return {"error": "escalate_node called with no result"}
    updated = spec.model_copy(
        update={
            "status": "blocked",
            "completed_at": state.result.completed_at,
            "result_summary": f"escalated: {state.result.blocker or 'unknown'} - {state.result.notes or ''}",
        }
    )
    return {"spec": updated, "error": state.result.blocker}
