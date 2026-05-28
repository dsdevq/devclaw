---
name: project_init
description: "Gatekeeper for understanding a project before Kit acts on it. Two arms. Existing-repo arm — clone the repo, map the architecture, write `recon.md` AND `plan.md` in the same turn, documenting any defensible assumptions Kit made. New-project arm — Socratic mode, bounded to AT MOST ONE clarifying question (and only when scope is genuinely ambiguous); otherwise pick the strongest defensible reading and write `plan.md` immediately. The operator's correction surface is a one-reply edit, not a multi-turn questionnaire. ALWAYS the first thing called when the operator references a project Kit doesn't yet know AND wants Kit to do non-trivial work on it. Also invoked when `task_intake` refused a code task because the project lacks `recon.md`/`plan.md`. Triggers — `/recon <repo>`, `let's start a new project: X`, `let's set up X`, `we should plan out Y`. NEVER use for known projects (check `~/.life/projects/<slug>/` first)."
---

# project_init

You are the gate. No autonomous code-shaped work runs against a project until Kit understands it. That understanding lives in `~/.life/projects/<slug>/plan.md` (always) plus `recon.md` (for existing repos). Your job is to produce those artifacts — honestly, not as a rubber-stamp.

This is Phase 5.7a's first deliverable. Architecture: `~/.life/system/project-curator-architecture.md` §2.8.

## Hard behavioral rules

- **AT MOST one clarifying question, and only when genuinely ambiguous.** "Genuinely ambiguous" means: no `target_repo` AND scope is internally contradictory (e.g. operator references "lifekit but actually let's rewrite it"). Otherwise, **proceed straight to `plan.md`** using the strongest defensible interpretation. Document the interpretation in `plan.md`'s "Assumptions" section so the operator can correct course in one reply. This applies to BOTH arms — Socratic Q&A is no longer a multi-turn ceremony; it is at most one question.
- **One project per invocation.** If the operator references two projects in the same message, ask which to recon first (this counts as the one clarifying question).
- **Never overwrite an existing `plan.md` or `recon.md`.** If they exist, refuse and direct the operator to edit by hand (or invoke a future `project_update` skill — does not yet exist).
- **Slugify deterministically.** For `dsdevq/lifekit-stack` → slug `lifekit-stack` (strip org). For new projects, derive a slug from the operator's wording (lowercase, kebab-case, ≤ 32 chars). Don't ask — just pick one and record it in `plan.md`.
- **HARD-KEEP gate — `~/.life/domains/`.** `project_init` never writes to `~/.life/domains/`. If reconning a project clearly implies a domain file change (e.g. "this is now my primary nutrition tracker"), do NOT touch the domain file — leave a note in `plan.md`'s "Domain implications" section and let the memory curator pick it up.
- **HARD-KEEP gate — paid infrastructure.** `project_init` never edits `openclaw.json`, VPS deploy configs, or paid GH workflows. Surface any implied changes in `plan.md`'s "Infra implications" section for explicit operator review.
- **conversation.md is append-only.** Every Kit↔operator turn during project_init lands in there. This is the audit trail.
- **No persona drift.** This is Kit-the-architect. Sharp questions, no flattery, no "great idea!"

## Inputs you receive

A natural-language message from the operator that mentions a project, OR an explicit `/recon <repo>` style command, OR a referral from `task_intake` saying "this project is unknown."

Discover whether this is the **existing-repo arm** or the **new-project arm**:

- **Existing-repo arm** when: the operator names a GitHub repo (`org/repo` or full URL), OR the project already has a remote in his world (`finance-sentry`, `lifekit`, `lifekit-stack`, etc.). The expectation is "go read the code, then come back with questions."
- **New-project arm** when: the operator describes a goal or idea with no existing codebase ("I want to build a habit-tracking thing for personal use", "let's plan a new daemon that does X"). The expectation is "no code yet — talk it through with me."

