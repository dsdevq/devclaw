#!/usr/bin/env python3
"""Sandbox E2E — the scenario test suite.

A library of named scenarios that exercise every real path the chef supports —
single tasks, full goal lifecycles, scope-grill turns, blocked planners, steered
goals, failing gates, no-progress watchdogs, quota pauses, off-track done-gates.
Each scenario is a YAML fixture under ``evals/sandbox/scenarios/<id>.yaml`` and
declares its expected outcome; the runner drives the chef accordingly, captures
a structured trace, and pass/fail-evaluates the ``expect`` block.

Two cognition modes per scenario:
  * ``stub``   — canned responses keyed by role; free, deterministic, CI-runnable.
  * ``claude`` — real ``claude --print`` over Pro/Max OAuth; opt-in.

The scenarios DO NOT touch real infra: each run gets an isolated workspace
(a real local git repo with no remote), a fresh SQLite at /tmp-ish, and a fresh
goals dir. The default engine is the deterministic ``stub_engine`` (no docker,
no real PRs); a scenario can override engine behaviour via ``engine_responses``.

Usage:

  # One scenario:
  .venv/bin/python evals/sandbox_e2e.py --scenario blocked_planner
  .venv/bin/python evals/sandbox_e2e.py --scenario goal_existing_project --cognition claude

  # Compare to a previous run:
  .venv/bin/python evals/sandbox_e2e.py --scenario blocked_planner \\
      --baseline evals/runs/sandbox-2026-06-25T10-30-00

The full suite driver lives in ``evals/run_all.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def _add_repo_to_path() -> None:
    here = Path(__file__).resolve().parent.parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))


_add_repo_to_path()

from devclaw.cognition import set_cognition  # noqa: E402
from devclaw.loom.trace import Tracer, record_note, set_tracer  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = REPO_ROOT / "evals" / "sandbox" / "scenarios"
RUNS_ROOT = REPO_ROOT / "evals" / "runs"


# ---- scenario-aware cognition stub ----------------------------------------


class ScenarioCognition:
    """Cognition stub keyed by role with sequenced responses.

    ``responses`` maps a role name to either a single string (returned every
    call for that role) or a list of strings (consumed in order; falls back
    to ``default`` once exhausted). A response string starting with
    ``RAISE:`` raises ``PlannerError(<rest>)`` — used by ``quota_pause`` to
    simulate a usage-limit response without hitting the real provider."""

    def __init__(self, responses: Optional[dict[str, Any]] = None, default: str = "{}") -> None:
        self.responses: dict[str, list[str]] = {}
        for role, val in (responses or {}).items():
            if isinstance(val, list):
                self.responses[role] = list(val)
            else:
                self.responses[role] = [str(val)]
        self.default = default
        self.calls: list[tuple[str, str, str]] = []

    async def __call__(
        self, prompt: str, *, role: str = "unknown", model: Optional[str] = None,
    ) -> str:
        from devclaw.loom import trace as _trace
        from devclaw.planner import PlannerError

        queue = self.responses.get(role)
        if queue:
            # If only one response was supplied (single-string form), don't
            # consume — the scenario expects that role to always return it.
            if len(queue) > 1:
                response = queue.pop(0)
            else:
                response = queue[0]
        else:
            response = self.default

        self.calls.append((role, model or "", prompt))
        if response.startswith("RAISE:"):
            err = response[len("RAISE:"):].strip() or "simulated failure"
            _trace.record_cognition(
                role=role, model=model or "(stub)", prompt=prompt,
                response="", latency_ms=0, error=err,
            )
            raise PlannerError(err)

        _trace.record_cognition(
            role=role, model=model or "(stub)", prompt=prompt,
            response=response, latency_ms=0,
        )
        return response


# ---- scenario parsing ------------------------------------------------------


@dataclass
class Scenario:
    id: str
    description: str = ""
    mode: str = "goal"  # goal | mcp | grill
    cognition_responses: dict[str, Any] = field(default_factory=dict)
    engine_responses: dict[str, Any] = field(default_factory=dict)
    goal: dict[str, Any] = field(default_factory=dict)
    mcp_call: dict[str, Any] = field(default_factory=dict)
    grill: dict[str, Any] = field(default_factory=dict)
    ticks: int = 3
    advance_clock_s: int = 0
    steering: list[dict] = field(default_factory=list)
    expect: dict[str, Any] = field(default_factory=dict)


def _load_scenario(slug: str) -> Scenario:
    import yaml

    path = SCENARIOS_DIR / f"{slug}.yaml"
    if not path.is_file():
        raise SystemExit(f"unknown scenario: {slug} (looked at {path})")
    raw = yaml.safe_load(path.read_text()) or {}
    return Scenario(
        id=raw.get("id", slug),
        description=raw.get("description", ""),
        mode=raw.get("mode", "goal"),
        cognition_responses=raw.get("cognition", {}).get("stub_responses", {}),
        engine_responses=raw.get("engine_responses", {}),
        goal=raw.get("setup", {}).get("goal", {}),
        mcp_call=raw.get("setup", {}).get("mcp_call", {}),
        grill=raw.get("setup", {}).get("grill", {}),
        ticks=int(raw.get("ticks", 3)),
        advance_clock_s=int(raw.get("advance_clock_s", 0)),
        steering=raw.get("steering", []) or [],
        expect=raw.get("expect", {}) or {},
    )


# ---- environment setup ----------------------------------------------------


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _init_empty_git_workspace(workspace_dir: Path) -> None:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if (workspace_dir / ".git").is_dir():
        return
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace_dir)], check=True)
    (workspace_dir / "README.md").write_text("# sandbox workspace\n")
    subprocess.run(["git", "-C", str(workspace_dir), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(workspace_dir),
         "-c", "user.email=sandbox@devclaw.local",
         "-c", "user.name=sandbox",
         "commit", "-q", "-m", "init"], check=True,
    )


def _seed_goal_yaml(goals_dir: Path, goal_id: str, spec: dict, workspace_dir: Path) -> None:
    import yaml
    d = goals_dir / goal_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "goal.yaml").write_text(
        yaml.safe_dump(
            {
                "objective": spec.get("objective", "demo").strip(),
                "cadence": spec.get("cadence", "1d"),
                "engine": spec.get("engine", "devclaw"),
                "workspace_dir": str(workspace_dir),
                "repo_url": spec.get("repo_url"),
                "verify_cmd": spec.get("verify_cmd"),
                "open_pr": bool(spec.get("open_pr", False)),
                "done_when": spec.get("done_when", "").strip(),
                "backlog": [str(b).strip() for b in (spec.get("backlog") or [])],
            },
            sort_keys=False,
        )
    )


def _snapshot_goal_artifacts(goals_dir: Path, goal_id: str, out_dir: Path) -> None:
    src = goals_dir / goal_id
    if not src.is_dir():
        return
    dest = out_dir / "goal-state"
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("goal.yaml", "STATUS.md", "log.md", "deliveries.md", "discovery.md", "spec.md", "inbox.md"):
        path = src / name
        if path.is_file():
            shutil.copy2(path, dest / name)


# ---- expect-block evaluation ----------------------------------------------


def _evaluate_expect(expect: dict, tracer: Tracer, goal_dir: Optional[Path], extra: dict) -> list[str]:
    """Return a list of human-readable failure messages. Empty list = pass."""
    failures: list[str] = []

    def _get_status() -> dict:
        if goal_dir is None:
            return {}
        status_path = goal_dir / "STATUS.md"
        if not status_path.is_file():
            return {}
        import re
        import yaml
        text = status_path.read_text()
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
        return yaml.safe_load(m.group(1)) if m else {}

    status = _get_status()

    def _log_text() -> str:
        if goal_dir is None:
            return ""
        p = goal_dir / "log.md"
        return p.read_text() if p.is_file() else ""

    # ---- final goal state predicates ----
    if "final_phase" in expect:
        actual = status.get("phase")
        if actual != expect["final_phase"]:
            failures.append(f"final_phase: expected {expect['final_phase']!r}, got {actual!r}")
    if "final_lifecycle" in expect:
        actual = status.get("lifecycle")
        if actual != expect["final_lifecycle"]:
            failures.append(f"final_lifecycle: expected {expect['final_lifecycle']!r}, got {actual!r}")
    if "blocked_on_contains" in expect:
        blocked_on = (status.get("blocked_on") or "")
        if expect["blocked_on_contains"] not in blocked_on:
            failures.append(f"blocked_on_contains: {expect['blocked_on_contains']!r} not in {blocked_on!r}")

    # ---- log / deliveries / discovery / spec predicates ----
    if "log_contains" in expect:
        log = _log_text()
        for needle in expect["log_contains"]:
            if needle not in log:
                failures.append(f"log_contains: {needle!r} not found in log.md")

    # ---- trace counts ----
    counts_eq = expect.get("counts_eq") or {}
    by_role = tracer.cognition_by_role()
    derived = {
        "ticks": len(tracer.by_kind("tick")),
        "cognition_calls": len(tracer.by_kind("cognition")),
        "dispatches": len(tracer.by_kind("dispatch")),
        "deliveries": len(tracer.by_kind("delivery")),
        "notifications": len(tracer.by_kind("notify")),
    }
    for key, want in counts_eq.items():
        got = derived.get(key, 0)
        if got != want:
            failures.append(f"counts_eq[{key}]: expected {want}, got {got}")

    counts_min = expect.get("counts_min") or {}
    for key, want in counts_min.items():
        got = derived.get(key, 0)
        if got < want:
            failures.append(f"counts_min[{key}]: expected ≥{want}, got {got}")

    role_eq = expect.get("cognition_by_role_eq") or {}
    for role, want in role_eq.items():
        got = by_role.get(role, 0)
        if got != want:
            failures.append(f"cognition_by_role_eq[{role}]: expected {want}, got {got}")

    role_min = expect.get("cognition_by_role_min") or {}
    for role, want in role_min.items():
        got = by_role.get(role, 0)
        if got < want:
            failures.append(f"cognition_by_role_min[{role}]: expected ≥{want}, got {got}")

    # ---- terminal outcome ----
    ticks = tracer.by_kind("tick")
    if ticks:
        final_outcome = ticks[-1]["outcome"]
        if "final_outcome" in expect and final_outcome != expect["final_outcome"]:
            failures.append(f"final_outcome: expected {expect['final_outcome']!r}, got {final_outcome!r}")
        outcomes_contain = expect.get("outcomes_contain") or []
        seen = {t["outcome"] for t in ticks}
        for want in outcomes_contain:
            if want not in seen:
                failures.append(f"outcomes_contain: {want!r} not in {sorted(seen)}")

    # ---- notification predicates ----
    notify_owner_contains = expect.get("notify_owner_contains") or []
    owner_msgs = [e.get("text", "") for e in tracer.by_kind("notify") if e.get("level") == "OWNER"]
    owner_blob = " · ".join(owner_msgs)
    for needle in notify_owner_contains:
        if needle not in owner_blob:
            failures.append(f"notify_owner_contains: {needle!r} not found in owner notifications")

    # ---- mode-specific extras (mcp_result, grill_result) ----
    for key, want in (expect.get("mcp_result_contains") or {}).items():
        got = extra.get("mcp_result", {}).get(key)
        if want not in str(got):
            failures.append(f"mcp_result_contains[{key}]: {want!r} not in {got!r}")

    if "grill_final_action" in expect:
        got = (extra.get("grill_final") or {}).get("action")
        if got != expect["grill_final_action"]:
            failures.append(f"grill_final_action: expected {expect['grill_final_action']!r}, got {got!r}")
    if "grill_questions_min" in expect:
        got = len(extra.get("grill_questions") or [])
        if got < expect["grill_questions_min"]:
            failures.append(f"grill_questions_min: expected ≥{expect['grill_questions_min']}, got {got}")
    if "grill_questions_eq" in expect:
        got = len(extra.get("grill_questions") or [])
        if got != expect["grill_questions_eq"]:
            failures.append(f"grill_questions_eq: expected {expect['grill_questions_eq']}, got {got}")

    return failures


# ---- environment wiring ---------------------------------------------------


def _wire_env(goals_dir: Path) -> None:
    os.environ["DEVCLAW_ENGINE"] = "stub"
    os.environ["DEVCLAW_GOALS_DIR"] = str(goals_dir)
    os.environ.setdefault("DEVCLAW_GOAL_PLAIN_SUMMARY", "0")  # skip the summary call for clean traces
    # A scenario reaching 'achieved' would otherwise try a real Tailscale deploy
    # via deploy.deploy_project — docker, port allocation, the works. Off in sandbox.
    os.environ.setdefault("DEVCLAW_GOAL_AUTODEPLOY", "0")


def _patch_for_sandbox() -> None:
    """Patch prepare_workspace + the goal-store clock so the sandbox is fully
    self-contained AND deterministic (timestamps don't drift across runs)."""
    import devclaw.engine.workspace as _ws_mod

    async def _sandbox_prep(ws_dir: str, repo_url: "str | None" = None) -> str:
        return "main"
    _ws_mod.prepare_workspace = _sandbox_prep
    for mod_name in ("devclaw.goal.tick", "devclaw.goal.service"):
        if mod_name in sys.modules:
            mod = sys.modules[mod_name]
            if hasattr(mod, "prepare_workspace"):
                mod.prepare_workspace = _sandbox_prep

    _FROZEN_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc)
    import devclaw.goal.store as _store_mod
    _store_mod._default_now = lambda: _FROZEN_NOW


