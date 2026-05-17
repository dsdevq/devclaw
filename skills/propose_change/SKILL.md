---
name: propose_change
description: "Draft an RFC-style design doc for non-atomic work on a known project. Lands at `~/.life/projects/<slug>/proposals-approved/<date>-<short-slug>.md`, announces on Telegram, and auto-invokes `define_run` so the work starts on the next Curator heartbeat. The operator's recourse is the resulting PR (merge or close) — no blocking on a `ship it` reply. The override path: operator may reply `cancel` / `hold` to abort before the next heartbeat, or `edit: <changes>` to re-draft before run definition. Triggers — `propose: ...`, `let's plan a change to X`, `draft an RFC for Y`, or `task_intake` routing here because the code task is non-atomic. NEVER invoke for atomic work (single-file typo fixes, one-line changes) — those bypass proposals entirely. NEVER invoke against a project missing `plan.md` (call `project_init` first)."
---

# propose_change

You are the PO/dev review boundary. Non-atomic code work — multi-file features, refactors, schema changes, anything that touches more than one concern — gets an RFC-style proposal so the design is on paper before code lands. This is Phase 5.7b's headline guardrail.

The default posture is **announce and proceed**: drafted proposals are auto-promoted to `proposals-approved/` and a Run is defined immediately. The whole point of devclaw is autonomous overnight work — blocking on an explicit human reply defeats that. The operator's review surface is the resulting PR (merge or close), not a chat acknowledgement.

Architecture: `~/.life/system/project-curator-architecture.md` §2.6 and "Human-in-loop posture".

## Hard behavioral rules

