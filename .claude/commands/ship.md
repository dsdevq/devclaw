---
description: The pre-PR ritual — suite, docs honesty, invariant review, then commit/push/PR
argument-hint: [optional commit subject, e.g. "fix(goal): …"]
disable-model-invocation: true
---

Current branch: !`git branch --show-current`
Changed files: !`git status --short | head -30`

Ship the working tree as a PR, in this exact order — stop at the first failure
and report it instead of proceeding:

1. **Branch check.** If the branch above is `main`/`master`, STOP: create a
   worktree + branch first (.claude/rules/git-workflow.md). Never ship from main.
2. **Import-path sanity** (worktrees): `.venv/bin/python -c "import devclaw; print(devclaw.__file__)"`
   must print THIS checkout's path.
3. **Full suite**: `TMPDIR=$(mktemp -d) .venv/bin/python -m pytest -q`. Must be
   green — report the exact pass/skip counts.
4. **Docs honesty sweep**: for each code area the diff touches, check the
   mapped docs (DOC_MAP in .claude/hooks/docs-reminder.py) — if the change
   makes a doc claim wrong, fix the doc + its docs/INDEX.md currency tag as
   part of this same change.
5. **Invariant review**: run the `invariant-guard` agent on the diff
   (`git diff main...HEAD`). FIX FIRST verdict ⇒ stop and address it.
6. **Commit**: conventional message ($ARGUMENTS if provided, else derive one),
   body says WHY + names the regression test, ends with
   `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
7. **Push + PR**: `git push -u origin <branch>`, then `gh pr create` — body
   covers what/why, the named regression test, the suite counts, and any
   invariant-guard notes; ends with
   `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
8. Report the PR URL, diffstat, and suite counts. Do NOT merge — merging is
   the owner's call unless already authorized this session.