# ---- mode dispatch --------------------------------------------------------


async def _run_goal_mode(scenario: Scenario, env: dict) -> dict:
    """Drive a goal-lifecycle scenario."""
    _patch_for_sandbox()

    from devclaw.goal.models import GoalStatus
    from devclaw.goal.service import GoalConfig, GoalService
    from devclaw.goal.store import GoalStore
    from devclaw.state_store import StateStore
    from devclaw.task_queue import TaskQueue

    goal_id = scenario.id
    goals_dir = env["goals_dir"]
    workspace_dir = env["workspace_dir"]
    state_db = env["state_db"]

    _seed_goal_yaml(goals_dir, goal_id, scenario.goal, workspace_dir)
    lifecycle_start = scenario.goal.get("lifecycle_start", "investigating")
    GoalStore(goals_dir).save_status(goal_id, GoalStatus(lifecycle=lifecycle_start))

    if scenario.goal.get("spec"):
        GoalStore(goals_dir).write_spec(goal_id, scenario.goal["spec"])

    store = StateStore(str(state_db))
    # CRITICAL: wire the stub engine into the queue EXPLICITLY. The TaskQueue
    # default is run_sandcastle — real docker — and DEVCLAW_ENGINE=stub is only
    # honoured by server/_state.py, which the sandbox bypasses. A wrong default
    # here silently runs scenarios against the production engine; the sanity
    # check below also fails the run if the resolved engine isn't 'stub'.
    from devclaw.engine.stub import stub_engine, stub_goal_planner
    queue = TaskQueue(store, planner=stub_goal_planner, runner=stub_engine)
    if queue.engine_kind != "stub":
        raise SystemExit(
            f"sandbox runner refused to start: queue.engine_kind={queue.engine_kind!r}, "
            "expected 'stub'. The TaskQueue was constructed with a non-stub runner — "
            "this would silently exercise the production engine."
        )
    record_note(f"engine: {queue.engine_kind}")

    cfg = GoalConfig(
        goals_dir=goals_dir, notify_url="",
        tick_seconds=900, eval_every=3, verify_done=False,
    )
    svc = GoalService(queue, store, cfg)

    errors: list[str] = []
    try:
        for i in range(scenario.ticks):
            record_note(f"tick {i + 1}/{scenario.ticks}")
            # Apply per-tick steering if scheduled.
            for s in scenario.steering:
                if s.get("before_tick") == i + 1:
                    record_note(f"steering: {s['message']!r}")
                    svc.steer_goal(goal_id, s["message"])
            try:
                outcome = await svc.tick_one(goal_id)
            except Exception as exc:  # noqa: BLE001
                msg = f"tick {i + 1} raised: {type(exc).__name__}: {exc}"
                errors.append(msg)
                record_note(msg)
                break
            record_note(f"outcome: {outcome}")
            await queue.drain()
            if outcome in ("done", "skip_done", "skip_cancelled"):
                record_note("terminal — stopping early")
                break

        # Optionally advance the no-progress watchdog by mutating the goal's
        # last_progress_at into the past, then run one more tick to fire it.
        if scenario.advance_clock_s > 0:
            from dataclasses import replace
            from datetime import timedelta
            gs = GoalStore(goals_dir)
            st = gs.load_status(goal_id)
            past = datetime(2026, 6, 25, 12, 0, 0, tzinfo=timezone.utc) - timedelta(seconds=scenario.advance_clock_s)
            gs.save_status(goal_id, replace(st, last_progress_at=past.isoformat(timespec="seconds")))
            record_note(f"advanced last_progress_at by {scenario.advance_clock_s}s → re-ticking")
            await svc.tick_one(goal_id)
            await queue.drain()
    finally:
        await queue.drain()
        store.close()

    return {"errors": errors, "goal_id": goal_id}


