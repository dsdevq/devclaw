"""Planner — turns a single high-level goal into a DAG of OpenHands tasks.

Cognition runs in Claude (we shell out to ``claude --print``); this layer only
validates the JSON the model produces. Same split as the runner: mechanism
here, decisions in Claude. Auth comes from the bind-mounted ~/.claude session —
no API key, ever.

Single goals (the "small bounded" case) still go through here: the planner
returns a one-element list with no deps. One code path; less special-casing.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .state_store import TaskKind

PLANNER_TIMEOUT_MS = int(os.environ.get("DEVCLAW_PLANNER_TIMEOUT_MS", "90000"))
CLAUDE_BIN = os.environ.get("DEVCLAW_CLAUDE_BIN", "claude")
MAX_TASKS_PER_PLAN = 20

VALID_KINDS: tuple[TaskKind, ...] = ("implement_feature", "fix_bug", "review_repository")


@dataclass
class PlannedTask:
    #: stable model-assigned id used to express deps within this plan only
    key: str
    goal: str
    kind: TaskKind
    #: keys (not UUIDs) of other tasks in this plan that must finish first
    depends_on_keys: list[str] = field(default_factory=list)


class PlannerError(Exception):
    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


SYSTEM_PROMPT = """You are DevClaw's planner. Decompose a single coding goal
into a directed acyclic graph (DAG) of smaller tasks that can each be executed
by an autonomous coding agent in one run.

Rules:
- Each task is bounded: an agent should finish it in one session.
- Prefer fewer, larger tasks over many tiny ones. Aim for 1-6 tasks. Use more
  only when the goal is genuinely large.
- If the goal is small (e.g. "fix a typo", "add a config flag"), return ONE task.
- Use "depends_on" for tasks that genuinely cannot start until another finishes
  (e.g. "frontend uses the API contract from task 1"). Don't invent fake deps.
- Independent tasks should have empty depends_on so they can run in parallel.
- Task "kind" must be one of: implement_feature, fix_bug, review_repository.
  Default to implement_feature unless the goal explicitly says fix a bug or
  review code without changing it.

Respond with STRICT JSON ONLY - no prose, no markdown fences. Schema:

{
  "tasks": [
    {
      "key": "<short stable id, e.g. 't1', 'scaffold'>",
      "goal": "<concrete instruction for the agent>",
      "kind": "implement_feature" | "fix_bug" | "review_repository",
      "depends_on": ["<key of another task in this plan>", ...]
    }
  ]
}"""


def build_planner_prompt(goal: str, workspace_dir: str) -> str:
    return f"""{SYSTEM_PROMPT}

Workspace: {workspace_dir}
Goal: {goal}

Return the JSON now."""


def extract_json(text: str) -> str:
    """Pull the first JSON object out of a model response. Tolerates leading
    prose or markdown fences even though the prompt forbids them."""
    trimmed = text.strip()
    if trimmed.startswith("{"):
        return trimmed
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", trimmed)
    if fence and fence.group(1):
        return fence.group(1)
    first = trimmed.find("{")
    last = trimmed.rfind("}")
    if first >= 0 and last > first:
        return trimmed[first : last + 1]
    raise PlannerError("No JSON object found in planner response", text)


def validate_plan(parsed: object) -> list[PlannedTask]:
    """Validate the parsed plan and return tasks in topological order. Raises
    PlannerError on cycles, dangling refs, missing fields, etc."""
    if not isinstance(parsed, dict):
        raise PlannerError("Plan must be a JSON object")
    raw = parsed.get("tasks")
    if not isinstance(raw, list):
        raise PlannerError("Plan.tasks must be an array")
    if len(raw) == 0:
        raise PlannerError("Plan must contain at least one task")
    if len(raw) > MAX_TASKS_PER_PLAN:
        raise PlannerError(
            f"Plan has {len(raw)} tasks; max is {MAX_TASKS_PER_PLAN}. Refine the goal."
        )

    seen: set[str] = set()
    tasks: list[PlannedTask] = []
    for t in raw:
        if not isinstance(t, dict):
            raise PlannerError("Each task must be an object")
        key = t.get("key").strip() if isinstance(t.get("key"), str) else ""
        goal = t.get("goal").strip() if isinstance(t.get("goal"), str) else ""
        kind_raw = t.get("kind") if isinstance(t.get("kind"), str) else "implement_feature"
        deps_raw = t.get("depends_on")
        if not key:
            raise PlannerError("Task missing 'key'")
        if not goal:
            raise PlannerError(f"Task '{key}' missing 'goal'")
        if key in seen:
            raise PlannerError(f"Duplicate task key '{key}'")
        if kind_raw not in VALID_KINDS:
            raise PlannerError(
                f"Task '{key}' has invalid kind '{kind_raw}'; "
                f"expected one of {', '.join(VALID_KINDS)}"
            )
        depends_on_keys: list[str] = []
        if deps_raw is not None:
            if not isinstance(deps_raw, list):
                raise PlannerError(f"Task '{key}' depends_on must be an array")
            for d in deps_raw:
                if not isinstance(d, str) or not d.strip():
                    raise PlannerError(f"Task '{key}' has non-string dep")
                if d == key:
                    raise PlannerError(f"Task '{key}' depends on itself")
                depends_on_keys.append(d.strip())
        seen.add(key)
        tasks.append(PlannedTask(key=key, goal=goal, kind=kind_raw, depends_on_keys=depends_on_keys))

    # Validate all dep refs resolve.
    for t in tasks:
        for d in t.depends_on_keys:
            if d not in seen:
                raise PlannerError(f"Task '{t.key}' depends on unknown key '{d}'")

    # Kahn topological sort — also detects cycles.
    by_key = {t.key: t for t in tasks}
    indegree = {t.key: len(t.depends_on_keys) for t in tasks}
    dependents: dict[str, list[str]] = {}
    for t in tasks:
        for d in t.depends_on_keys:
            dependents.setdefault(d, []).append(t.key)

    ready = sorted(k for k, n in indegree.items() if n == 0)
    ordered: list[PlannedTask] = []
    while ready:
        k = ready.pop(0)
        ordered.append(by_key[k])
        for d in dependents.get(k, []):
            indegree[d] -= 1
            if indegree[d] == 0:
                ready.append(d)
        ready.sort()  # deterministic order across runs

    if len(ordered) != len(tasks):
        raise PlannerError("Plan contains a dependency cycle")
    return ordered


async def call_claude(prompt: str) -> str:
    """Spawn ``claude --print`` with the planner prompt and return its stdout.
    Injected into ``plan_goal`` so tests can stub the subprocess."""
    env = dict(os.environ)
    # Belt + suspenders: never let an API key override the OAuth session.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    try:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN,
            "--print",
            "--output-format=text",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise PlannerError(f"Failed to spawn {CLAUDE_BIN}: {exc}") from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=PLANNER_TIMEOUT_MS / 1000
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise PlannerError(f"claude --print timed out after {PLANNER_TIMEOUT_MS}ms")

    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise PlannerError(f"claude --print exited {proc.returncode}. stderr:\n{stderr}", stdout)
    return stdout


async def plan_goal(
    goal: str,
    workspace_dir: str,
    claude_caller: Callable[[str], Awaitable[str]] = call_claude,
) -> list[PlannedTask]:
    """Full planner entry point: prompt Claude, validate, return ordered DAG."""
    prompt = build_planner_prompt(goal, workspace_dir)
    raw = await claude_caller(prompt)
    json_text = extract_json(raw)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as err:
        raise PlannerError(f"Planner JSON parse failed: {err}", raw) from err
    return validate_plan(parsed)
