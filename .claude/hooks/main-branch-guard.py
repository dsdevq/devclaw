#!/usr/bin/env python3
"""PreToolUse guard: block `git commit` / `git push` that would land on main.

Enforces the repo convention "branch per change; open a PR, don't push to
main" at the tool boundary — the one convention that is irreversible when
violated and had zero enforcement before this hook.

Deliberately narrow (false positives cost more than false negatives here):

  BLOCKED  git commit …            when the command's EFFECTIVE dir is on main
  BLOCKED  git push (bare)         while on main (would push main)
  BLOCKED  any push refspec that TARGETS main (…:main, `origin main`, HEAD:main)

  ALLOWED  everything else — branch deletes, explicit non-main refspecs, pulls,
           fetches, worktree ops, and any commit in a worktree/feature branch.

Effective dir: real work runs in a worktree the command `cd`s into, but the
session cwd in the payload is the main checkout (kept on `main`). Trusting the
payload cwd blocked EVERY legitimate worktree commit (systematic false
positive) and conditioned the DEVCLAW_ALLOW_MAIN reflex that hollows out the
guard. So we resolve the dir the git command actually runs in — `git -C <path>`
or a leading literal `cd <path>` — and only fall back to the payload cwd when
neither is present. A target dir that is shell-expanded ($VAR / $() / `cmd`) is
unresolvable here → branch treated as unknown → the commit-on-main block is
skipped; the dir-independent push-targets-main check still applies.

Escape hatch: prefix the command with `DEVCLAW_ALLOW_MAIN=1 `.

Exit codes: 0 allow · 2 block (stderr shown to Claude). Fail-OPEN on our own
errors: a broken guard must never wedge work — this protects a convention, not
a security boundary.
"""

import json
import os
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


def effective_cwd(command: str, payload_cwd: str) -> "str | None":
    """Directory the git command will run in. `git -C <path>` (the dir form,
    which precedes the subcommand — NOT `git commit -C <ref>`) and a leading
    `cd <path>` win over the payload cwd. A shell-expanded path we can't resolve
    here returns None (branch unknown)."""
    for pat in (r"\bgit\s+-C\s+(\S+)",
                r"(?:^|&&|\|\||;)\s*cd\s+(\S+)"):
        m = re.search(pat, command)
        if m:
            raw = m.group(1)
            if "$" in raw or "`" in raw:
                return None
            return os.path.expanduser(raw.strip("'\""))
    return payload_cwd


def blocks(command: str, cwd: str) -> "str | None":
    if "DEVCLAW_ALLOW_MAIN=1" in command:
        return None
    if not re.search(r"\bgit\b[^|;&]*\b(commit|push)\b", command):
        return None
    eff = effective_cwd(command, cwd)
    on_main = eff is not None and current_branch(eff) in ("main", "master")

    if on_main and re.search(r"\bgit\b[^|;&]*\bcommit\b", command):
        return (
            "blocked: `git commit` on main. Branch per change — create a "
            "worktree + branch (see .claude/rules/git-workflow.md). "
            "Override only if truly intended: prefix with DEVCLAW_ALLOW_MAIN=1"
        )

    for push in re.finditer(r"\bgit\b[^|;&]*\bpush\b([^|;&]*)", command):
        args = [a for a in push.group(1).split() if not a.startswith("-")]
        if any(a == "main" or a.endswith(":main") or a.endswith("/main") and ":" in a
               for a in args):
            return (
                "blocked: push targets main. Open a PR instead (squash-merge "
                "via gh). Override: prefix with DEVCLAW_ALLOW_MAIN=1"
            )
        if on_main and len(args) <= 1 and "--delete" not in push.group(1):
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