- **Announce and proceed by default.** A newly drafted proposal is auto-promoted to `proposals-approved/` and `define_run` is invoked in the same turn. The operator is *notified*, not *gated*. No waiting on a `ship it` reply.
- **One proposal per invocation.** Multi-proposal asks split into multiple invocations.
- **No proposal without an effort estimate AND evidenceable acceptance criteria.** Both are the contract; this skill REFUSES to draft without them.
- **Acceptance criteria must be evidenceable** — testable, gh-api-checkable, file-checkable. NOT "the code is cleaner." YES "BankSyncJobTest.StormScenario_RetainsMaxPlaidCallsUnder5 passes."
- **Refuse if project unknown.** If `~/.life/projects/<slug>/plan.md` is missing, refuse and route to `project_init`.
- **HARD-KEEP gate — `~/.life/domains/`.** If the proposed work touches `~/.life/domains/` directly (memory curator's sovereign surface), do NOT auto-promote. Draft into `proposals/`, announce, and wait for explicit `ship it`. Domain writes are the one place a human ack is still required.
- **HARD-KEEP gate — paid infrastructure.** If the proposed work touches paid infra (VPS deploy steps, paid GitHub Actions workflows, `openclaw.json` rewrites, any change with a non-trivial $ cost), do NOT auto-promote. Draft into `proposals/`, announce, and wait for explicit `ship it`. Money decisions stay human-in-the-loop.

## Three modes — figure out which

The skill is invoked for ONE of these three intents:

1. **Draft mode** — the operator asks for a new RFC. Default mode; covered in §A. Draft mode auto-promotes + defines a Run (unless a HARD-KEEP gate trips).
2. **Edit mode** — the operator replies `edit: <changes>` to an existing draft *before* the Run heartbeat fires, or against a HARD-KEEP draft sitting in `proposals/`. Covered in §B.
3. **Resolve mode** — the operator replies `ship it` (to release a HARD-KEEP draft), `cancel` / `hold` (to abort an auto-promoted draft before its Run starts), or `reject` to archive. Covered in §C.

If unclear, ask once. Don't guess between draft and edit.

---

## §A — Draft mode

### A.1 Resolve project + validate prerequisites

```bash
SLUG="<from intent, e.g. finance-sentry>"
PROJ_DIR="$HOME/.life/projects/$SLUG"

if [[ ! -f "$PROJ_DIR/plan.md" ]]; then
  # Refuse — project_init must run first.
  echo "Project '$SLUG' is unknown to Kit. Run project_init first (\`let's recon $SLUG\`)."
  exit 1
fi
```

Read `$PROJ_DIR/plan.md` and (if present) `$PROJ_DIR/recon.md`. The proposal's "Motivation" section MUST tie back to goals in plan.md.

### A.2 Decide gate posture + build the slug + path

First, decide whether a HARD-KEEP gate trips:

```bash
HARD_KEEP=false
# touches ~/.life/domains/ ?
# touches VPS deploy / paid GH workflow / openclaw.json ?
# → if yes: HARD_KEEP=true
```

Then build the path. Auto-promoted proposals (`HARD_KEEP=false`) skip `proposals/` and land directly in `proposals-approved/`. Gated proposals land in `proposals/` as before.

```bash
DATE=$(date -u +%Y-%m-%d)
SHORT_SLUG="<3–5 words, kebab-case, ≤ 40 chars, e.g. banksync-circuit-breaker>"

if [[ "$HARD_KEEP" == "true" ]]; then
  PROP_PATH="$PROJ_DIR/proposals/$DATE-$SHORT_SLUG.md"
else
  PROP_PATH="$PROJ_DIR/proposals-approved/$DATE-$SHORT_SLUG.md"
fi

if [[ -e "$PROP_PATH" ]]; then
  # Collision — append a random suffix.
  PROP_PATH="${PROP_PATH%.md}-$(openssl rand -hex 2).md"
fi
```

### A.3 Validate the ask before drafting

Refuse (do NOT create the file) if any of:

- The ask is genuinely atomic (single file, one-line change, typo). → reply: *"This is atomic — drop it directly via task_intake; proposals are for non-atomic work."*
- No evidenceable acceptance criteria can be derived. → reply: *"I can't write a proposal without testable acceptance criteria. What's the concrete signal that this is done?"*
- Effort is unbounded ("rewrite the world"). → reply: *"Scope is too open — narrow to one concrete change."*

### A.4 Draft the RFC

Write `$PROP_PATH` with this EXACT structure (no improvisation on section names — the operator reads many of these; consistency matters). The frontmatter `status:` reflects auto-promotion: `approved` when proceeding by default, `proposed` only when a HARD-KEEP gate trips.

```markdown
---
status: approved          # or 'proposed' if HARD_KEEP gate tripped
project: <slug>
drafted: <ISO8601 UTC>
approved: <ISO8601 UTC>   # same as drafted when auto-promoted; omit for HARD-KEEP
estimated_effort: <"X tasks, ~Y hours agent time">
---

# <Concise title — what changes>

## Motivation

<2–4 sentences. Tie to goals in plan.md. Quote one specific plan.md line if relevant.>

## What changes

<Bulleted list of concrete changes. Files, modules, behaviors. Be specific — name files where possible.>

## Step-by-step plan

<Numbered list. Each step is a future task (in Phase 5.7c these become DAG nodes). Note parallelism inline with "(parallel with N)".>

1. <step>
2. (parallel with 3) <step>
3. (parallel with 2) <step>
4. (depends on 1, 2, 3) <step>

## Impact on existing functionality

<What gets touched. What might break. What data is at risk. What rolls back cleanly. Be honest — proposals that minimize risk get rejected.>

## Risks + mitigations

- **Risk:** <thing that could go wrong>
  **Mitigation:** <how>
- **Risk:** …
  **Mitigation:** …

## Acceptance criteria

<Each criterion EVIDENCEABLE. Testable, gh-api-checkable, file-checkable. Number them.>

1. <criterion>
2. <criterion>
3. <criterion>

## Effort estimate

<X tasks, ~Y hours total agent time. Per-task breakdown if non-trivial.>

## Override controls (in chat)

Default flow is auto-promote and run. Operator overrides:

- `cancel` / `hold` → abort before the next Curator heartbeat (moves the proposal to `proposals-rejected/` and removes the pending Run; only works while Run status is still `pending`).
- `edit: <changes>` → only honored if the Run hasn't started yet, OR if this draft is sitting in `proposals/` because a HARD-KEEP gate tripped.
- `ship it` → only meaningful for HARD-KEEP drafts in `proposals/`; promotes them to `proposals-approved/` and defines the Run.
- Once the Run starts, the operator's recourse is the resulting PR (merge or close).
```

### A.5 Auto-promote, define the run, announce — IN THAT ORDER

If `HARD_KEEP=false` (default):

1. The file is already in `proposals-approved/` (per A.2). No move step needed.
2. Invoke `define_run` with `$PROP_PATH` as input. Read `~/.openclaw/workspace/skills/define_run/SKILL.md` first; follow its procedure. It writes `runs/<run-slug>/dag.yaml` + `status.yaml`. Capture its one-line confirmation.
3. Announce on Telegram via Bash (NOT the message tool):

   ```bash
   CHAT_ID="<requester chat id>"
   openclaw message send --channel telegram --target "$CHAT_ID" \
     --message "📝 Proposal: <title> · auto-approved · 🚀 Run defined: <slug>/<run-slug> · <N> tasks · Curator picks up on next heartbeat. Reply 'cancel' before then to abort; otherwise watch for the PR."
   ```

If `HARD_KEEP=true`:

1. The file is in `proposals/` with `status: proposed`. Do NOT move it. Do NOT invoke `define_run`.
2. Announce on Telegram:

   ```bash
   openclaw message send --channel telegram --target "$CHAT_ID" \
     --message "📝 Proposal (HARD-KEEP gate): <title> at <relative-path>. Touches <domain|paid infra>. Reply 'ship it', 'edit: <changes>', or 'reject'."
   ```

Do NOT enumerate the proposal back at the operator in chat — he reads the file. Be terse.

---

## §B — Edit mode

Triggered when the operator replies `edit: <changes>` referencing an existing draft.

### B.1 Find the proposal

The proposal is the most recent `proposed` doc in `~/.life/projects/*/proposals/` — OR the operator names it explicitly. If ambiguous (multiple in-flight proposals), ask which.

```bash
PROP_PATH="<resolved path>"

# Verify it's still 'proposed' — refuse if already approved or rejected.
grep -q '^status: proposed' "$PROP_PATH" || {
  echo "Proposal is no longer in 'proposed' state — refusing to edit. New proposals only at this point."
  exit 1
}
```

### B.2 Re-draft in place

Read the existing proposal. Apply the operator's requested edits. Re-write the file holistically (this is the ONE write-pattern where a full rewrite is fine — proposals are a single contract, not a streamed log).

Same structure, same frontmatter rules — but the `drafted:` field is **NOT updated** (preserve original). Add an `edited:` line under it.

### B.3 Reply

```
📝 Redrafted: <title>
   <relative-path>

Reply `ship it`, `edit: <more changes>`, or `reject`.
```

---

## §C — Resolve mode

Triggered by the operator replying `ship it`, `reject`, `cancel`, or `hold`. Under auto-proceed, this mode is the *override* path — most proposals never need it.

### C.1 `ship it` — release a HARD-KEEP draft

Only valid for drafts currently sitting in `proposals/` (i.e., a HARD-KEEP gate tripped at draft time). For already-auto-approved proposals, `ship it` is a no-op (reply "already approved and dispatched").

```bash
PROP_PATH="<resolved path under proposals/>"
SLUG=$(basename "$(dirname "$(dirname "$PROP_PATH")")")
FNAME=$(basename "$PROP_PATH")
APPROVED_PATH="$HOME/.life/projects/$SLUG/proposals-approved/$FNAME"
```

Use Edit tool to change `status: proposed` → `status: approved` in the frontmatter. Add an `approved:` ISO timestamp under `drafted:`. Then `mv` the file:

```bash
mv "$PROP_PATH" "$APPROVED_PATH"
```

**Then invoke `define_run`** (Phase 5.7c handoff). Read `~/.openclaw/workspace/skills/define_run/SKILL.md` first; follow its procedure with `$APPROVED_PATH` as input. Reply:

```
✅ Approved: <title>
   moved → ~/.life/projects/<slug>/proposals-approved/<fname>

🚀 Run defined: <slug>/<run-slug> · <N> tasks · Curator picks up on next heartbeat (≤30 min).
```

If `define_run` refuses (idempotency, un-evidence-able step, cycle in dependencies, missing target_repo), surface its refusal verbatim and DON'T move the proposal back. The approval stands; the Run definition is what needs fixing (likely a proposal `edit:` round).

### C.2 `cancel` / `hold` — abort an auto-promoted Run before it starts

Valid only while the Run is still `pending` (i.e., Curator hasn't dispatched its first task yet). Once the first task has been dispatched, the operator's recourse is the resulting PR (close it).

```bash
PROP_PATH="<auto-approved proposal in proposals-approved/>"
RUN_DIR="<corresponding runs/<run-slug>/>"
```

Verify `status.yaml` shows `pending` / no `in_flight` tasks. Then:

1. Edit the proposal frontmatter: `status: approved` → `status: cancelled`. Add `cancelled:` ISO timestamp.
2. `mv` proposal → `proposals-rejected/` (history kept).
3. Delete the run dir contents (or rename to `<run-slug>.cancelled/`) so Curator stops scanning it.

Reply:

```
🛑 Cancelled: <title>
   Run <slug>/<run-slug> aborted before dispatch.
```

### C.3 `reject` — archive an un-started HARD-KEEP draft

```bash
REJECTED_PATH="$HOME/.life/projects/$SLUG/proposals-rejected/$FNAME"
```

Edit `status: proposed` → `status: rejected`. Add `rejected:` ISO timestamp. `mv` to `proposals-rejected/`.

Reply:

```
❌ Rejected: <title>
   archived → ~/.life/projects/<slug>/proposals-rejected/<fname>
```

Rejected proposals are KEPT (not deleted) — the history is valuable.

---

## Heuristics for sizing — atomic vs proposal-worthy

This is the call `task_intake` is supposed to make, but it also matters here for refusal logic. Atomic = single concern, ≤ 1 hour, trivially reviewable diff:

| Signal | Verdict |
|---|---|
| One file, one logical change (typo, rename, version bump, single-line config) | **atomic** |
| New endpoint with tests, single module | **borderline — the operator decides** |
| Touches multiple modules / changes contracts / migrates schema | **proposal-worthy** |
| Introduces a new dependency, framework, or service | **proposal-worthy** |
| Removes/replaces existing functionality | **proposal-worthy** |
| "Refactor X" — even single-module | **proposal-worthy** (refactors hide impact) |
| Affects security/auth/data flow | **proposal-worthy** |

When in doubt, draft the proposal. the operator reading an over-cautious RFC and saying "this was atomic, just do it" is fine. The reverse — silent multi-module changes — is the failure mode this skill exists to prevent.

## What this skill is not

- Not for atomic work. Atomic = `task_intake` directly.
- Not for projects without `plan.md`. Run `project_init` first.
- Not for running the work — this skill drafts the proposal and (when not HARD-KEEP) hands off to `define_run` / Curator. The actual code changes happen in the runners.
- Not for editing approved proposals. Once auto-promoted or `ship it`-approved, the proposal is **locked**. Scope changes need a new proposal. Per architecture §9 open question 4.

## Failure modes

| Failure | Action |
|---|---|
| Project has no plan.md | Refuse. Route to `project_init`. |
| Acceptance criteria un-evidenceable | Refuse with example of an evidenceable rewrite. |
| Proposal collision on path | Append random hex suffix. |
| `define_run` fails during auto-promotion | Surface its refusal verbatim. Leave the proposal in `proposals-approved/` (no rollback) so the operator can `edit:` and retry. |
| `ship it` on a proposal that doesn't exist (typo, race) | Reply: "Couldn't find a proposed RFC in <slug>'s `proposals/` — name the file explicitly?" |
| `cancel` after Curator already dispatched | Refuse: "Run has started — close the resulting PR instead." |
| `edit:` on an already-approved or auto-promoted proposal whose Run has started | Refuse: "Approved proposals are locked once the Run starts. New proposal needed for scope changes." |
