# Resolving merge conflicts

Use when a git merge or rebase is in progress and conflicting. The goal is a
resolution that preserves both sides' intent — never a mechanical "pick ours".

1. **See the current state.** Which operation is in progress (merge, rebase,
   cherry-pick), which files conflict, and what the history looks like on both
   sides (`git log --oneline --left-right <ours>...<theirs>` on the touched
   paths).

2. **Find the intent behind each side.** Read the commit messages of the
   conflicting commits, and any linked issues or PRs that are reachable.
   Understand *why* each change was made before deciding between them —
   a conflict is two intents colliding, and you can't arbitrate intents you
   haven't read.

3. **Resolve each hunk.** Preserve both intents where possible. Where they are
   genuinely incompatible, pick the one that matches the current task's goal
   and record the trade-off in your summary. Do **not** invent new behavior
   that neither side had. Always resolve; never `--abort` — an aborted merge
   is an abandoned task, not a completed one.

4. **Run the project's checks.** Discover and run the automated checks —
   typically typecheck, then tests, then format. A resolution that compiles
   but breaks tests is not resolved. Fix anything the merge broke.

5. **Finish the operation.** Stage everything and commit. If rebasing,
   continue until every commit is replayed — a half-finished rebase is worse
   than either branch.

---
*Adapted from [mattpocock/skills](https://github.com/mattpocock/skills) (MIT © 2026 Matt Pocock).*
