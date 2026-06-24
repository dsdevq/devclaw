"""Plain-language summarizer — rewrite an owner-altitude notification into one or
two non-technical sentences.

This is cognition, but deliberately bounded so it can't threaten the quota:
  - it runs ONLY on OWNER-level notifications (see goal_tick.NotifyLevel) — the
    rare, owner-facing events (a blocker, a paused-for-review, a direction
    question, a verified completion), never per-task, never on the zero-token
    idle path;
  - it runs at a cheap tier (haiku by default);
  - it is best-effort — ANY failure (caller error, empty / runaway output)
    returns the original text, so a notification is never lost and a tick is
    never broken by the summarizer.

The outcome-goals split: mechanism decides WHEN to notify (the altitude gate in
goal_tick); cognition here writes WHAT it says.
"""

from __future__ import annotations

import os

from .planner import ClaudeCaller

#: cheap tier — owner notifications are rare, so this never touches idle quota.
GOAL_SUMMARY_MODEL = os.environ.get("DEVCLAW_GOAL_SUMMARY_MODEL", "haiku") or None
#: on by default; disable with DEVCLAW_GOAL_PLAIN_SUMMARY=0 to send raw text.
PLAIN_SUMMARY_ENABLED = os.environ.get("DEVCLAW_GOAL_PLAIN_SUMMARY", "1") not in ("0", "false", "")

_PROMPT = """Rewrite this status update so a NON-TECHNICAL product owner understands it at a glance.
Rules:
- One or two plain sentences. No jargon, no IDs, no file names, no tool names.
- Preserve the meaning and any action the owner must take.
- Preserve a single leading emoji if the message has one.
- Output ONLY the rewritten message — no preamble, no quotes.

Message:
{text}"""

#: a rewrite longer than this is treated as runaway/garbage → fall back to raw.
_MAX_LEN = 600


async def plain_summary(text: str, *, caller: ClaudeCaller) -> str:
    """Best-effort plain-language rewrite of ``text``. Returns ``text`` unchanged
    on any failure — a notification must never be lost to a summarizer hiccup."""
    try:
        out = (await caller(_PROMPT.format(text=text))).strip()
    except Exception:  # noqa: BLE001 — best-effort; never break a notification
        return text
    if not out or len(out) > _MAX_LEN:
        return text
    return out


def default_caller() -> ClaudeCaller:
    """Production summarizer caller, bound to the cheap summary tier. Imported
    lazily from devclaw's shared ``claude --print`` factory so unit tests (which
    inject a fake) never touch the subprocess."""
    from ..planner import claude_with_model

    return claude_with_model(GOAL_SUMMARY_MODEL)
