---
name: research-task
description: "Execute a bounded `kind: research|draft|chore` Task Spec — research deeply, draft documents, or do focused chores against acceptance criteria. Writes artifacts to `~/.life-state/tasks/<id>/output/` and (when the work proposes domain mutations) patches to `~/.life-state/.curator-proposed/`. Triggered by the task_dispatch skill OR invoked directly with a spec path. Mirror of `code-task` for non-code work. Not for multi-day work."
---

# research-task

You are running a bounded autonomous **research, draft, or chore** task. The Task Spec at the path you've been given is your full input. You are NOT in conversation — the operator may be asleep. The only output he sees is what you write to `result.json` and the announce message at the end.

## Hard behavioral rules

- **Never edit `~/.life/domains/` directly.** Curator owns that surface. If your work concludes with a domain mutation, write a patch under `~/.life-state/.curator-proposed/<task_id>.patch` and append an event to `~/.life-state/queue.jsonl` so curator picks it up.
- **Never edit spec.yaml.** Read the `task_update` skill before changing anything in `~/.life-state/tasks/<id>/`. The only files you create directly are inside `~/.life-state/tasks/<id>/output/`, plus the one-shot `result.json` and append-only `run.log.jsonl`.
- **All scratch work happens in `/tmp/<task_id>/`.** Don't clutter `~/` or `~/.life/`.
- **Stay inside the time budget.** The spec carries `budget.max_runtime_seconds`. If you're approaching the limit, stop, save partial progress to `output/PARTIAL.md`, and write `result.json` with `status: blocked`, `blocker: time_budget_exceeded`, and a clear `to_resume` note.
- **No clarifying questions.** If the spec is ambiguous, make a defensible call and document it in `result.json.notes`.

## Inputs

When invoked, you'll be given a path like `~/.life-state/tasks/<task_id>/spec.yaml`. The frontmatter you care about:

```yaml
task_id: 2026-05-17-<slug>-<rand>
kind: research                          # research | draft | chore
verbatim_intent: |
  <what the operator actually said>
acceptance_criteria:
  - <bullet 1>
  - <bullet 2>
budget:
  max_runtime_seconds: 7200             # your hard cap
output_destination: optional            # one of:
                                        #   inline       — content in result.json.output
                                        #   file:<path>  — write to that exact path
                                        #   curator      — patch to .curator-proposed/
                                        #   tasks-output — file under ~/.life-state/tasks/<id>/output/ (DEFAULT)
```

If the spec is missing `kind` or the kind isn't `research|draft|chore`, write `result.json` with `status: blocked`, `blocker: unsupported_kind`, and stop — don't try to do code work; that's `code-task`.

## Execution sequence

### 1. Set up

```bash
TASK_ID="<from spec.yaml>"
SCRATCH="/tmp/${TASK_ID}"
mkdir -p "$SCRATCH"
mkdir -p ~/.life-state/tasks/${TASK_ID}/output
```

Append to `run.log.jsonl`:
```json
{"ts":"<iso>","actor":"research-task","event":"started","kind":"<kind>","scratch":"/tmp/<id>"}
```

### 2. Do the work

Use Read for `~/.life/` context, WebFetch for external sources, Write/Edit for drafts, Bash for any shell work. Patterns by kind:

- **research**: start by listing the questions in `verbatim_intent`. For each, gather sources, weigh them, synthesize. Don't just paste search results — write a coherent answer. Cite sources at the end.
- **draft**: read context from `~/.life/` (commitments, engineering, career — whichever domain is relevant). Match the operator's writing voice (terse, direct, no preamble). Default output format: Markdown.
- **chore**: bounded mechanical work — file rename, log audit, format normalization, etc. Be explicit about what you changed; don't drift into adjacent cleanups.

Append meaningful events to `run.log.jsonl` as you go (`event: source_fetched`, `event: section_drafted`, `event: replan`). This is the audit trail.

### 3. Write outputs

Where the result lands depends on the spec's `output_destination` (default: `tasks-output`):

- **`tasks-output`** (default): write your artifact(s) to `~/.life-state/tasks/<task_id>/output/<filename>.md` (or `.json`, `.yaml` — match the content). Reference each file in `result.json.artifacts`.
- **`inline`**: put the full content in `result.json.output` (string field). Use for short results that don't deserve a file.
- **`file:<path>`**: write to the absolute path the spec specifies. Path must be inside `~/.life/` — refuse otherwise.
- **`curator`**: write a unified-diff patch to `~/.life-state/.curator-proposed/<task_id>.patch` AND append a notification to `~/.life-state/queue.jsonl`:
   ```json
   {"ts":"<iso>","actor":"research-task","event":"curator_patch_proposed","task_id":"<id>","patch_path":".curator-proposed/<id>.patch"}
   ```

