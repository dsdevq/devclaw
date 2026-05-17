---
name: code-task
description: "Execute a bounded `kind: code` Task Spec — clone target repo, implement changes against acceptance criteria, run tests, push branch, open a PR, and report status back into ~/.life/tasks/<id>/result.json. Triggered by the task_dispatch skill (Phase 5.5+) OR invoked directly when the operator references a Task Spec path. NOT for multi-day work — that's swarm's job (Phase 6); this skill is for tasks finishable in <4h with at most one round of replan."
---

# code-task

You are running a bounded autonomous coding task. The Task Spec at the path you've been handed is your full input. Your job: read the spec, do the work, open a PR, report the result.

You are **NOT in conversation** with the operator during this run. He may be asleep. No clarifying questions; if the spec is ambiguous, make a defensible call and document it in `result.json` notes. The only output the operator sees is what you write to `result.json` and the announce message at the end.

## Hard behavioral rules

- **Never edit `~/.life/domains/` directly.** Curator owns that surface. If your task needs a domain change, write a patch under `~/.life/.curator-proposed/<task_id>.patch` and append a notification to `~/.life/queue.jsonl`.
- **Never edit the spec.yaml.** That's the `task_update` skill's job. You may *read* the spec; the only mutation you make to the task directory is appending to `run.log.jsonl` and writing `result.json` exactly once at the end.
- **Never touch `~/projects/` on the host or the PC's working trees.** All your work happens in `/tmp/<task_id>/` inside the container. Clone fresh; throw away after.
- **Stay inside the time budget.** The spec carries `budget.max_runtime_seconds`. If you're approaching the limit, stop, push whatever you have, and report `status: blocked` with reason `time_budget_exceeded` plus a clear "to resume, do X" handoff note.
- **Don't push to `main`/`master` directly.** Always work on a branch named `kit/<task_id>-<short-slug>` and open a PR. Even for trivial fixes.
- **Don't bypass safety checks.** No `--no-verify`, no `--force` push, no skipping tests. If the pre-commit hook fails, fix the underlying issue or report blocked.

## Inputs you'll receive

When invoked, you'll be given a path like `~/.life/tasks/<task_id>/spec.yaml` (atomic) OR `~/.life/projects/<slug>/runs/<run>/tasks/<task_id>/spec.yaml` (run-bound, Phase 5.7c). The frontmatter you care about:

```yaml
task_id: 2026-05-17-fix-typo-in-readme-x9a3
kind: code                                   # always 'code' when this skill runs
verbatim_intent: |
  In dsdevq/lifekit-stack, fix the typo "depployment" → "deployment" in README.md.
acceptance_criteria:
  - The typo is corrected in README.md
  - PR opened against main
  - No other file changes
budget:
  max_runtime_seconds: 1800                  # 30 min — your hard cap
target_repo: dsdevq/lifekit-stack             # required for code tasks
target_branch: main                          # base branch for the PR (default: main)
project: lifekit-stack                       # set by task_intake's project gate; null for raw atomic
proposal_path: ~/.life/.../proposals-approved/...md  # set for proposal-bound runs; null otherwise
run: <run-slug>                              # set ONLY when spawned by project_curator (Phase 5.7c)
run_node: <node-id>                          # set ONLY when run-bound
context_files:                               # set ONLY when run-bound (project_curator fills it)
  - ~/.life/projects/lifekit-stack/plan.md
  - ~/.life/projects/lifekit-stack/recon.md
  - ~/.life/projects/lifekit-stack/runs/<run>/dag.yaml
```

If `target_repo` is missing, write `result.json` with `status: blocked` reason `target_repo_missing` and stop — don't guess.

### Project/run context loading

If `project` is set, BEFORE you start any work — Read these files (in this order) to load context:

1. `~/.life/projects/<project>/plan.md` — the project's "how Kit should work on this project" section is binding.
2. `~/.life/projects/<project>/settings.yaml` — `test_command`, `default_base_branch`, `mirror_to_issues` flags.
3. `~/.life/projects/<project>/recon.md` if present — read selectively (the conventions + module map sections, not the whole thing).
4. If `proposal_path` is set: read the proposal end-to-end. Its "What changes" + "Risks + mitigations" + "Acceptance criteria" sections are the contract.
5. If `run` is set: read `~/.life/projects/<project>/runs/<run>/dag.yaml`. Find your node (`id == run_node`); look at sibling nodes' status — if a sibling is `runner_status: claimed_done` or `verified_done` and you depend on it, look at its `evidence.files_changed` to understand the state of the repo.
6. Any extra `context_files` not covered above.

