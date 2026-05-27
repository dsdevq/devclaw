---
name: task_intake
description: "Turn a natural-language intent from the operator into a fully-formed Task Spec at `~/.life-state/tasks/<id>/spec.yaml`. Invoke when the operator says something like 'overnight: ...', 'task: ...', 'tonight please do X', 'log a task to ...', 'queue up: ...', OR when his conversational turn is unambiguously a delegation rather than a chat (e.g. 'research X and draft Y', 'implement Z in repo W'). NEVER use this skill for chitchat, ambient questions, food logs, or anything that doesn't have a clear deliverable. After writing the spec, confirm with the operator in one short line."
---

# task_intake

You are turning the operator's natural-language intent into a structured Task Spec that the autonomous-overnight pipeline (`task_dispatch` cron) can pick up and execute. You are the **only** writer of new task directories. After this skill, control returns to the operator's chat.

## Hard behavioral rules

- **One task per invocation.** If the operator's intent contains multiple distinct deliverables, ask him to split them (or split them yourself and create multiple specs — but be explicit).
- **You write spec.yaml ONCE.** No updates. Changes go through `task_update`.
- **The `task_id` is permanent.** Format: `YYYY-MM-DD-<short-slug>-<rand4hex>`. Generate `rand4hex` with `openssl rand -hex 2`.
- **The `requester_route` MUST be captured.** Without it, the runner can't announce back. Read it from the current channel context (Telegram chat id, account id).
- **Classify `kind` accurately.** This drives routing. The five values:
  - `code` — modify a code repo, open a PR. Routed to `code-task`.
  - `research` — gather info, synthesize, produce a report. Routed to `research-task`.
  - `draft` — write a document (blog post, plan, response, etc.). Routed to `research-task`.
  - `chore` — bounded mechanical work (audit, format, file ops). Routed to `research-task`.
  - `decision` — needs the operator's choice; surfaces to Telegram, no autonomous run. Use sparingly.
- **No hidden assumptions.** Put every interpretation in `verbatim_intent` (raw text) AND in `acceptance_criteria` (your parsed version). The dispatcher and runner will read both.
- **Project gate (Phase 5.7a).** For `kind: code`, the target project MUST be known (`~/.life/projects/<slug>/plan.md` exists) OR the task qualifies for Option B stub mode (atomic-on-unknown). For known projects with non-atomic code work, an approved proposal in `~/.life/projects/<slug>/proposals-approved/` is REQUIRED. See §0 below.

## Inputs you receive

the operator's natural-language message. Example: *"Overnight, in dsdevq/lifekit-stack, add a STATUS.md file noting which phases of the architecture are live. Open a PR."*

You also have the channel context — the current Telegram chat id, account id. You'll capture these in `requester_route`.

## Sequence

### 0. Project gate (Phase 5.7a — code tasks only)

**Skip this section entirely for `kind` other than `code`.** Research/draft/chore/decision tasks don't need a project context yet. (Future Phase may bind them too; not now.)

For `kind: code`:

#### 0.1 Derive the slug

```
target_repo = "dsdevq/lifekit-stack"  ⇒  slug = "lifekit-stack"
target_repo = "myorg/some-tool"        ⇒  slug = "some-tool"
```

Strip the org prefix. Lowercase. Project slug = repo name only. This must match whatever `project_init` would have created.

#### 0.2 Check project state

```bash
SLUG="<derived>"
PROJ_DIR="$HOME/.life/projects/$SLUG"

PLAN_EXISTS=$( [[ -f "$PROJ_DIR/plan.md" ]] && echo yes || echo no )
RECON_EXISTS=$( [[ -f "$PROJ_DIR/recon.md" ]] && echo yes || echo no )
APPROVED_COUNT=$(ls -1 "$PROJ_DIR/proposals-approved/" 2>/dev/null | wc -l)
```

#### 0.3 Classify the work — atomic vs non-atomic

Atomic = single concern, single file (or trivially few), ≤ 30 min budget, diff is reviewable at a glance. Examples: typo fix, version bump, single-line config change, dependency bump, rename one identifier in one file.

Non-atomic = anything else. Multi-file, schema/contract changes, refactors (even single-module), new dependencies, security-touching, anything where impact isn't trivially visible from the diff.

When in doubt, classify as non-atomic. The cost of over-cautious (drafting a proposal the operator then says "just do it" to) is low. The cost of under-cautious (autonomous multi-module change with no design review) is the failure mode this whole architecture exists to prevent.