### 4. Write result.json (write-once)

```json
{
  "task_id": "<task_id>",
  "kind": "<kind>",
  "status": "done",
  "completed_at": "<iso>",
  "artifacts": ["~/.life-state/tasks/<id>/output/research.md"],
  "sources_consulted": ["https://...", "..."],
  "notes": "<defensible-call documentation, drive-by observations, follow-ups>",
  "runtime_seconds": 1832
}
```

On failure: `status: blocked`, plus `blocker` field naming the cause from this enum:
- `time_budget_exceeded`
- `unsupported_kind`
- `sources_unreachable`
- `output_destination_invalid`
- `acceptance_criteria_unattainable`
- `unknown_<short>`

Always include `to_resume` with 1–2 sentences naming the next concrete step a human (or a fresh invocation) could take.

### 5. Update the spec, then announce — IN THAT ORDER

**5a. Update spec.yaml.** Read `~/.openclaw/workspace/skills/task_update/SKILL.md` first for the rules. Then use the Edit tool to mutate the spec at `~/.life-state/tasks/<task_id>/spec.yaml`:

```yaml
# change these EXACT fields (do not rewrite the whole file):
status: done                          # or 'blocked' on failure
completed_at: <iso8601 UTC>
result_summary: <one line, ≤200 chars>
```

REQUIRED — without it the dispatch cron will re-pick the spec on its next tick and re-run forever. Also append a `spec_updated` event to `~/.life-state/tasks/<task_id>/run.log.jsonl`.

**5b. Announce via the shell.** Run via Bash (NOT the message-tool, NOT auto-announce — those are unreliable in cron-spawned sub-agent contexts on this stack):

```bash
CHAT_ID="<requester_route.to from spec.yaml>"
MSG="✅ <task_id> · <result_summary>"   # or "⚠️ <task_id> · blocked: <blocker> · <to_resume>"
openclaw message send --channel telegram --target "$CHAT_ID" --message "$MSG"
```

The command must return `✅ Sent via telegram. Message ID: ...` — if it doesn't, append `announce_failed` event to `run.log.jsonl` (DO NOT change spec status to blocked just because announce failed; the work IS done, the announce is best-effort).

No screenshots, no logs in chat. Keep the chat surface clean — detail lives in `result.json`.

## Patterns by kind

### Research — "compare three Hetzner ARM SKUs that can run Whisper + Ollama"

1. List the questions. (price? RAM? vCPU? ARM-vs-x86 binary support? Whisper+Ollama memory footprint?)
2. WebFetch Hetzner pricing + each SKU spec. Note prices in EUR.
3. Cross-check with the Whisper and Ollama docs for RAM/CPU minimums.
4. Synthesize: rank, justify, recommend.
5. Output: `~/.life-state/tasks/<id>/output/comparison.md` with table + 1-paragraph recommendation.

### Draft — "blog post on lifekit, ~1200 words, voice matching the existing PLAN.md"

1. Read `~/projects/lifekit/README.md`, `~/.life/PLAN.md`, the existing blog-draft.md (per CLAUDE.md it's already committed).
2. Identify the operator's voice patterns from PLAN.md (terse, direct, no academic preamble).
3. Outline → draft → tighten.
4. Output: `~/.life-state/tasks/<id>/output/blog-draft.md`.

### Chore — "audit ~/.life/system/ for orphaned files referenced by nothing"

1. List files in `~/.life/system/`.
2. Grep `~/.life/` for references to each.
3. Build a report: file → refs (or "no refs found").
4. Output: `~/.life-state/tasks/<id>/output/orphan-audit.md`. Don't auto-delete — propose, don't act.

## Failure modes

| Failure | Action |
|---|---|
| WebFetch returns 403/404 for a key source | Try one alternative; if still failing, note it in `result.json.sources_consulted` with a `(unreachable)` flag and proceed with what you have. |
| Acceptance criteria genuinely unattainable (e.g. "find data that doesn't exist") | Block with `acceptance_criteria_unattainable`, document why in notes. |
| Output exceeds practical size | Split into multiple files under `output/`, reference all in `artifacts`. |
| Spec asks for code work (you got a misclassified task) | Block with `unsupported_kind: code` — the dispatcher should route to `code-task` next time. |

## What this skill is not

- Not for code work — that's `code-task`.
- Not for multi-day deep research — bound to single-session at v1 (no checkpointed resume across runs).
- Not for direct mutation of `~/.life/domains/` — curator only.
