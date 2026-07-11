#!/usr/bin/env python3
"""PostToolUse(Bash) hook: after a git commit that touches code but no .md,
inject a reminder naming the docs that describe the changed areas.

Non-blocking by design — the commit stands; this asks for a conscious doc
check before the PR goes up (CLAUDE.md: fix the doc in the same PR, update
its currency tag in docs/INDEX.md). Many commits legitimately have no doc
impact; the right response then is a one-line "no doc impact" and moving on.

DOC_MAP is audited by the docs-audit skill — keep it in sync with docs/INDEX.md.
Set DEVCLAW_DOCS_HOOK_ALL=1 to skip the HEAD-recency guard (testing/debugging).
"""
import json
import os
import subprocess
import sys
import time

DOC_MAP = [
    ("devclaw/server/", ["docs/architecture-layers.md (layer 1: MCP surface)"]),
    ("devclaw/goal/", ["docs/architecture-layers.md (layers 2-3)", "docs/task-execution-flow.md"]),
    ("devclaw/engine/", ["docs/task-execution-flow.md", "docs/engine-decision.md", "docs/live-shakedown.md"]),
    ("openhands-runner/", ["docs/task-execution-flow.md (layer 5)", "docs/live-shakedown.md"]),
    (".sandcastle/", ["docs/live-shakedown.md"]),
    ("devclaw/delivery/", ["docs/delivery-flows.md"]),
    ("devclaw/task_queue.py", ["docs/architecture-layers.md (layer 4)", "docs/task-execution-flow.md"]),
    ("devclaw/state_store.py", ["docs/architecture-layers.md (single-writer invariant)"]),
    ("devclaw/cli.py", ["README.md (usage narrative)"]),
]


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    command = (payload.get("tool_input") or {}).get("command", "")
    if "git commit" not in command:
        return

    repo = os.environ.get("CLAUDE_PROJECT_DIR", ".")

    def git(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", "-C", repo, *args],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except Exception:
            return ""

    # Only react to a commit that just landed: a failed `git commit` (or one
    # run in some other repo) leaves this repo's HEAD old, and we stay silent.
    if os.environ.get("DEVCLAW_DOCS_HOOK_ALL") != "1":
        head_ts = git("log", "-1", "--format=%ct")
        if not head_ts.isdigit() or time.time() - int(head_ts) > 180:
            return

    files = [f for f in git("diff-tree", "-r", "--no-commit-id", "--name-only", "HEAD").splitlines() if f]
    if not files or any(f.endswith(".md") for f in files):
        return

    areas, docs = [], []
    for prefix, mapped in DOC_MAP:
        if any(f == prefix or f.startswith(prefix) for f in files):
            areas.append(prefix)
            docs.extend(d for d in mapped if d not in docs)
    if not docs:  # only unmapped paths (tests/, evals/, config) — no doc surface
        return

    message = (
        "Docs-honesty check (devclaw commit hook): the commit that just landed touches "
        + ", ".join(areas)
        + " but includes no .md changes. Docs describing these areas: "
        + "; ".join(docs)
        + ". Per CLAUDE.md, if this change makes any doc statement wrong, fix the doc "
        "in this same PR and update its currency tag in docs/INDEX.md (also consider "
        "README.md's narrative, and docs/env-vars.md if env vars were added/renamed). "
        "If the commit has no doc impact, state that in one line and move on."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": message,
        }
    }))


main()