Heuristic: `budget.max_runtime_seconds ≤ 1800` (30 min) is a *necessary* condition for atomic, but not sufficient — you still apply the diff-shape test above.

#### 0.4 Decide — gate, stub, or proceed

Four cases:

| Project known? | Atomic? | Approved proposal exists? | Action |
|---|---|---|---|
| Yes (plan.md present) | Yes | — | **Proceed** to step 1. |
| Yes | No | Yes | **Proceed** — record `proposal_path` in spec frontmatter. |
| Yes | No | No | **REFUSE.** Reply: *"This isn't atomic — needs a proposal first. Use `propose: <ask>` against `<slug>`."* |
| No (plan.md missing) | Yes | — | **Option B stub** — create a stub project, then proceed (§0.5). |
| No | No | — | **REFUSE.** Reply: *"`<slug>` isn't known to Kit yet AND this isn't atomic. Run `/recon <repo>` first, then drop the task."* |

#### 0.5 Option B — stub mode for atomic-on-unknown

For atomic code work on an unknown project, auto-create the minimal project artifact and proceed. This is the ONE exception to the "no execution without understanding" rule and exists because forcing recon-before-typo-fix is friction without value.

```bash
mkdir -p "$PROJ_DIR/proposals" "$PROJ_DIR/proposals-approved" "$PROJ_DIR/proposals-rejected" "$PROJ_DIR/runs" "$PROJ_DIR/tasks"

cat > "$PROJ_DIR/plan.md" <<EOF
---
slug: $SLUG
created: $(date -u +%Y-%m-%dT%H:%M:%SZ)
mode: stub
---

# Project: $SLUG

Auto-created stub for atomic work. No recon performed yet — Kit understands this project only by its name. Atomic-only mode is implied; non-atomic work against this project will be REFUSED by \`task_intake\` until \`project_init\` runs.

Run \`/recon $SLUG\` to upgrade this stub to a fully-recon'd project.
EOF

cat > "$PROJ_DIR/settings.yaml" <<EOF
github_repo: $TARGET_REPO
github_visibility: unknown
mirror_to_issues: false
default_base_branch: main
test_command: null
notes: "Stub project — created by task_intake Option B. Recon not yet performed."
EOF
```

Note this in the spec.yaml `notes` field: `"stub project — no recon"`. Note it in the reply to the operator.

#### 0.6 Recording the proposal binding

If the work is non-atomic-with-approved-proposal, record the binding in the spec:

```yaml
project: <slug>
proposal_path: ~/.life/projects/<slug>/proposals-approved/<filename>.md
```

This lets the runner (and, in 5.7c, the verifier) load the proposal context.

### 1. Parse the intent

Extract:
- **What's the deliverable?** Be concrete. ("A PR adding STATUS.md with phase status.")
- **Where does it live?** For code: which repo. For research/draft: where does the output go (default: `~/.life-state/tasks/<id>/output/`).
- **What constitutes done?** Reframe as 2–5 acceptance criteria, each a single testable bullet.
- **What's the rough size?** Pick a `budget.max_runtime_seconds`:
  - small chore / typo fix: 900 (15 min)
  - small feature / one-file refactor / focused research: 1800 (30 min)
  - draft / multi-file change / broader research: 3600 (1 hour)
  - bigger feature / dense research: 7200 (2 hours)
  - anything bigger: 14400 (4 hours) — and flag in `notes` that this is at the edge of Phase 5.5; consider whether it should be `kind: code-heavy` for swarm later

### 2. Classify kind

Use this decision table. When in doubt, prefer the lower-risk classification.

| Signal | kind |
|---|---|
| "implement", "add to repo", "open a PR", "fix bug in", "refactor", names a code repo | **code** |
| "research", "compare", "evaluate", "investigate", "find out", "survey" | **research** |
| "write", "draft", "compose", "outline", "blog post", "email", "memo" | **draft** |
| "audit", "list", "clean up", "rename", "reorganize", file/dir mechanical work | **chore** |
| "should I…?", "decide between…", "pick one of…" — explicit ask for the operator's opinion | **decision** |

### 3. Build the spec.yaml