Don't paraphrase these into a planning doc — just internalize them and proceed. The context is binding; if it conflicts with `verbatim_intent`, the project's plan.md wins (escalate via `result.json` notes).

## Execution sequence

### 1. Set up the workspace

```bash
TASK_ID="<from spec.yaml>"
WORK="/tmp/${TASK_ID}"
rm -rf "$WORK"   # idempotent — fresh clone each run
git clone "https://github.com/${target_repo}.git" "$WORK"
cd "$WORK"
git checkout -b "kit/${TASK_ID}-<short-slug>"
```

Append the first entry to `~/.life/tasks/<task_id>/run.log.jsonl`:
```json
{"ts":"<iso>","actor":"code-task","event":"workspace_ready","workdir":"/tmp/<task_id>"}
```

### 2. Implement against the acceptance criteria

This is the open part. Use the Read/Edit/Write tools to make the changes the spec asks for. Use Bash for build/test commands.

Heuristics:
- **Start by exploring the relevant files.** Don't edit blind. For a typo fix, grep the typo first. For a feature, read the surrounding code first.
- **Check for tests.** If the repo has tests, run them before changing anything to establish baseline. After changes, run them again — your patch must keep them green (or fix any new failures).
- **Match style.** Read 2–3 nearby files; mirror their conventions (indentation, naming, doc comments). Don't impose alien style.
- **One PR, one concern.** Don't bundle drive-by cleanups even if you notice them — note them in `result.json.notes` for a follow-up task.

Append meaningful events to `run.log.jsonl` (e.g. `event: tests_passed`, `event: file_edited`, `event: replan` if you change approach mid-way). This is the audit trail.

### 3. Commit + push + open PR

```bash
cd "$WORK"
git add -A
git status   # sanity-check: only the files you intended
git commit -m "$(cat <<EOF
<concise title — present tense, no emoji, no AI signature unless the repo has one>

<2–4 sentence body explaining the why, not the what>

Refs: ~/.life/tasks/${TASK_ID}/
EOF
)"
git push -u origin "kit/${TASK_ID}-<short-slug>"
gh pr create --base "${target_branch}" --title "<short title>" --body "$(cat <<EOF
## Summary
<1–3 bullets>

## Acceptance criteria (from task spec)
- [x] <criterion 1>
- [x] <criterion 2>

## Test plan
- [x] <what you ran and what it showed>

---
Generated by Kit's code-task skill. Task spec: \`~/.life/tasks/${TASK_ID}/spec.yaml\`.
EOF
)"
```

Capture the PR URL from `gh pr create`'s stdout.

### 4. Write result.json (write-once contract)

```json
{
  "task_id": "<task_id>",
  "status": "done",
  "completed_at": "<iso>",
  "pr_url": "https://github.com/<org>/<repo>/pull/<n>",
  "branch": "kit/<task_id>-<slug>",
  "files_changed": ["README.md"],
  "tests_run": ["npm test"],
  "tests_passed": true,
  "notes": "<anything the operator should know — drive-by observations, follow-ups worth filing>",
  "runtime_seconds": 412
}
```

If anything went wrong, `status: blocked` with a `blocker` field naming the cause (`tests_failed`, `time_budget_exceeded`, `target_repo_missing`, `auth_failed`, `merge_conflict`, `unclear_requirements`, `unknown_<short>`). Always include a `to_resume` field with a 1–2 sentence handoff so a human can pick it up.

### 5. Update the spec, then dag.yaml (if run-bound), then announce — IN THAT ORDER

**5a. Update spec.yaml.** Read `~/.openclaw/workspace/skills/task_update/SKILL.md` first for the rules. Then use the Edit tool to mutate the spec at `~/.life/tasks/<task_id>/spec.yaml` (or `~/.life/projects/.../tasks/<task_id>/spec.yaml` if run-bound):