async def _run_mcp_mode(scenario: Scenario, env: dict) -> dict:
    """Drive an MCP-direct scenario (no goal layer). Calls the named tool with
    its args and captures the result."""
    _patch_for_sandbox()

    # Initialize the server's MCP module so the tools are registered.
    from devclaw import server as _server_mod  # noqa: F401
    # Pick the tool by name from devclaw.server.tools
    from devclaw.server import tools as _tools

    call = scenario.mcp_call
    tool_name = call["tool"]
    args = dict(call.get("args") or {})
    # If workspace_dir wasn't supplied, point at the sandbox workspace.
    args.setdefault("workspace_dir", str(env["workspace_dir"]))

    fn = getattr(_tools, tool_name, None)
    if fn is None or not callable(fn):
        return {"errors": [f"unknown MCP tool: {tool_name}"], "mcp_result": None}
    record_note(f"calling MCP tool: {tool_name}({json.dumps(args)})")
    try:
        # The decorated tool isn't directly callable in all FastMCP versions;
        # call the underlying coroutine if needed.
        underlying = getattr(fn, "__wrapped__", fn)
        if hasattr(fn, "fn"):
            underlying = fn.fn
        raw = await underlying(**args)
    except Exception as exc:  # noqa: BLE001
        return {"errors": [f"mcp call raised: {type(exc).__name__}: {exc}"], "mcp_result": None}
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        parsed = {"raw": str(raw)}
    record_note(f"mcp result: {json.dumps(parsed)[:200]}")

    # Let any in-flight queue work settle so the trace captures it.
    queue = getattr(_server_mod, "queue", None)
    if queue is not None and hasattr(queue, "drain"):
        try:
            await queue.drain()
        except Exception:  # noqa: BLE001
            pass

    return {"errors": [], "mcp_result": parsed}