```yaml
task_id: <YYYY-MM-DD>-<short-slug>-<rand4hex>
created_at: <iso8601 UTC>
created_by: vps-kit
requester_route:
  channel: telegram
  to: "<chat_id>"
  account_id: "<account_id>"
verbatim_intent: |
  <the operator's exact words, including any prefatory framing he gave>
kind: <code|research|draft|chore|decision>
acceptance_criteria:
  - <criterion 1 — testable>
  - <criterion 2>
  - <criterion 3>
budget:
  max_runtime_seconds: <one of: 900|1800|3600|7200|14400>
# kind-specific fields (only include when relevant)
target_repo: <org/repo>            # REQUIRED for kind: code
target_branch: main                # default 'main' for kind: code; override if spec'd
project: <slug>                    # REQUIRED for kind: code (Phase 5.7a) — slug = repo name without org
proposal_path: <path|null>         # set for non-atomic code with approved proposal; null otherwise
output_destination: tasks-output   # for research/draft/chore: tasks-output|inline|file:<path>|curator
# mutation-controlled fields (initial state — task_update owns transitions)
status: ready
dispatch_target: null
dispatch_run_id: null
dispatched_at: null
completed_at: null
result_summary: null
```

### 4. Validate before writing

Reject (don't create the spec) if any of:
- `kind: code` and `target_repo` missing → ask the operator "which repo?"
- `kind: code` and the project gate (§0) refused → don't create the spec; the refusal reply already told the operator what to do
- `kind` can't be classified from the intent → ask the operator to clarify
- The intent is clearly chat-not-task ("how was your day?") → don't invoke this skill at all; you shouldn't be here
- The deliverable is irreversibly destructive (delete production data, force-push to main, etc.) → refuse, explain why

### 5. Write the spec

```bash
TASK_ID="<from above>"
mkdir -p ~/.life-state/tasks/$TASK_ID
# Write spec.yaml using Write tool with the YAML content above.
```

Append the first event to `~/.life-state/tasks/<task_id>/run.log.jsonl`:
```json
{"ts":"<iso>","actor":"task_intake","event":"spec_created","kind":"<kind>","budget_seconds":<n>}
```

### 6. Confirm with the operator (single short line)

Reply to him exactly:
```
📋 task <task_id> · kind=<kind> · budget=<n>m · will run on next dispatch tick (≤15 min).
```

For Option B stub creations, add a second line:
```
📋 task <task_id> · kind=code · budget=<n>m · will run on next dispatch tick (≤15 min).
⚠ stub project — no recon performed. Run /recon <slug> anytime to upgrade.
```

That's it. Do NOT enumerate the acceptance criteria back at him — they're in the spec, and he can `cat` it. Do NOT ask "anything else?" — be terse.

If you rejected (validation failure or project gate), reply with a single line explaining what's missing and exit without creating the spec.

## Examples

### Code task
the operator: *"Overnight, in dsdevq/finance-sentry, add a /health endpoint to the API that returns { status: 'ok', git_sha, uptime_seconds }. Open a PR with tests."*
→ spec: `kind: code`, `target_repo: dsdevq/finance-sentry`, budget 3600, ac: [endpoint exists, returns the three fields, unit test passes, PR opened against main]
→ reply: `📋 task 2026-05-17-finance-sentry-health-endpoint-3f1a · kind=code · budget=60m · will run on next dispatch tick (≤15 min).`

### Research task
the operator: *"Research what the cheapest Hetzner ARM SKU is that can run Whisper-large plus Ollama Gemma2 at the same time. Write it as a proposal under ~/.life/system/proposals.md."*
→ spec: `kind: research`, `output_destination: curator` (since it proposes a domain-adjacent file edit), budget 3600, ac: [3+ SKUs compared on price/RAM/CPU, recommendation chosen, patch landed in .curator-proposed/]
→ reply: `📋 task 2026-05-17-hetzner-whisper-ollama-research-91bc · kind=research · budget=60m · will run on next dispatch tick (≤15 min).`

### Decision
the operator: *"Should I migrate from OpenClaw to Hermes Agent, or stay?"*
→ spec: `kind: decision`, ac: [criteria for the decision documented, runners surface to Telegram, no autonomous resolution]
→ reply: `📋 task 2026-05-17-openclaw-vs-hermes-decision-5d22 · kind=decision · will surface on next dispatch tick (≤15 min).`

### Rejection example
the operator: *"Add a feature."*
→ no spec, reply: `Need more: which repo, what feature, what's "done" look like? I'll create the task when you tell me.`

## What this skill is not

- Not for status updates ("how's task X going?") — that's just chat; the operator can `cat result.json`.
- Not for editing existing tasks — see `task_update`.
- Not for ambient capture (food logs, mood notes) — those go to `~/.life-state/queue.jsonl` directly via the curator pattern, not as autonomous tasks.