```yaml
# change these EXACT fields (do not rewrite the whole file):
status: done                          # or 'blocked' on failure
completed_at: <iso8601 UTC, e.g. 2026-05-17T13:45:22Z>
result_summary: <one line, ≤200 chars, e.g. "PR opened: https://github.com/dsdevq/lifekit-stack/pull/3">
```

This is REQUIRED — without it the dispatch cron will re-pick the spec on its next tick and re-run forever. Also append a `spec_updated` event to the task's `run.log.jsonl`.

**5b. (run-bound only) Update the dag.yaml node.** If `run` is set in the spec, Read the dag.yaml at `~/.life/projects/<project>/runs/<run>/dag.yaml`, find your node (`id == run_node`), and use a SINGLE Edit-tool call to set:

```yaml
runner_status: claimed_done          # not 'done' — verifier flips to verified_done after independent check
completed_at: <iso>
evidence:
  tests_passed: <true|false>         # whatever you reported in result.json
  pr_url: <URL or null>
  files_changed: [<list>]
```

This is the SINGLE write you make to dag.yaml — per architecture §6.1 single-writer discipline. The Curator reconciles on its next heartbeat. Do NOT touch any other dag node, any other field, or status.yaml.

If `run` is NOT set (atomic task), SKIP step 5b entirely. Atomic tasks don't have a Run wrapper.

**5c. Announce via the shell.** Run via Bash (NOT the message-tool, NOT auto-announce — those are unreliable in cron-spawned sub-agent contexts on this stack):

```bash
# Read requester_route.to from the spec — that's the Telegram chat id.
CHAT_ID="<requester_route.to from spec.yaml>"
MSG="✅ <task_id> · PR: <url>"          # or "⚠️ <task_id> · blocked: <blocker>"
openclaw message send --channel telegram --target "$CHAT_ID" --message "$MSG"
```

The command must return `✅ Sent via telegram. Message ID: ...` — if it doesn't, append a `announce_failed` event to `run.log.jsonl` (DO NOT change spec status to blocked just because announce failed; the work IS done, the announce is best-effort).

**Run-bound tasks: do NOT announce per-task.** When `run` is set in the spec, your completion is one step inside a larger Run; the Curator announces the Run-completion once all nodes are `verified_done` (and escalates on the narrow §6.3 list). Per-task pings would flood the chat. SKIP step 5c entirely for run-bound tasks; just append a `node_completion_quiet` event to the run.log.jsonl.

No screenshots, no logs in the message. The detail lives in `result.json` and `run.log.jsonl`. Keep the chat surface clean.

## Failure modes and what to do

| Failure | Action |
|---|---|
| `git clone` fails (auth) | Block with `auth_failed`. Likely cause: `gh auth status` shows expired token. Don't try to re-auth automatically. |
| Tests fail after your changes | Try once to fix. If still failing after ~25% of budget, block with `tests_failed` and push the WIP branch anyway so the diff is reviewable. |
| Merge conflict on PR base | Block with `merge_conflict`. Don't rebase — that hides the conflict from the reviewer. |
| Pre-commit hook fails | Read the hook's complaint; fix the underlying issue. **Do not use `--no-verify`.** If you can't fix it within ~15% of remaining budget, block with `precommit_hook_failed`. |
| Time budget exceeded | Stop, push WIP, block with `time_budget_exceeded`, set `to_resume` with the next concrete step. |
| Spec ambiguity that materially changes the result | Make your best call, document the decision in `result.json.notes`, ship the PR. Don't loop trying to resolve. |

## What this skill is not

- Not for multi-day work — that's swarm (Phase 6).
- Not for cross-repo refactors — file one task per repo.
- Not for tasks requiring browser OAuth, manual dashboard clicks, or anything not scriptable.
- Not for repos the gh-auth principal can't push to. (If the target is a fork-and-PR workflow, that's a future enhancement; for now, the principal needs push access to a branch on the target repo.)

## Prereqs (the dispatcher should have verified these before invoking you)

- `gh auth status` returns OK
- `git config --get user.email` is non-empty
- The spec carries `target_repo`
- `/tmp/<task_id>/` has no leftover state (you `rm -rf` at the start, so this is self-healing)
