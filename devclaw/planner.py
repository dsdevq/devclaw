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
import sys
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .loom import trace as _trace
from .state_store import TaskKind

#: default ceiling for any cognition call when the caller doesn't supply its
#: own. Each role's ``default_caller`` may pass a larger value via
#: :func:`claude_with_model` when its expected output volume warrants — the
#: decomposer is the canonical example (opus generating multi-KB YAML
#: routinely exceeds 90s).
PLANNER_TIMEOUT_MS = 90_000
CLAUDE_BIN = os.environ.get("DEVCLAW_CLAUDE_BIN", "claude")
MAX_TASKS_PER_PLAN = 20

# Per-role model tiering. Running every cognition call on the account default
# (Opus) burns the Pro/Max quota fast and is slow; tier each role to the lightest
# model that does its job. These are `claude --model` values (an alias like
# 'sonnet'/'opus', or a full id). Planning is rare + high-leverage → Opus; the
# scope grill is conversational → Sonnet (set in elicitation.py); the eval judge
# is bounded classification → Haiku (set in eval_judge.py). The heavy coding path
# (OpenHands) is tiered separately via DEVCLAW_EXEC_MODEL. An empty value →
# the CLI's account default (no --model flag passed).
from .model_tiers import model_for as _model_for
PLANNER_MODEL = _model_for("planner")

VALID_KINDS: tuple[TaskKind, ...] = ("implement_feature", "fix_bug", "review_repository")


@dataclass
class PlannedTask:
    #: stable model-assigned id used to express deps within this plan only
    key: str
    goal: str
    kind: TaskKind
    #: keys (not UUIDs) of other tasks in this plan that must finish first
    depends_on_keys: list[str] = field(default_factory=list)
    #: the spec milestone this task serves (plan-from-spec only; else None)
    milestone: str | None = None


class PlannerError(Exception):
    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


def build_planner_prompt(goal: str, workspace_dir: str) -> str:
    from .prompts import load_prompt

    return (
        f"{load_prompt('plan-goal')}\n\n"
        f"Workspace: {workspace_dir}\n"
        f"Goal: {goal}\n\n"
        "Return the JSON now."
    )


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
        milestone = t.get("milestone").strip() if isinstance(t.get("milestone"), str) else None
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
        tasks.append(
            PlannedTask(
                key=key,
                goal=goal,
                kind=kind_raw,
                depends_on_keys=depends_on_keys,
                milestone=milestone or None,
            )
        )

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


def _build_claude_argv(prompt: str, model: str | None) -> list[str]:  # noqa: ARG001
    """Argv for a ``claude --print`` call. ``--model`` is inserted only when a
    model is given (else the CLI uses the account default). Pure → unit-tested.

    ``--output-format=json`` (T0.5, 2026-07-10): the CLI wraps the response in
    a result envelope carrying REAL token usage + cost, which the trace records
    instead of the old len/4 guesses. :func:`parse_cli_envelope` unwraps it;
    anything that doesn't parse as the envelope falls back to raw-stdout
    behavior, so callers still just receive the response text.

    The ``prompt`` parameter is kept for backwards-compat with the pre-2026-07-03
    signature but is NO LONGER appended to argv — it now rides on stdin (see
    :func:`call_claude`) to avoid ``[Errno 7] Argument list too long`` when the
    goal-planner's prompt (log + deliveries + steering) crosses the OS ARG_MAX
    limit (~128 KB on Linux). Live-hit closeloop-mission-v2 2026-07-03T18:35Z."""
    argv = [CLAUDE_BIN, "--print", "--output-format=json"]
    if model:
        argv += ["--model", model]
    return argv


@dataclass(frozen=True)
class CliEnvelope:
    """The ``claude --print --output-format json`` result envelope — only the
    fields devclaw consumes. Shape verified empirically 2026-07-10 against the
    live CLI; a scrubbed capture is committed at
    ``tests/fixtures/claude_print_json_envelope.json``::

        {"type": "result", "subtype": "success", "is_error": false,
         "result": "<text>", "total_cost_usd": 0.0188989, "duration_ms": 2298,
         "usage": {"input_tokens": 10, "output_tokens": 38,
                   "cache_read_input_tokens": 17209,
                   "cache_creation_input_tokens": 8201, ...}, ...}
    """

    result_text: str = ""
    subtype: str = ""
    is_error: bool = False
    #: human-readable failure wording for error envelopes. The quota guard
    #: (loom.limits.classify_failure) regexes PlannerError messages, so this
    #: MUST carry the CLI's raw wording verbatim.
    error_text: str = ""
    tokens_in: int | None = None
    tokens_out: int | None = None
    cache_read: int | None = None
    cache_creation: int | None = None
    cost_usd: float | None = None


def _usage_int(usage: dict, key: str) -> int | None:
    v = usage.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return int(v)