async def _run_grill_mode(scenario: Scenario, env: dict) -> dict:
    """Drive a scope_grill scenario turn by turn. The scenario supplies the
    idea + a list of user answers; the runner feeds each answer back into the
    transcript and calls scope_grill again until it returns ``done`` (or the
    answer list is exhausted)."""
    from devclaw.elicitation import next_step

    idea = scenario.grill.get("idea", "demo")
    answers = list(scenario.grill.get("user_answers", []))
    transcript: list[dict] = []
    questions: list[str] = []
    final: dict | None = None
    errors: list[str] = []

    for i in range(len(answers) + 1):
        record_note(f"grill turn {i + 1}")
        try:
            step = await next_step(idea, transcript)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"grill turn {i + 1} raised: {type(exc).__name__}: {exc}")
            break
        if step.get("action") == "ask":
            q = step.get("question", "")
            questions.append(q)
            record_note(f"ask: {q[:120]}")
            if not answers:
                final = step
                break
            answer = answers.pop(0)
            transcript.append({
                "question": q,
                "recommended": step.get("recommended", ""),
                "answer": answer,
            })
            record_note(f"answer: {answer[:120]}")
        else:  # done
            final = step
            record_note("grill: done")
            break

    return {
        "errors": errors,
        "grill_final": final,
        "grill_questions": questions,
        "grill_transcript_len": len(transcript),
    }


