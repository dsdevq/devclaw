#!/usr/bin/env python3
"""PreToolUse guard: block `git commit` / `git push` that would land on main.

Enforces the repo convention "branch per change; open a PR, don't push to
main" at the tool boundary — the one convention that is irreversible when
violated and had zero enforcement before this hook.

Deliberately narrow (false positives cost more than false negatives here):

  BLOCKED  git commit …            while the command's cwd is on main
  BLOCKED  git push (bare)         while on main (would push main)
  BLOCKED  git push <remote>       while on main (same)
  BLOCKED  any push refspec that TARGETS main (…:main, `origin main`, HEAD:main)

  ALLOWED  everything else — including branch deletes (`push origin --delete x`),
           explicit non-main refspecs pushed from a main checkout
           (`push origin sha:refs/heads/x`), pulls, fetches, worktree ops.

Escape hatch: prefix the command with `DEVCLAW_ALLOW_MAIN=1 ` when a push to
main is genuinely intended.

Exit codes per the hooks contract: 0 allow · 2 block (stderr shown to Claude).
Fail-OPEN on our own errors: a broken guard must never wedge normal work —
this protects a convention, it is not a security boundary.
"""

import json
import re
import subprocess
import sys


def current_branch(cwd: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd or ".", "branch", "--show-current"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def blocks(command: str, cwd: str) -> "str | None":
    if "DEVCLAW_ALLOW_MAIN=1" in command:
        return None
    # Only inspect commands that contain a git commit/push at all.
    if not re.search(r"\bgit\b[^|;&]*\b(commit|push)\b", command):
        return None
    on_main = current_branch(cwd) in ("main", "master")

    if on_main and re.search(r"\bgit\b[^|;&]*\bcommit\b", command):
        return (
            "blocked: `git commit` on main. Branch per change — create a "
            "worktree + branch (see .claude/rules/git-workflow.md). "
            "Override only if truly intended: prefix with DEVCLAW_ALLOW_MAIN=1"
        )

    for push in re.finditer(r"\bgit\b[^|;&]*\bpush\b([^|;&]*)", command):
        args = [a for a in push.group(1).split() if not a.startswith("-")]
        # `git push origin --delete x` / explicit non-main refspecs are fine.
        if any(a == "main" or a.endswith(":main") or a.endswith("/main") and ":" in a
               for a in args):
            return (
                "blocked: push targets main. Open a PR instead (squash-merge "
                "via gh). Override: prefix with DEVCLAW_ALLOW_MAIN=1"
            )
        if on_main and len(args) <= 1 and "--delete" not in push.group(1):
            # bare `git push` / `git push origin` from a main checkout
            return (
                "blocked: bare `git push` while on main would push main. "
                "Branch per change. Override: prefix with DEVCLAW_ALLOW_MAIN=1"
            )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if payload.get("tool_name") != "Bash":
            return 0
        command = (payload.get("tool_input") or {}).get("command", "") or ""
        cwd = payload.get("cwd") or ""
        msg = blocks(command, cwd)
        if msg:
            print(msg, file=sys.stderr)
            return 2
        return 0
    except Exception:
        return 0  # fail-open: never wedge work on a guard bug


if __name__ == "__main__":
    sys.exit(main())
