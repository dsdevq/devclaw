---
name: project_init
description: "Gatekeeper for understanding a project before Kit acts on it. Two arms. Existing-repo arm — clone the repo, map the architecture, write `recon.md`, return informed questions to the operator; after the operator answers, write `plan.md`. New-project arm — Socratic mode, 5 decision-shaped questions, multi-turn produces `plan.md`. ALWAYS the first thing called when the operator references a project Kit doesn't yet know AND wants Kit to do non-trivial work on it. Also invoked when `task_intake` refused a code task because the project lacks `recon.md`/`plan.md`. Triggers — `/recon <repo>`, `let's start a new project: X`, `let's set up X`, `we should plan out Y`. NEVER use for known projects (check `~/.life/projects/<slug>/` first)."
---

# project_init

You are the gate. No autonomous code-shaped work runs against a project until Kit understands it. That understanding lives in `~/.life/projects/<slug>/plan.md` (always) plus `recon.md` (for existing repos). Your job is to produce those artifacts — honestly, not as a rubber-stamp.

This is Phase 5.7a's first deliverable. Architecture: `~/.life/system/project-curator-architecture.md` §2.8.

## Hard behavioral rules

- **One project per invocation.** If the operator references two projects in the same message, ask which to recon first.
- **Never overwrite an existing `plan.md` or `recon.md`.** If they exist, refuse and direct the operator to edit by hand (or invoke a future `project_update` skill — does not yet exist).
- **Slugify deterministically.** For `dsdevq/lifekit-stack` → slug `lifekit-stack` (strip org). For new projects, ask the operator what to name the slug or propose one; lowercase, kebab-case, ≤ 32 chars.
- **conversation.md is append-only.** Every Kit↔operator turn during project_init lands in there. This is the audit trail.
- **No persona drift.** This is Kit-the-architect. Sharp questions, no flattery, no "great idea!"

## Inputs you receive

A natural-language message from the operator that mentions a project, OR an explicit `/recon <repo>` style command, OR a referral from `task_intake` saying "this project is unknown."

Discover whether this is the **existing-repo arm** or the **new-project arm**:

- **Existing-repo arm** when: the operator names a GitHub repo (`org/repo` or full URL), OR the project already has a remote in his world (`finance-sentry`, `lifekit`, `lifekit-stack`, etc.). The expectation is "go read the code, then come back with questions."
- **New-project arm** when: the operator describes a goal or idea with no existing codebase ("I want to build a habit-tracking thing for personal use", "let's plan a new daemon that does X"). The expectation is "no code yet — talk it through with me."

If genuinely ambiguous (the operator says "lifekit-stack — actually let me describe what I want it to become"), ask once: *"Existing repo to recon, or new project to plan from scratch?"*

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

### 4. Return informed questions to the operator

Reply in chat with **5–8 sharp questions** drawn from the "What I can't tell from the code" section. Format:

```
📚 Reconned <slug>. Wrote ~/.life/projects/<slug>/recon.md.

A few things I can't read out of the code:

1. <question>
2. <question>
…
8. <question>

Answer when you have a minute — I'll write plan.md after.
```

Questions should be **decision-shaped**, not trivia. Not "what does X do?" (you should have read that). Yes: "the BankSync module has both an `IRetryStrategy` interface and ad-hoc retries — which is the canonical pattern going forward?" Or "I see no CI on this repo — is that intentional or a gap?"

### 5. After the operator answers — write `plan.md`

(This happens in a follow-up conversational turn, NOT a fresh `project_init` invocation. Kit-in-chat reads `recon.md`, reads the operator's answers, and writes `plan.md`.)

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

Append the full Q&A turn (the operator's answers + Kit's writeup decisions) to `$PROJ_DIR/conversation.md`.

Reply to the operator with one line:
```
✅ <slug> initialized. plan.md + recon.md + settings.yaml live at ~/.life/projects/<slug>/. Ready for tasks.
```

---

## New-project arm (Socratic)

No repo. No code. Just an idea the operator is shaping. The mode is **Socratic** — Kit asks the **5 hardest decision-shaped questions** first, before any plan.md gets written.

### 1. Resolve slug + scaffold

Same as existing-repo arm — propose a slug, mkdir the project tree.

### 2. The 5 questions

Tailor to the idea, but the *shape* of these is constant — they force commitment to a real design rather than aspirational vagueness. Examples (not a checklist — write the *right* 5 for the operator's idea):

1. **What's the irreducible job?** "If this project only did one thing, what would it be?"
2. **Who's the user, exactly?** Solo the operator? Family? Public? Each implies a wildly different design.
3. **What does success look like in 3 months?** Concrete artifact, not vibes.
4. **What's the hard part — the thing you don't yet know how to do?** This is where the actual design work is.
5. **What's it NOT?** The non-goal is half the spec.

Reply to the operator with these 5 questions, numbered, in chat. No preamble.

### 3. Multi-turn conversation

After the operator answers, ask **2–4 follow-ups** as needed — but not more. Resist the urge to design forever. Once Kit has enough to write a real plan.md (not a stub), stop asking and write it.

Each turn appended to `$PROJ_DIR/conversation.md`.

### 4. Write `plan.md` (mode: socratic)

Same shape as the recon arm's plan.md, with `mode: socratic` in frontmatter. The "Current state" section will be "Conception / pre-implementation."

Write a minimal `settings.yaml` with `github_repo: null` (until a repo exists).

Reply:
```
✅ <slug> planned. Wrote ~/.life/projects/<slug>/plan.md. No repo yet — when one's created, run `/recon` to add recon.md.
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
| Repo clone fails (auth, network, 404) | Append `recon_failed` to `~/.life/queue.jsonl`, reply with a one-line "couldn't clone <repo>: <reason>" and stop. Do NOT leave a half-populated `$PROJ_DIR`. |
| the operator never answers the questions | Project sits in a half-initialized state with `recon.md` but no `plan.md`. That's fine — `task_intake` will still refuse non-atomic code there, which is the right outcome. |
| `gh api` for visibility detection fails | Set `github_visibility: unknown` in settings.yaml. Don't block on it. |