If genuinely ambiguous (the operator says "lifekit-stack — actually let me describe what I want it to become"), this is the ONE clarifying question the hard rules allow: *"Existing repo to recon, or new project to plan from scratch?"* Anything less than internally contradictory: pick the most defensible arm and proceed (document the call in `plan.md`'s Assumptions section).

---

## Existing-repo arm

### 1. Resolve slug + scaffold

```bash
SLUG="<slug, e.g. lifekit-stack>"
REPO="<org/repo, e.g. dsdevq/lifekit-stack>"
PROJ_DIR="$HOME/.life/projects/$SLUG"

if [[ -e "$PROJ_DIR/plan.md" ]]; then
  echo "Project already initialized at $PROJ_DIR — refusing to overwrite. Edit plan.md/recon.md directly if you need to."
  exit 0
fi

mkdir -p "$PROJ_DIR/proposals" "$PROJ_DIR/proposals-approved" "$PROJ_DIR/proposals-rejected" "$PROJ_DIR/runs" "$PROJ_DIR/tasks"
```

### 2. Clone + recon

Clone to a scratch dir, never into `$PROJ_DIR`. The recon is a *read* — no edits, no branches.

```bash
SCRATCH="/tmp/recon-${SLUG}-$(openssl rand -hex 2)"
git clone --depth 50 "https://github.com/${REPO}.git" "$SCRATCH"
cd "$SCRATCH"
```

Then **actually look at the code.** Use Read/Glob/Grep/Bash. The bar is "could you defend a design decision in this codebase to a sharp colleague tomorrow?" Map at minimum:

1. **Top-level shape.** `ls -la`, language/framework guess from manifest files (`package.json`, `pyproject.toml`, `Cargo.toml`, `*.csproj`, `Dockerfile`, `docker-compose.yml`, `Makefile`, `justfile`).
2. **READMEs / docs.** Read `README.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, `docs/**`. Don't summarize — quote the load-bearing claims and note where they live.
3. **Build/run.** How does it boot? Tests? Lint?
4. **Module boundaries.** Top-level dirs, what's in each, naming conventions, where business logic lives.
5. **External deps + integrations.** What does this thing talk to (databases, APIs, queues)?
6. **Active surfaces.** Recent commits (`git log --oneline -n 30`), open TODOs (`grep -rn "TODO\|FIXME\|XXX"` — bounded), CI configs.
7. **Pain signals.** Long files, dead code, mixed conventions, things that *look* like in-progress refactors mid-flight.
8. **What you can't tell from reading alone.** This is the most important section — these become the operator's questions in step 4.

### 3. Write `recon.md`

`$PROJ_DIR/recon.md` — markdown, no frontmatter. Sections in this exact order:

```markdown
# Recon: <slug>

**Repo:** <org/repo>
**Reconned:** <ISO date>
**Default branch:** <main|master|other>

## TL;DR
<3–5 sentences. What is this thing, what's it for, what does it look like under the hood.>

## Stack
<Bulleted: language(s), framework(s), build tool, runtime, key libs. Cite filenames.>

## Module map
<Top-level dirs + 1-line each. Tree-ish format OK. Cite paths.>

## How it runs
- **Build:** <command + source>
- **Test:** <command + source>
- **Lint:** <command + source, or "none observed">
- **Boot:** <how a fresh checkout becomes a running thing>

## External surfaces
<APIs called, DBs spoken to, files written, processes spawned. Cite paths.>

## Conventions observed
<Naming, error handling, logging, comments style, test style. Cite examples.>

## Recent activity (last 30 commits)
<Two or three sentences. What's been moving. Cite a few SHAs or files.>

## TODOs / smells / open questions in the code
<Bulleted. Cite paths/lines. Be honest — don't soften.>

## What I can't tell from the code
<The questions Kit needs the operator to answer. These become the chat questions in §4.>
```

After writing, **delete the scratch clone** — it's a recon, not a checkout. Don't leave clones around.

```bash
rm -rf "$SCRATCH"
```

### 4. Write `plan.md` immediately — proceed by default

Do NOT block on operator answers. The whole point of devclaw is autonomous work; gating recon on a chat round-trip defeats it. Write `plan.md` in the same turn as `recon.md`, using the strongest defensible interpretation of "What I can't tell from the code". Record those interpretations in `plan.md`'s **Assumptions** section so the operator can correct course in one reply if any are wrong.

**ONE clarifying question is permitted, and only when genuinely ambiguous** (no `target_repo` *and* internally contradictory scope — e.g., the operator referenced the repo by name but described work that clearly belongs to a different repo). In that single case, ask the one question and wait. Otherwise: proceed to step 5 immediately.

### 5. Write `plan.md`

`$PROJ_DIR/plan.md` shape — markdown, lightweight frontmatter:

```markdown
---
slug: <slug>
created: <ISO date>
mode: recon                # 'recon' for existing-repo arm, 'socratic' for new
---

# Project: <slug>

## What it is
<2–3 sentences. The honest one-paragraph description.>

## Goals
<Bulleted. The outcomes the operator cares about for this project.>

## Non-goals
<Bulleted. What this project explicitly DOESN'T try to be.>

## Architecture invariants
<Bulleted. The rules that mustn't be broken — boundaries, security gates, conventions.>

## Current state
<Where the project actually is right now. Phase, milestones, known TODOs. Reference recon.md for deep code-level state.>

## Assumptions
<Decisions Kit made when writing this plan without operator input. Bulleted, each one a single sentence: "Assumed X because Y." The operator's one-reply correction surface — if any of these are wrong, the operator says so and Kit updates.>

## Domain implications
<If reconning surfaced anything that implies a `~/.life/domains/` change, list it here. HARD-KEEP — project_init does NOT touch domain files.>

## Infra implications
<If reconning surfaced implied changes to `openclaw.json`, VPS deploy, or paid GH workflows, list them here. HARD-KEEP — project_init does NOT touch infra config.>

## Open questions
<Things still undecided. These get resolved as work lands.>

## How Kit should work on this project
<Anything operator-shaped: do not push directly to main; tests are required; PRs only on branches named X; etc.>
```

Also write `$PROJ_DIR/settings.yaml`:

```yaml
github_repo: <org/repo>
github_visibility: <auto-detect via `gh api repos/<org/repo> --jq .visibility`>
mirror_to_issues: false                # default off; the operator flips on per-project
default_base_branch: <from recon>
test_command: <from recon, or null>
notes: ""
```

Append a one-paragraph "auto-drafted plan.md from recon; assumptions enumerated inside" note to `$PROJ_DIR/conversation.md`.

Reply to the operator with one line plus the assumptions:
```
✅ <slug> initialized. plan.md + recon.md + settings.yaml at ~/.life/projects/<slug>/.

Assumed:
- <one-line assumption>
- <one-line assumption>

Reply with corrections if any are wrong; otherwise the next code task will use this as ground truth.
```

---

## New-project arm (Socratic — bounded to one question)

No repo. No code. Just an idea the operator is shaping. Under the auto-proceed posture, Socratic mode is **bounded to AT MOST one clarifying question**, and only when the operator's brief is genuinely ambiguous (e.g., they named no target audience AND the scope describes two contradictory products). Otherwise: pick the strongest defensible interpretation and write `plan.md` immediately, recording the interpretation in the **Assumptions** section.

### 1. Resolve slug + scaffold

Same as existing-repo arm — derive a slug from the operator's brief, mkdir the project tree. Don't ask.

### 2. Decide: write the plan, or ask one question

Read the operator's brief. Apply this test:

- **No target audience identified AND scope internally contradictory?** → ask the ONE highest-leverage question. Examples:
  - "Solo tool for you, or something other people will use?" (when audience drives the entire shape)
  - "Is this replacing X, or a new thing alongside X?" (when scope is contradictory)
- **Anything less ambiguous?** → skip to step 3. Make defensible calls, document them in Assumptions.

If you asked the question, wait for the answer (one round trip), then proceed.

### 3. Write `plan.md` (mode: socratic)

Same shape as the recon arm's plan.md, with `mode: socratic` in frontmatter. The "Current state" section will be "Conception / pre-implementation." Use the **Assumptions** section liberally — every interpretation you made without operator input goes there as a single-sentence bullet.

Write a minimal `settings.yaml` with `github_repo: null` (until a repo exists).

Reply:
```
✅ <slug> planned. plan.md at ~/.life/projects/<slug>/.

Assumed:
- <one-line assumption>
- <one-line assumption>

Reply with corrections if any are wrong. No repo yet — when one's created, run `/recon` to add recon.md.
```

---

## Option B stub mode (NOT this skill)

If `task_intake` is dropping an **atomic** code task on an unknown project, it auto-creates a stub project itself — does NOT route through here. `project_init` is for when the operator *wants* to understand a project, not for atomic typo-fix shortcuts. See `task_intake/SKILL.md` for the stub path.

## What this skill is not

- Not a "make me a new repo" skill — the operator creates repos on GitHub himself; this skill operates on existing or conceptual projects only.
- Not a runner — produces docs, not code changes.
- Not invoked for known projects — check `~/.life/projects/<slug>/` before invoking. If `plan.md` exists, this skill should not run.
- Not the `propose_change` skill — that one drafts RFCs for *changes* to a known project. project_init bootstraps the project itself.

## Failure modes

| Failure | Action |
|---|---|
| Project already has `plan.md` | Refuse. Tell the operator to edit by hand. |
| Repo clone fails (auth, network, 404) | Append `recon_failed` to `~/.life-state/queue.jsonl`, reply with a one-line "couldn't clone <repo>: <reason>" and stop. Do NOT leave a half-populated `$PROJ_DIR`. |
| the operator never replies to a single clarifying question | After ~24h, write `plan.md` anyway using the strongest defensible interpretation — record both candidate readings under "Assumptions" so the operator can correct in one reply. Half-initialized states are no longer an acceptable outcome under the auto-proceed posture. |
| `gh api` for visibility detection fails | Set `github_visibility: unknown` in settings.yaml. Don't block on it. |
