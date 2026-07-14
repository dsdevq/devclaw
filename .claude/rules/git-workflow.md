# Git workflow — branches, PRs, merges

## Branch discipline

- **Never commit or push on `main`** — branch per change (a PreToolUse hook
  enforces this; the escape hatch is prefixing the command with
  `DEVCLAW_ALLOW_MAIN=1 ` when you genuinely mean it, e.g. pulling).
- **Branch work happens in a worktree** — Denys runs parallel sessions in this
  checkout. `git worktree add <scratchpad-path> -b <branch> origin/main`, work
  there, remove the worktree when merged. Verify `git branch --show-current`
  before every commit.
- Branch names: `<type>/<slug>` — `fix/`, `feat/`, `docs/`, `harden/`,
  `refactor/`.

## Commits and PRs

- Conventional commits: `fix(queue): …`, `feat(goal): …`, `docs: …`. Body says
  WHY, names the named regression test.
- End every commit message with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- End every PR body with:
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`
- Full suite green before `gh pr create` (see rules/testing.md). Docs honesty:
  if the diff makes a doc wrong, fix the doc + its `docs/INDEX.md` currency tag
  in the SAME PR.
- Repo merges are **squash** (`gh pr merge --squash`).

## Stacked PRs (learned the hard way — #235)

Squash-merging a stack parent with `--delete-branch` **CLOSES the child PR**
(GitHub does not retarget it). Procedure:

1. Merge the parent WITHOUT deleting its branch.
2. Retarget the child: `gh api -X PATCH repos/<owner>/<repo>/pulls/<N> -f base=main`
   (`gh pr edit --base` can silently fail; use the REST call).
3. Rebase ONLY the child's own commits onto main:
   `git rebase --onto origin/main <old-parent-tip> <child-branch>` — a plain
   merge of main reproduces the parent's squashed content as conflicts.
4. Force-push (`--force-with-lease`), re-run the suite, merge, THEN delete
   branches.

Conflicts between two PRs that both append at a file's tail (tests, INDEX.md
rows) are unions — keep both sides; never drop the other PR's content.