def parse_cli_envelope(stdout: str) -> CliEnvelope | None:
    """Parse CLI stdout as the ``--output-format json`` result envelope.

    Returns ``None`` whenever stdout is NOT the envelope — not JSON, not a
    dict, missing ``type == "result"``, or a "success" without a string
    ``result``. The caller then falls back to treating raw stdout as the
    response text, exactly like the pre-json-mode behavior. Deliberately
    defensive: a CLI version drift must degrade to the old behavior, never
    crash cognition."""
    try:
        parsed = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict) or parsed.get("type") != "result":
        return None
    subtype = str(parsed.get("subtype") or "")
    is_error = bool(parsed.get("is_error")) or subtype != "success"
    result = parsed.get("result")
    if is_error:
        # Prefer the CLI's own wording; fall back to the whole envelope so no
        # failure text (quota wording!) is ever lost to the classifier.
        if isinstance(result, str) and result.strip():
            error_text = result
        elif isinstance(parsed.get("error"), str) and parsed["error"].strip():
            error_text = parsed["error"]
        else:
            error_text = json.dumps(parsed, default=str)
    else:
        if not isinstance(result, str):
            return None  # envelope-shaped but no usable text → raw fallback
        error_text = ""
    usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
    cost = parsed.get("total_cost_usd")
    return CliEnvelope(
        result_text=result if isinstance(result, str) else "",
        subtype=subtype,
        is_error=is_error,
        error_text=error_text,
        tokens_in=_usage_int(usage, "input_tokens"),
        tokens_out=_usage_int(usage, "output_tokens"),
        cache_read=_usage_int(usage, "cache_read_input_tokens"),
        cache_creation=_usage_int(usage, "cache_creation_input_tokens"),
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
    )


async def call_claude(
    prompt: str,
    model: str | None = None,
    *,
    role: str = "unknown",
    timeout_ms: int | None = None,
) -> str:
    """Spawn ``claude --print --output-format json`` with the prompt and return
    the response TEXT (the envelope's ``result`` field — callers parse their own
    YAML/JSON out of it, contract unchanged; stdout that doesn't parse as the
    envelope is returned raw, as before json mode). Real token usage + cost from
    the envelope flow into the cognition trace. ``model``
    picks the tier (alias or full id); None → account default. ``role`` labels
    the cognition site (planner / evaluator / grill / judge / summary / review /
    research) for the trace recorder. ``timeout_ms`` overrides
    :data:`PLANNER_TIMEOUT_MS` for this call — roles whose output volume warrants
    a larger budget (decomposer) pass their own value. Injected into cognition
    roles so tests can stub the subprocess; each role binds its own
    model+role+timeout via :func:`claude_with_model`."""
    effective_timeout_ms = timeout_ms if timeout_ms is not None else PLANNER_TIMEOUT_MS
    env = dict(os.environ)
    # Belt + suspenders: never let an API key override the OAuth session.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)

    argv = _build_claude_argv(prompt, model)
    argv_head = f"{CLAUDE_BIN} --print" + (f" --model {model}" if model else "")
    started = _trace.now_ms()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            # 2026-07-03 argv → stdin migration: the goal-planner's prompt (log
            # + deliveries + steering) crossed ARG_MAX on closeloop-mission-v2
            # after ~20 dispatches, and every subsequent plan attempt hit
            # ``[Errno 7] Argument list too long``. The prompt now rides on
            # stdin instead. ``claude --print`` reads the whole stdin as the
            # prompt when argv doesn't provide one; closing stdin after write
            # (via ``communicate(input=)``) avoids the "no stdin data received
            # in 3s" warning the old ``stdin=DEVNULL`` path was working around.
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        latency = _trace.now_ms() - started
        _trace.record_cognition(
            role=role, model=model or "", prompt=prompt, response="",
            latency_ms=latency, error=f"spawn failed: {exc}",
        )
        _trace.record_subprocess(
            cmd="claude --print", argv_head=argv_head, latency_ms=latency,
            exit_code=None, error=f"spawn failed: {exc}",
        )
        raise PlannerError(f"Failed to spawn {CLAUDE_BIN}: {exc}") from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(input=prompt.encode("utf-8")),
            timeout=effective_timeout_ms / 1000,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        latency = _trace.now_ms() - started
        _trace.record_cognition(
            role=role, model=model or "", prompt=prompt, response="",
            latency_ms=latency, error="timeout",
        )
        _trace.record_subprocess(
            cmd="claude --print", argv_head=argv_head, latency_ms=latency,
            exit_code=None, error="timeout",
        )
        raise PlannerError(f"claude --print timed out after {effective_timeout_ms}ms")

    stdout = stdout_b.decode("utf-8", "replace")
    stderr = stderr_b.decode("utf-8", "replace")
    latency = _trace.now_ms() - started
    envelope = parse_cli_envelope(stdout)
    _trace.record_subprocess(
        cmd="claude --print", argv_head=argv_head, latency_ms=latency,
        exit_code=proc.returncode, error=(stderr[:200] if proc.returncode != 0 else ""),
    )
    if proc.returncode != 0:
        # CRITICAL INVARIANT — the quota guard. A Claude usage-limit ("You're
        # out of extra usage · resets 10pm (UTC)") comes back on STDOUT with an
        # EMPTY stderr, and the entire pause machinery keys off
        # ``classify_failure`` regexing this PlannerError's message. Whatever
        # json mode does with that wording — wraps it in an error envelope, or
        # prints it as plain non-JSON text — the raw wording MUST land verbatim
        # in the message: envelope error text when it parses, raw stdout tail
        # when it doesn't.
        detail = (
            envelope.error_text
            if envelope is not None and envelope.is_error
            else stdout[-500:]
        )
        _trace.record_cognition(
            role=role, model=model or "", prompt=prompt, response=stdout,
            latency_ms=latency, error=f"exit={proc.returncode}; stderr={stderr[:200]}",
        )
        raise PlannerError(
            f"claude --print exited {proc.returncode}. stderr:\n{stderr}\n"
            f"stdout:\n{detail}",
            stdout,
        )
    if envelope is None:
        # Not the JSON envelope (CLI version drift, plain-text output). Degrade
        # to the pre-json-mode behavior: raw stdout IS the response text. One
        # breadcrumb on stderr so the drift is visible without breaking cognition.
        sys.stderr.write(
            "devclaw: claude --print stdout did not parse as a JSON result "
            "envelope; treating raw stdout as the response text\n"
        )
        _trace.record_cognition(
            role=role, model=model or "", prompt=prompt, response=stdout, latency_ms=latency,
        )
        return stdout
    if envelope.is_error:
        # Exit 0 but the envelope reports an error. Same invariant as above:
        # the envelope's wording flows verbatim into the message for the
        # classifier (quota/rate-limit wording must survive).
        _trace.record_cognition(
            role=role, model=model or "", prompt=prompt, response=stdout,
            latency_ms=latency,
            error=f"cli error envelope ({envelope.subtype or 'unknown'}): "
                  f"{envelope.error_text[:200]}",
        )
        raise PlannerError(
            f"claude --print returned an error envelope "
            f"(subtype={envelope.subtype or 'unknown'}): {envelope.error_text}",
            stdout,
        )
    _trace.record_cognition(
        role=role, model=model or "", prompt=prompt,
        response=envelope.result_text, latency_ms=latency,
        tokens_in=envelope.tokens_in, tokens_out=envelope.tokens_out,
        cache_read=envelope.cache_read, cache_creation=envelope.cache_creation,
        cost_usd=envelope.cost_usd,
    )
    return envelope.result_text


