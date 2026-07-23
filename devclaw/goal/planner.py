"""The next-action planner — the goal layer's first cognition call.

Folded in from goalclaw. Fired ONLY when the cheap check found real work. Same
mechanism/cognition split as devclaw's DAG planner: Claude decides, Python
validates the JSON. It decides the *next action* toward a goal — it does NOT
decompose code (that's start_program) and it does NOT judge direction (that's
goal_evaluator). Light reasoning → the sonnet tier by default.

Cognition reuses devclaw's own ``claude --print`` caller (``planner.claude_with_model``)
rather than the Agent SDK goalclaw used standalone: one cognition mechanism for
the whole service, no extra dependency, and it bills the Pro/Max OAuth session
directly — which (from 2026-06-15) draws from a different pool than the Agent SDK,
so ``--print`` is also the safer quota choice for a recurring loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Awaitable, Callable

from .checklist import ready_items as _ready_items
from .models import Action, BlockOption, Checklist, Goal, GoalStatus, PlanResult
from ..task_git import _review_repo_context_sync

ClaudeCaller = Callable[[str], Awaitable[str]]

_VALID_TOOLS = {"start_program", "implement_feature", "fix_bug", "review_repository"}
_VALID_DECISIONS = {"act", "sleep", "blocked", "done"}

#: the goal planner's model tier (bounded JSON, light reasoning → sonnet)
from ..model_tiers import model_for as _model_for
GOAL_PLANNER_MODEL = _model_for("goal_planner")


class GoalPlannerError(Exception):
    def __init__(self, message: str, raw: str | None = None) -> None:
        super().__init__(message)
        self.raw = raw


#: Cap (chars) for the "engine result" section of the plan prompt. The
#: ``finished_detail`` the tick hands in embeds the engine's task detail
#: verbatim, and for ``review_repository`` that detail deliberately carries up
#: to 200 KB of raw agent transcript (#133 keeps it whole so the done-gate
#: evaluator's per-clause extractor can find the filled report at its END).
#: The planner needs the settle facts and the conclusions, not the transcript:
#: production planner prompt 20260714T181447219Z (finance-sentry-ui-library)
#: hit 236 KB / ~49k tokens (normal is ~8.5–10k) because this section embedded
#: an entire engine result — the worker's own prompt echo included — whole.
#: 12 KB sits between #133's 6 KB non-review keep and #132's 20 KB evaluator
#: headroom: enough for any real summary/verdict, never transcript-sized.
_ENGINE_RESULT_KEEP = 12_000

#: Rendered as its own line where content was elided, so the model knows it is
#: reading the TAIL of a larger engine result, not the whole thing.
_ENGINE_RESULT_TRUNCATION_MARKER = (
    "[…engine result truncated to fit the planning budget: earlier content "
    "elided, tail kept — summaries/verdicts live at the end; the full output "
    "is in the task record / deliveries]"
)


def _cap_engine_result(detail: str) -> str:
    """Bound the just-finished engine result before it rides into the plan
    prompt. Small results pass through UNTOUCHED (byte-identical — existing
    call sites and test stubs see no change). Oversized results are
    tail-kept — ``_task_detail`` appends the verify-gate verdict and gate
    output AFTER the agent summary, and a review report's own conclusions land
    at its end, so the tail is where the signal lives (#132's fallback
    direction; a header-anchored extract would drop those trailing gate
    lines). The settle header line (``tool=… id=… status=…`` with gate/PR
    evidence, built in ``tick_settle``) is preserved so the planner keeps the
    grounded facts of WHAT finished, with an explicit marker where the middle
    was elided."""
    if len(detail) <= _ENGINE_RESULT_KEEP:
        return detail
    head, sep, body = detail.partition("\n")
    if sep and len(head) <= 400:
        return "\n".join((head, _ENGINE_RESULT_TRUNCATION_MARKER, body[-_ENGINE_RESULT_KEEP:]))
    # Degenerate shape (one huge line / oversized first line): plain tail-keep.
    return "\n".join((_ENGINE_RESULT_TRUNCATION_MARKER, detail[-_ENGINE_RESULT_KEEP:]))


async def _collect_repo_context(workspace_dir: str) -> str:
    """Live workspace snapshot for the plan prompt — the same grounded facts the
    review gate gets (remote, branch, head, key-file probes, tracked top-level
    layout; see :func:`devclaw.task_git._review_repo_context_sync`), collected
    fresh at PLAN time. On the fallback paths (investigation dispatch failed,
    discovery synthesis failed, ``DEVCLAW_GOAL_INVESTIGATE=0``, from-scratch
    goals) the prompt otherwise carries ZERO workspace-derived facts beyond a
    path string — and host-side ``claude`` inherits devclaw's own cwd, so an
    ungrounded planner can substitute the control-plane repo (triage F5, the
    planner sibling of the #227 review-gate fix). On the healthy path it keeps
    the discovery brief honest: the brief is a creation-time artifact, this is
    the workspace NOW.

    Async wrapper — runs the blocking collector in a thread (same child-watcher
    rationale as ``task_queue._git_diff``) and looks up
    :func:`_review_repo_context_sync` as a module global so tests can patch it
    here. Strictly best-effort and it NEVER raises: any hiccup degrades to ""
    (the prompt simply omits the section) — grounding must never fail a plan
    step. The tick calls this ONLY past its should_plan gate, so idle/blocked
    ticks stay zero-cost (no git subprocess)."""
    try:
        if not os.path.isdir(workspace_dir):
            # No directory to probe — the collector answers from that one stat
            # (no git subprocess), so skip the thread hop. Keeps the path to
            # cognition free of executor scheduling for absent workspaces,
            # which the tick-lock tests rely on when they park a planner
            # behind bare event-loop pumps (and every non-prepped test goal
            # is this case).
            return _review_repo_context_sync(workspace_dir)
        return await asyncio.to_thread(_review_repo_context_sync, workspace_dir)
    except Exception:  # noqa: BLE001 — best-effort, never fail the plan step
        return ""


def _render_checklist_section(checklist: Checklist) -> str:
    """A compact view of the checklist for the per-tick planner: ready items
    (status==not_started + deps satisfied) plus a coarse status tally. The
    planner picks the next action from ready items; the tally tells it when
    the checklist is exhausted and it can propose done."""
    total = len(checklist.items)
    by_status: dict[str, int] = {}
    for it in checklist.items:
        by_status[it.status] = by_status.get(it.status, 0) + 1
    tally = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
    ready = _ready_items(checklist)
    lines = [
        f"items total: {total}  ({tally})",
        f"ready items (pick ONE; populate addresses=[<id>] in your action):",
    ]
    if not ready:
        lines.append("  (none — every not_started item has unmet dependencies; "
                     "if everything else is done, propose decision='done')")
    for it in ready:
        deps_note = (
            f"  deps: {', '.join(it.depends_on)}" if it.depends_on else "  deps: none"
        )
        evid = f"  evidence_target: {it.evidence_target}"
        note = f"  note: {it.note}" if it.note else ""
        lines.append(f"  - {it.id}: {it.requirement}")
        lines.append(deps_note)
        lines.append(evid)
        if note:
            lines.append(note)
    if checklist.open_questions:
        lines.append("\nopen questions the decomposer left for the owner:")
        for q in checklist.open_questions:
            lines.append(f"  - {q}")
    if checklist.notes:
        lines.append("\nnotes from the decomposer (executor hints):")
        for n in checklist.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)


def build_prompt(
    goal: Goal,
    status: GoalStatus,
    recent_log: str,
    steering: str,
    finished_detail: str,
    discovery: str = "",
    checklist: Checklist | None = None,
    trends: str = "",
    repo_context: str = "",
) -> str:
    from ..prompts import load_prompt

    backlog = "\n".join(f"  - {b}" for b in goal.backlog) or "  (none listed)"
    parts = [
        load_prompt("goal-planner"),
        "\n## Goal",
        f"id: {goal.id}",
        f"objective: {goal.objective}",
        f"done_when: {goal.done_when or '(not specified)'}",
        f"engine: {goal.engine}  workspace_dir: {goal.workspace_dir}",
        f"verify_cmd: {goal.verify_cmd or '(none)'}",
        "backlog:",
        backlog,
        "\n## Current state",
        f"phase: {status.phase}",
        f"next (intended): {status.next or '(none)'}",
    ]
    if status.last_eval_verdict:
        parts.append(f"last direction eval: {status.last_eval_verdict} — {status.last_eval_note}")
    parts += [
        "\n## Recent history (log)",
        recent_log or "(no events yet)",
    ]
    # Live workspace snapshot (triage F5): rendered BEFORE the discovery brief
    # — the brief is a creation-time artifact, this is the workspace now. ""
    # (collector hiccup) skips the section rather than telegraphing an empty
    # discipline, same convention as trends below.
    if repo_context:
        parts += [
            "\n## Repository context (facts from the actual workspace — "
            "trust this over any assumption)",
            repo_context,
        ]
    if discovery:
        parts += [
            "\n## Discovery brief (from investigating the repo — current state · "
            "gap-to-good · what good looks like; draw the next action from this)",
            discovery,
        ]
    # Trend signals: per-project retrospective findings the detector wrote to
    # ``<workspace>/.devclaw/trends.md``. Surfaced AFTER discovery (which is
    # current-state framing) and BEFORE the checklist (which is the action
    # menu) so the planner can let the retrospective inform the pick. Caller
    # passes "" when the file is missing OR holds only the "(no trends yet)"
    # placeholder — keeps the prompt clean rather than telegraphing an empty
    # discipline (trend-PR3 design choice).
    if trends:
        parts += [
            "\n## Trend signals (recent retrospective findings for this project)",
            trends,
        ]
    if checklist is not None and checklist.items:
        parts += [
            "\n## Checklist (ready items)",
            _render_checklist_section(checklist),
        ]
    if finished_detail:
        parts += [
            "\n## The action that just finished (engine result)",
            _cap_engine_result(finished_detail),
        ]
    if steering:
        parts += ["\n## NEW steering (honor this)", steering]
    parts.append("\nReturn the JSON now.")
    return "\n".join(parts)


def extract_json(text: str) -> str:
    trimmed = text.strip()
    if trimmed.startswith("{"):
        return trimmed
    fence = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", trimmed)
    if fence and fence.group(1):
        return fence.group(1)
    first, last = trimmed.find("{"), trimmed.rfind("}")
    if first >= 0 and last > first:
        return trimmed[first : last + 1]
    raise GoalPlannerError("No JSON object found in planner response", text)


def _parse_block_options(raw: object) -> list[BlockOption]:
    """Blank-safe parse of a blocked decision's structured options (§6, ADR
    0010). A non-list — or any entry missing key/label/steer, or a duplicate
    key — is silently dropped: an absent/malformed options field must degrade
    to the pre-§6 free-text block, NEVER raise. This is the fail-closed
    cognition rule — an undocumented/garbled model field is ignored, not
    honored (cf. the removed planner verify_cmd override)."""
    if not isinstance(raw, list):
        return []
    out: list[BlockOption] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "")).strip()
        label = str(entry.get("label", "")).strip()
        steer = str(entry.get("steer", "")).strip()
        if not key or not label or not steer or key in seen:
            continue
        seen.add(key)
        out.append(
            BlockOption(key=key, label=label, detail=str(entry.get("detail", "")).strip(), steer=steer)
        )
    return out


def validate(parsed: object) -> PlanResult:
    if not isinstance(parsed, dict):
        raise GoalPlannerError("Plan must be a JSON object")
    decision = parsed.get("decision")
    if decision not in _VALID_DECISIONS:
        raise GoalPlannerError(f"decision must be one of {_VALID_DECISIONS}, got {decision!r}")
    note = str(parsed.get("note", "")).strip()

    if decision == "blocked":
        question = str(parsed.get("question", "")).strip()
        if not question:
            raise GoalPlannerError("blocked decision requires a non-empty 'question'")
        options = _parse_block_options(parsed.get("options"))
        # `recommended` is only meaningful if it names one of the parsed options;
        # an unknown/blank key degrades to "no recommendation" (the UI just shows
        # no highlight), never an error.
        recommended = str(parsed.get("recommended", "")).strip()
        if recommended and recommended not in {o.key for o in options}:
            recommended = ""
        return PlanResult(
            decision="blocked", question=question, options=options,
            recommended=recommended, note=note or question,
        )

    if decision in ("sleep", "done"):
        return PlanResult(decision=decision, note=note)

    # decision == "act"
    raw_actions = parsed.get("actions")
    if not isinstance(raw_actions, list) or len(raw_actions) != 1:
        raise GoalPlannerError("act decision requires exactly one action")
    a = raw_actions[0]
    if not isinstance(a, dict):
        raise GoalPlannerError("action must be an object")
    tool = a.get("tool")
    if tool not in _VALID_TOOLS:
        raise GoalPlannerError(f"action.tool must be one of {_VALID_TOOLS}, got {tool!r}")
    g = str(a.get("goal", "")).strip()
    if not g:
        raise GoalPlannerError("action.goal must be non-empty")
    raw_addresses = a.get("addresses")
    addresses: list[str] = []
    if isinstance(raw_addresses, list):
        seen: set[str] = set()
        for entry in raw_addresses:
            s = str(entry).strip()
            if s and s not in seen:
                seen.add(s)
                addresses.append(s)
    raw_title = a.get("title")
    parsed_title = str(raw_title).strip() if raw_title else None
    # A planner-supplied "verify_cmd" is deliberately IGNORED — never honored,
    # never an error (triage F5). The prompt schema has never offered the
    # field, so any value here is an ungrounded guess, and accepting it let
    # that guess mechanically OVERRIDE the firmed command at dispatch
    # (engine.py: ``action.verify_cmd or goal.verify_cmd``). The firmed
    # verify_cmd IS the grounded contract; Action.verify_cmd stays None on
    # every planner path so the engine always falls through to it.
    action = Action(
        engine="devclaw",
        tool=tool,
        goal=g,
        open_pr=bool(a.get("open_pr", True)),
        addresses=addresses,
        title=parsed_title or None,
    )
    return PlanResult(decision="act", actions=[action], note=note or g)


async def plan(
    goal: Goal,
    status: GoalStatus,
    recent_log: str,
    steering: str,
    finished_detail: str,
    *,
    claude_caller: ClaudeCaller,
    discovery: str = "",
    checklist: Checklist | None = None,
    trends: str = "",
    repo_context: str = "",
) -> PlanResult:
    """Run the next-action plan step. ``claude_caller`` is injected so tests stub
    the LLM. ``discovery`` is the investigating-phase brief, when present.
    ``checklist`` is the decomposer's structured plan — when present, the
    prompt enters checklist mode and the planner picks one ready item.
    ``trends`` is the per-project trend retrospective tail (closes the
    detector → consumer loop; see trend-PR3). ``repo_context`` is the live
    workspace snapshot from :func:`_collect_repo_context` — grounded facts
    from the actual repo at plan time (triage F5); "" omits the section."""
    prompt = build_prompt(
        goal, status, recent_log, steering, finished_detail, discovery, checklist, trends,
        repo_context,
    )
    raw = await claude_caller(prompt)
    try:
        parsed = json.loads(extract_json(raw))
    except json.JSONDecodeError as exc:
        raise GoalPlannerError(f"planner emitted invalid JSON: {exc}", raw) from exc
    return validate(parsed)


def default_caller() -> ClaudeCaller:
    """The production cognition caller, bound to the goal-planner tier. Imported
    lazily from devclaw's shared ``claude --print`` factory so unit tests (which
    inject a fake) never touch the subprocess."""
    from ..planner import claude_with_model

    return claude_with_model(GOAL_PLANNER_MODEL, role="goal_planner")