# ---- top-level runner -----------------------------------------------------


async def _run(scenario: Scenario, cognition_mode: str, out_dir: Path) -> dict:
    goals_dir = out_dir / "goals"
    workspace_dir = out_dir / "workspace"
    state_db = out_dir / "state.db"
    goals_dir.mkdir(parents=True, exist_ok=True)
    _init_empty_git_workspace(workspace_dir)

    _wire_env(goals_dir)

    # cognition: stub by default, real claude if requested
    if cognition_mode == "stub":
        cog = ScenarioCognition(responses=scenario.cognition_responses, default="{}")
        set_cognition(cog)
        record_note(f"cognition: STUB (scenario={scenario.id})")
    else:
        set_cognition(None)  # let env-default rebuild → ClaudeCognition
        record_note(f"cognition: CLAUDE (scenario={scenario.id})")

    env = {"goals_dir": goals_dir, "workspace_dir": workspace_dir, "state_db": state_db}
    tracer = Tracer(label=f"scenario:{scenario.id}")
    set_tracer(tracer)
    extra: dict[str, Any] = {}
    errors: list[str] = []

    try:
        if scenario.mode == "goal":
            out = await _run_goal_mode(scenario, env)
            errors = out.get("errors", [])
            extra["goal_id"] = out.get("goal_id")
        elif scenario.mode == "mcp":
            out = await _run_mcp_mode(scenario, env)
            errors = out.get("errors", [])
            extra["mcp_result"] = out.get("mcp_result")
        elif scenario.mode == "grill":
            out = await _run_grill_mode(scenario, env)
            errors = out.get("errors", [])
            extra["grill_final"] = out.get("grill_final")
            extra["grill_questions"] = out.get("grill_questions")
        else:
            errors.append(f"unknown mode: {scenario.mode}")
    finally:
        set_tracer(None)
        set_cognition(None)

    # Persist artifacts.
    tracer.dump_json(out_dir / "trace.json")
    tracer.dump_timeline(out_dir / "timeline.md")

    goal_dir = (goals_dir / extra["goal_id"]) if extra.get("goal_id") else None
    if extra.get("goal_id"):
        _snapshot_goal_artifacts(goals_dir, extra["goal_id"], out_dir)

    failures = _evaluate_expect(scenario.expect, tracer, goal_dir, extra)
    summary = {
        "scenario": scenario.id,
        "description": scenario.description,
        "cognition_mode": cognition_mode,
        "started_at": tracer.started_at,
        "errors": errors,
        "expect_failures": failures,
        "passed": (not errors) and (not failures),
        "outcomes": [t["outcome"] for t in tracer.by_kind("tick")],
        "cognition_calls": len(tracer.by_kind("cognition")),
        "cognition_by_role": tracer.cognition_by_role(),
        "dispatches": len(tracer.by_kind("dispatch")),
        "deliveries": len(tracer.by_kind("delivery")),
        "notifications": len(tracer.by_kind("notify")),
        "mcp_result": extra.get("mcp_result"),
        "grill_final": extra.get("grill_final"),
        "grill_questions_count": len(extra.get("grill_questions") or []),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Sandbox E2E scenario runner.")
    ap.add_argument("--scenario", required=True, help="slug under evals/sandbox/scenarios/")
    ap.add_argument("--cognition", choices=("claude", "stub"), default="stub",
                    help="cognition backend (default: stub)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    scenario = _load_scenario(args.scenario)
    out_dir = args.out or (RUNS_ROOT / f"sandbox-{scenario.id}-{_now_slug()}")
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = asyncio.run(_run(scenario, args.cognition, out_dir))

    headline = {
        "scenario": summary["scenario"],
        "passed": summary["passed"],
        "errors": summary["errors"],
        "expect_failures": summary["expect_failures"],
        "cognition_calls": summary["cognition_calls"],
        "cognition_by_role": summary["cognition_by_role"],
        "dispatches": summary["dispatches"],
        "deliveries": summary["deliveries"],
        "out": str(out_dir),
    }
    print(json.dumps(headline, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
