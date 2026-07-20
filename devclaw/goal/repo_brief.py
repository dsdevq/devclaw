"""Repo-scoped worker memory brief (mission-control borrow item 3).

Every new goal on the same repo used to relearn build quirks and test
gotchas from zero: ``goal_docs`` die with their goal, and the sandbox
workspace is ``git clean -fdx``-wiped per dispatch, so nothing a worker
left in-repo survived either. This module is the pure glue around the
host-side fix — a ``project_docs`` row keyed by normalized workspace path:

- workers hand back one-line ``REPO NOTES:`` facts (the runner's return
  contract parses them into ``result_json``);
- the settle path merges them into the repo's accumulated brief
  (:func:`merge_repo_notes` — plain line dedupe + a size cap, zero LLM);
- the dispatch path prepends the brief to the next worker's goal text
  (:func:`render_brief_prefix` — plain text injection, model-agnostic,
  no vendor hook wiring).

Everything here is pure computation; reads/writes live on the GoalStore
(``read_repo_brief`` / ``write_repo_brief``) and the calling tick modules.
"""

from __future__ import annotations

from ..project_registry import _normalize_workspace

#: Cap on the accumulated brief. Oldest lines are dropped first — the brief
#: is an operational cheat-sheet, not an archive; recent facts win.
MAX_BRIEF_CHARS = 4000


def scope_key_for(workspace_dir: "str | None") -> "str | None":
    """The project_docs join key for a workspace — the registry's normalized
    path shape, so the brief and the project registry agree on identity."""
    return _normalize_workspace(workspace_dir)


def merge_repo_notes(existing: "str | None", new_notes: str) -> str:
    """Fold a worker's hand-back notes into the accumulated brief.

    Plain-code policy (deterministic, zero LLM): one fact per line; exact
    duplicate lines are dropped; new lines append at the end; when the cap
    is exceeded the OLDEST lines fall off. Returns the merged brief text.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for raw in (existing or "").splitlines():
        line = raw.strip()
        if line and line not in seen:
            lines.append(line)
            seen.add(line)
    for raw in new_notes.splitlines():
        line = raw.strip().lstrip("-*• \t").strip()
        if line and line.lower() != "none" and line not in seen:
            lines.append(line)
            seen.add(line)
    while lines and sum(len(l) + 1 for l in lines) > MAX_BRIEF_CHARS:
        dropped = lines.pop(0)
        seen.discard(dropped)
    return "\n".join(lines)


def render_brief_prefix(brief: "str | None") -> str:
    """The dispatch-time prefix for a non-empty brief, '' otherwise.

    Framed as prior-run observations, not instructions — the worker's goal
    and skills stay authoritative; the brief is context it may verify.
    """
    if not brief or not brief.strip():
        return ""
    return (
        "[Repo notes — observations handed back by previous devclaw runs on "
        "this repository. Treat as hints, verify anything load-bearing:]\n"
        f"{brief.strip()}\n\n---\n\n"
    )