def claude_with_model(
    model: str | None,
    *,
    role: str = "unknown",
    timeout_ms: int | None = None,
) -> Callable[[str], Awaitable[str]]:
    """A one-argument cognition caller bound to a model + role label. Routes
    through the configured :class:`~devclaw.cognition.Cognition` (claude by
    default; ``DEVCLAW_COGNITION=stub`` for offline harnesses). ``timeout_ms``
    overrides the default ceiling for this role — pass it when the role's
    expected output volume routinely exceeds the global default (decomposer).
    Backend-swap happens at the cognition seam — this factory keeps its
    historical name + signature so existing callers stay untouched."""
    from .cognition import bind

    return bind(model, role=role, timeout_ms=timeout_ms)


#: planning (plan_goal + plan_spec) runs at the planner tier
_planner_caller = claude_with_model(PLANNER_MODEL, role="planner")


def _parse_plan(raw: str) -> list[PlannedTask]:
    """Extract → parse → validate a planner response into an ordered DAG."""
    json_text = extract_json(raw)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as err:
        raise PlannerError(f"Planner JSON parse failed: {err}", raw) from err
    return validate_plan(parsed)


async def plan_goal(
    goal: str,
    workspace_dir: str,
    claude_caller: Callable[[str], Awaitable[str]] = _planner_caller,
) -> list[PlannedTask]:
    """Plan a bare goal string (the small-bounded `start_program` case)."""
    raw = await claude_caller(build_planner_prompt(goal, workspace_dir))
    return _parse_plan(raw)


# ===== plan-from-spec ========================================================
# The build-a-project-from-scratch path: decompose an *approved spec* (the
# shared scope contract handed in by the OpenClaw waiter after scope_grill) into
# a milestone-ordered DAG. Richer than plan_goal — the model is grounded in the
# spec's milestones, acceptance criteria, scope, and constraints.

def build_spec_planner_prompt(spec: str, workspace_dir: str) -> str:
    from .prompts import load_prompt

    return (
        f"{load_prompt('plan-spec')}\n\n"
        f"Workspace: {workspace_dir}\n\n"
        f"APPROVED SPEC:\n{spec}\n\n"
        "Return the JSON now."
    )


async def plan_spec(
    spec: str,
    workspace_dir: str,
    claude_caller: Callable[[str], Awaitable[str]] = _planner_caller,
) -> list[PlannedTask]:
    """Decompose an approved spec into a milestone-ordered DAG. Same validated
    DAG shape as plan_goal, with per-task milestones populated."""
    raw = await claude_caller(build_spec_planner_prompt(spec, workspace_dir))
    return _parse_plan(raw)
