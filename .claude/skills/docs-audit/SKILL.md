---
name: docs-audit
description: Audit devclaw's documentation for drift against the code and normalize doc structure. Verifies every doc in docs/INDEX.md (plus README.md and CLAUDE.md) against the actual code, fixes or flags stale claims, updates currency tags, and converges all .md files toward one house format. Use whenever Denys asks to audit/check/refresh the docs, asks "are the docs up to date", says a doc looks wrong or stale, wants docs normalized/restructured, or after a tranche of PRs lands and the docs haven't been swept since. Also the tool for triaging anything INDEX.md already marks STALE.
---

# docs-audit — keep docs/ honest, uniform, and navigable

The repo's contract is "a stale doc that looks current is worse than no doc."
This audit makes that contract real: verify claims against code, fix or flag,
tag currency, normalize format. **The code wins** — never mark a doc current
because it reads well; only because its checkable claims checked out.

## Scope

`docs/**/*.md` + `README.md` + `CLAUDE.md` + the path→doc map in
`.claude/hooks/docs-reminder.py` (`DOC_MAP`). Explicitly out of scope:
`.agent/skills/` (devclaw's worker layer — product, not harness) and generated
views (`STATUS.md`/`log.md`/`deliveries.md` — projections, never hand-edited).

## Procedure

### 1. Inventory against INDEX

Read `docs/INDEX.md`, then `find docs -name '*.md'`. Every file needs a row; every row
needs a file. An unlisted doc is invisible (nobody trusts a doc that isn't in
the manifest); a ghost row is a broken promise. Fix both immediately.

### 2. Scope the work with git — audit incrementally, not O(everything)

For each doc, find when it last changed and what code moved since:

```bash
git log -1 --format="%h %cs" -- docs/<file>.md
git log --oneline <that-hash>..HEAD -- devclaw/ openhands-runner/ .sandcastle/
```

A doc whose subject area has no commits since its last edit needs only a spot
check. A doc whose area churned (check the layer map in CLAUDE.md for which
code belongs to which doc) gets full verification. This is what keeps repeat
audits cheap.

### 3. Verify load-bearing claims

For each doc in scope, extract the claims that break workflows when wrong, and
check each against the source of truth:

| Claim type | Verify against |
|---|---|
| MCP tool names | `grep "@mcp.tool" -A2 devclaw/server/tools.py` |
| File paths, module names | `ls` / `test -e` |
| Env vars | grep the codebase; cross-check `docs/reference/env-vars.md` |
| CLI commands, entry points | `pyproject.toml` scripts, `--help` |
| Flow/sequence statements | read the named functions (tick, dispatch, settle) |
| Invariants quoted from CLAUDE.md | the enforcing code + its named regression test |

Prose, motivation, and design rationale are not verifiable — leave them alone
unless the design itself changed. If many docs are dirty, fan the per-doc
verification out to parallel Explore subagents and keep only the verdicts.

### 4. Fix or flag — never silently rewrite

- **Small drift** (renamed tool, moved file, dead link, removed env var): fix
  inline in the doc, same audit.
- **Structural drift** (the architecture moved, a whole flow is gone): do NOT
  rewrite the narrative yourself — that's a design statement, Denys's call.
  Mark the INDEX row STALE in the established house style: what's wrong,
  precisely, and "flagged for triage". A precise STALE flag is a fully
  acceptable audit outcome; an invented narrative is not.
- Update every touched row's currency tag in `docs/INDEX.md`.

### 5. Normalize format

Converge each audited doc toward the house shape (don't churn prose that
already works — diff noise buries the real fixes):

- `# Title` — one H1, matching the INDEX row's framing.
- First paragraph: what this doc covers and who reads it when.
- `##` sections, tables for enumerable facts, fenced blocks for commands.
- Relative links for anything in-repo; every cross-reference clickable.
- End with "where to look next" links when the doc hands off elsewhere.

### 6. Audit the enforcement loop itself

Check `DOC_MAP` in `.claude/hooks/docs-reminder.py`: every mapped doc still
exists, every code area that has a doc is mapped, new subsystems since the last
audit get rows. The hook is only as honest as this map.

### 7. Report

End with a verdict table — one row per doc: **CURRENT** (verified, evidence),
**FIXED** (what changed), or **STALE-flagged** (what's wrong, why it wasn't
fixed here). Plus the inventory result and any DOC_MAP changes. Never report
a doc CURRENT that you didn't verify — say "not audited" instead.
