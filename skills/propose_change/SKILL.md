---
name: propose_change
description: "Draft an RFC-style design doc for non-atomic work on a known project. Lands at `~/.life/projects/<slug>/proposals/<date>-<short-slug>.md` and is Telegram-posted for the operator's review. Also handles the operator's reply — `ship it` moves the doc to `proposals-approved/`, `edit: <changes>` re-drafts in place, `reject` moves to `proposals-rejected/`. Triggers — `propose: ...`, `let's plan a change to X`, `draft an RFC for Y`, or `task_intake` routing here because the code task is non-atomic. NEVER invoke for atomic work (single-file typo fixes, one-line changes) — those bypass proposals entirely. NEVER invoke against a project missing `plan.md` (call `project_init` first)."
---

# propose_change

You are the PO/dev review boundary. Non-atomic code work — multi-file features, refactors, schema changes, anything that touches more than one concern — does NOT run autonomously without an approved proposal sitting in `proposals-approved/`. This is Phase 5.7b's headline guardrail.

Architecture: `~/.life/system/project-curator-architecture.md` §2.6.

## Hard behavioral rules

- **One proposal per invocation.** Multi-proposal asks split into multiple invocations.
- **No proposal without an effort estimate AND evidenceable acceptance criteria.** Both are the contract; this skill REFUSES to draft without them.
- **Acceptance criteria must be evidenceable** — testable, gh-api-checkable, file-checkable. NOT "the code is cleaner." YES "BankSyncJobTest.StormScenario_RetainsMaxPlaidCallsUnder5 passes."
- **Never approve your own draft.** Only the operator's `ship it` reply moves a file from `proposals/` → `proposals-approved/`.
- **Refuse if project unknown.** If `~/.life/projects/<slug>/plan.md` is missing, refuse and route to `project_init`.

## Three modes — figure out which

The skill is invoked for ONE of these three intents:

1. **Draft mode** — the operator asks for a new RFC. Default mode; covered in §A.
2. **Edit mode** — the operator replies `edit: <changes>` to an existing draft. Covered in §B.
3. **Resolve mode** — the operator replies `ship it` or `reject` to an existing draft. Covered in §C.

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

### A.2 Build the slug + path

```bash
DATE=$(date -u +%Y-%m-%d)
SHORT_SLUG="<3–5 words, kebab-case, ≤ 40 chars, e.g. banksync-circuit-breaker>"
PROP_PATH="$PROJ_DIR/proposals/$DATE-$SHORT_SLUG.md"

if [[ -e "$PROP_PATH" ]]; then
  # Collision — append a random suffix.
  PROP_PATH="$PROJ_DIR/proposals/$DATE-$SHORT_SLUG-$(openssl rand -hex 2).md"
fi
```

### A.3 Validate the ask before drafting

Refuse (do NOT create the file) if any of:

- The ask is genuinely atomic (single file, one-line change, typo). → reply: *"This is atomic — drop it directly via task_intake; proposals are for non-atomic work."*
- No evidenceable acceptance criteria can be derived. → reply: *"I can't write a proposal without testable acceptance criteria. What's the concrete signal that this is done?"*
- Effort is unbounded ("rewrite the world"). → reply: *"Scope is too open — narrow to one concrete change."*

### A.4 Draft the RFC

Write `$PROP_PATH` with this EXACT structure (no improvisation on section names — the operator reads many of these; consistency matters):

```markdown
---
status: proposed
project: <slug>
drafted: <ISO8601 UTC>
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

## Reply in chat to advance

- `ship it` → moves to `proposals-approved/`, will define a Run in Phase 5.7c
- `edit: <changes>` → Kit redrafts in place
- `reject` → moves to `proposals-rejected/`
```

### A.5 Reply to the operator

```
📝 Proposal drafted: <title>
   <relative-path-from-home>

Reply `ship it`, `edit: <changes>`, or `reject` when you've read it.
```

Do NOT enumerate the proposal back at him in chat — he reads the file. Be terse.

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

Triggered by the operator replying `ship it`, `reject`, OR explicit cancel.

### C.1 `ship it` — approve

```bash
PROP_PATH="<resolved path>"
SLUG=$(basename "$(dirname "$(dirname "$PROP_PATH")")")
FNAME=$(basename "$PROP_PATH")
APPROVED_PATH="$HOME/.life/projects/$SLUG/proposals-approved/$FNAME"

# Single-writer move; flip status in frontmatter first, then move.
# (Editing then moving is the atomic-enough pattern for a markdown file.)
```

Use Edit tool to change `status: proposed` → `status: approved` in the frontmatter. Add an `approved:` ISO timestamp under `drafted:`. Then `mv` the file:

```bash
mv "$PROP_PATH" "$APPROVED_PATH"
```

**Then invoke `define_run`** (Phase 5.7c handoff). Read `~/.openclaw/workspace/skills/define_run/SKILL.md` first; follow its procedure with `$APPROVED_PATH` as input. It writes `runs/<run-slug>/dag.yaml` + `status.yaml` and returns a one-line Run-defined confirmation. Concatenate that into your reply so the operator sees both events:

```
✅ Approved: <title>
   moved → ~/.life/projects/<slug>/proposals-approved/<fname>

🚀 Run defined: <slug>/<run-slug> · <N> tasks · Curator picks up on next heartbeat (≤30 min).
```

If `define_run` refuses (idempotency — run already exists; un-evidence-able step; cycle in dependencies; missing target_repo), surface its refusal verbatim and DON'T move the proposal back from `proposals-approved/`. The approval stands; the Run definition is what needs fixing (likely a proposal `edit:` round).

### C.2 `reject`

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
- Not for running the work — even after approval, Phase 5.7b stops at "approved doc in proposals-approved/". Curator (5.7c) takes over from there.
- Not for editing approved proposals. Once approved, the proposal is **locked**. Scope changes need a new proposal. Per architecture §9 open question 4.

## Failure modes

| Failure | Action |
|---|---|
| Project has no plan.md | Refuse. Route to `project_init`. |
| Acceptance criteria un-evidenceable | Refuse with example of an evidenceable rewrite. |
| Proposal collision on path | Append random hex suffix. |
| `ship it` on a proposal that doesn't exist (typo, race) | Reply: "Couldn't find a proposed RFC in <slug>'s `proposals/` — name the file explicitly?" |
| `edit:` on an already-approved proposal | Refuse: "Approved proposals are locked. New proposal needed for scope changes." |
