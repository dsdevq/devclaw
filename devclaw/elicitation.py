"""The scope grill — interrogate to a shared spec.

Pure cognition: given a rough idea and the running transcript, decide the next
question (with a recommended answer) or finalize a spec.md. Stateless — the
caller (today: the ``scope_grill`` MCP tool, called turn-by-turn by the OpenClaw
waiter) holds the transcript. The chef provides the *craft* (what to ask, what
'enough' looks like); the waiter holds the conversation in Telegram.

The interview methodology is adapted from Matt Pocock's MIT-licensed ``grill-me``
skill (github.com/mattpocock/skills): interview relentlessly until shared
understanding, walk each branch resolving dependencies one-by-one, recommend an
answer per question, ask one at a time, and decide-instead-of-ask when the
answer is obvious.

No transport, no persistence — unit-testable with a stubbed ``claude_caller``.
"""

from __future__ import annotations

import json
import os
from typing import Awaitable, Callable

from .planner import PlannerError, claude_with_model, extract_json

#: conversational requirement-gathering — Sonnet is the right tier. Empty →
#: account default. Read at call time so the env stays the single source.
GRILL_MODEL = os.environ.get("DEVCLAW_GRILL_MODEL", "sonnet") or None

#: hard cap so a grill can't loop forever — after this many answered turns the
#: model is forced to finalize the spec from what it has.
MAX_GRILL_QUESTIONS = int(os.environ.get("DEVCLAW_MAX_GRILL_QUESTIONS", "20"))

_GRILL_RULES = """You are DevClaw's project elicitor. You are interviewing the
user to reach a SHARED UNDERSTANDING of a software project before any code is
written. Methodology (adapted from Matt Pocock's grill-me):

- Interview relentlessly until you genuinely understand WHAT to build and HOW.
- Walk the design tree branch by branch; resolve dependencies between decisions
  one at a time. Ask the single most valuable next question given what's known.
- Ask ONE question at a time. Always include your recommended answer.
- Decide-instead-of-ask: if a question has an obvious best-practice answer, don't
  ask it — fold the decision into the spec and move on.
- Cover at least: the core goal + who it's for, scope (explicitly in AND out),
  tech stack + key architecture decisions, milestones, acceptance criteria,
  hard constraints (perf, hosting, deps), and known risks."""

_SPEC_SHAPE = """A spec is Markdown with these sections:
# <project> — spec
## Goal            — one paragraph; what success is
## Scope           — in / out (explicit out-of-scope list)
## Stack & arch    — decisions + the "why"
## Milestones      — the coarse phases the build moves through
## Acceptance      — checkable criteria per milestone
## Constraints     — perf, deps, hosting, non-negotiables
## Open risks      — known unknowns carried into execution"""

_RESPONSE_CONTRACT = """Respond with STRICT JSON ONLY — no prose, no fences.
Either ask the next question:
  {"action": "ask", "question": "<one question>", "recommended": "<your recommended answer>"}
or, when you have enough for a shared understanding, finalize:
  {"action": "done", "spec": "<the full spec.md markdown>"}"""


def build_grill_prompt(idea: str, transcript: list[dict], *, finalize: bool) -> str:
    lines = [f"PROJECT IDEA:\n{idea}", ""]
    if transcript:
        lines.append("INTERVIEW SO FAR (question → recommended → user's answer):")
        for i, turn in enumerate(transcript, 1):
            lines.append(f"{i}. Q: {turn.get('question', '')}")
            if turn.get("recommended"):
                lines.append(f"   (recommended: {turn['recommended']})")
            lines.append(f"   A: {turn.get('answer', '')}")
        lines.append("")
    if finalize:
        closing = (
            "You have asked enough questions. Do NOT ask another — output the "
            'final spec now as {"action": "done", "spec": "..."}, filling any '
            "remaining gaps with your recommended defaults."
        )
    else:
        closing = (
            "Decide: is there a genuinely valuable next question, or do you now "
            "have a shared understanding? Ask one question OR finalize the spec."
        )
    return "\n".join(
        [_GRILL_RULES, "", _SPEC_SHAPE, "", *lines, closing, "", _RESPONSE_CONTRACT]
    )


def validate_step(parsed: object) -> dict:
    """Validate a grill response into {'action':'ask',...} or {'action':'done','spec':...}."""
    if not isinstance(parsed, dict):
        raise PlannerError("Grill response must be a JSON object")
    action = parsed.get("action")
    if action == "ask":
        q = parsed.get("question")
        if not isinstance(q, str) or not q.strip():
            raise PlannerError("Grill 'ask' missing a question")
        rec = parsed.get("recommended")
        return {
            "action": "ask",
            "question": q.strip(),
            "recommended": rec.strip() if isinstance(rec, str) else "",
        }
    if action == "done":
        spec = parsed.get("spec")
        if not isinstance(spec, str) or not spec.strip():
            raise PlannerError("Grill 'done' missing a spec")
        return {"action": "done", "spec": spec.strip()}
    raise PlannerError(f"Grill action must be 'ask' or 'done', got {action!r}")


def default_caller() -> Callable[[str], Awaitable[str]]:
    """Production cognition caller bound to the grill tier (lazy, env-current)."""
    return claude_with_model(GRILL_MODEL)


async def next_step(
    idea: str,
    transcript: list[dict],
    claude_caller: "Callable[[str], Awaitable[str]] | None" = None,
) -> dict:
    """Run one grill turn. Returns an 'ask' step (next question + recommendation)
    or a 'done' step (the finalized spec). Forces finalization once the question
    cap is hit. ``claude_caller`` is injected so tests can stub the subprocess;
    bound to the grill tier on first real use."""
    if claude_caller is None:
        claude_caller = default_caller()
    finalize = len(transcript) >= MAX_GRILL_QUESTIONS
    raw = await claude_caller(build_grill_prompt(idea, transcript, finalize=finalize))
    try:
        parsed = json.loads(extract_json(raw))
    except json.JSONDecodeError as err:
        raise PlannerError(f"Grill JSON parse failed: {err}", raw) from err
    step = validate_step(parsed)
    # Safety: if the cap forced finalize but the model still tried to ask, treat
    # whatever it gave as incomplete and demand a spec on the caller's next pass.
    if finalize and step["action"] == "ask":
        raise PlannerError("Grill exceeded the question cap without finalizing a spec")
    return step
