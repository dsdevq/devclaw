---
name: curator_drain
description: Drain ~/.life/queue.jsonl into domain-file edits per the curator protocol in PLAN.md. For domains on the `auto_apply_allowlist` (ideas.md, engineering.md, learning.md, health.md), commits directly to `~/.life/<domain>.md`. For non-allowlisted domains (career.md, commitments.md, finance.md, and any new domain by default), writes proposed edits as patches to `~/.life/.curator-proposed/` for manual review. Emits a daily digest to `~/.life/journal/curator/YYYY-MM-DD.md` logging every patch AND every auto-applied commit. Triggered by `openclaw cron` every 15-30 min, or any time Denys asks ("drain the queue", "run curator", "curator drain").
---

# Curator drain

You drain `~/.life/queue.jsonl` into domain edits. **You operate in two modes per item, selected by the target domain:**

- **Auto-apply** (when the target domain is on `auto_apply_allowlist`): edit `~/.life/<domain>.md` directly and commit with the exact message format below. Bad commits are `git revert <sha>` away — that's the safety net (`~/.life/PLAN.md` Curator protocol).
- **Supervised** (every other domain): write a `.patch` file under `~/.life/.curator-proposed/` for Denys to review. No git operations.

`Refuse-and-defer` is always a valid output regardless of mode.

## Config

```yaml
auto_apply_allowlist:
  - ideas.md
  - engineering.md
  - learning.md
  - health.md
```

Any domain file NOT on this list stays supervised — that explicitly protects `career.md`, `commitments.md`, `finance.md`, and any new domain added later (default-supervised).

## Hard behavioral rules

- The allowlist is the ONLY mode selector. No per-domain rules, no risk-class fields, no per-item overrides. The target domain alone decides patch-vs-commit.
- For supervised items: no domain file mutation, no git operations. Patches are inert files for Denys to review.
- For auto-apply items: edit the target domain file, `git add -- ~/.life/<domain>.md`, and `git commit -m "curator: <domain> from queue item <queue-id>"` — that exact message shape. `git revert <sha>` is the documented rollback.
- One commit per queue item. Do NOT batch multiple items into a single commit; the rollback safety net depends on commit-per-item granularity.
- Idempotent. If a queue item's id already appears in (a) `.curator-proposed/<id>.patch`, (b) any commit message of the form `curator: ... from queue item <id>`, or (c) today's digest, skip it.
- Refuse-and-defer is always a valid output. When uncertain, write a deferred patch (supervised mode) with a `# DEFERRED:` header — never auto-commit a deferred classification, even if the target domain is on the allowlist.
- Be conservative. If the queue item could fit two domains, route to the better fit and note the alternative in the digest.
- Never fabricate content. If a queue item is ambiguous or garbled, defer.

## Protocol invariants (from `~/.life/PLAN.md` Curator protocol)

- Every supervised queue item gets its own patch file at `~/.life/.curator-proposed/<queue-id>.patch`.
- Every auto-applied queue item gets its own commit with message `curator: <domain> from queue item <queue-id>`.
- Patch format: unified diff against the current domain file, with a header comment block.
- `career.md`, `commitments.md`, `finance.md` are NOT on the allowlist and stay supervised.

## Inputs — read in this order

1. `~/.life/queue.jsonl` — newline-delimited JSON events. Each item has at minimum `id`, `timestamp`, and `content`. May also have `source`, `channel`, `tags`.
2. `~/.life/.curator-proposed/` — list existing patches to skip already-processed items by `<queue-id>`.
3. The git log of `~/.life/` — skip items whose ID appears in any commit message (`curator: ... from queue item <id>`). This covers both prior supervised items that Denys applied AND prior auto-applied items.
4. Each domain file in `~/.life/domains/` — `career.md`, `engineering.md`, `learning.md`, `health.md`, `commitments.md`, `ideas.md`, `finance.md`. Read on demand once you've classified an item.
5. Existing digest file `~/.life/journal/curator/YYYY-MM-DD.md` for today, if it exists — you append, not overwrite.

## Classification rules

For each queue item, pick exactly one of:

1. **Edit a domain** — content has clear durable value for one specific domain. Then branch on mode:
   - If the target domain is on `auto_apply_allowlist`: edit + commit (auto-apply).
   - Otherwise: write `.curator-proposed/<id>.patch` (supervised).
2. **Defer** — content is ambiguous, low-confidence, or spans multiple domains in a non-trivial way. ALWAYS supervised — write the patch with `# DEFERRED:` header and explain why; Denys decides. Never auto-commit a defer, even if the would-be target is on the allowlist.
3. **Skip** — content is not durable (one-off command, ephemeral status, already-stale by the time of drain). Note in digest with reason. No patch, no commit.

Disambiguation hints (not exhaustive):

- Code/architecture/decisions/blockers → `engineering.md`
- Python/AI/learning notes/spaced-repetition flags → `learning.md`
- Calendar/RSVP/promises/appointments → `commitments.md`
- Nutrition/sleep/gym/health flags → `health.md`
- Anthropic/job applications/career-arc milestones → `career.md`
- Holdings/risk/finance-learning → `finance.md`
- Raw ideas not yet in another domain → `ideas.md`

## Auto-apply commit procedure (allowlisted domains)

For each item routed to an allowlisted domain:

1. Re-read `~/.life/domains/<domain>.md` to find the right section boundary for the new content.
2. Edit the file in place — insert the new content under the appropriate section header, matching the file's existing style (bullet form, indentation, frontmatter respected).
3. Stage and commit, one commit per queue item:
   ```bash
   cd ~/.life
   git add -- domains/<domain>.md
   git commit -m "curator: <domain> from queue item <queue-id>"
   ```
4. Capture the resulting short SHA for the digest entry.

The commit message format is fixed (`curator: <domain> from queue item <queue-id>`) so that:
- `git log --grep="queue item <id>"` resolves an item to its commit.
- `git revert <sha>` is the documented one-shot rollback.
- The idempotency check in step 3 of "Inputs" finds and skips already-applied items.

## Supervised patch file format (non-allowlisted domains)

Each `.curator-proposed/<id>.patch` must contain a header comment block, then a unified diff that `git apply` can consume:

```
# Queue item: <id>
# Captured: <timestamp from queue>
# Source: <source field if present, else "unknown">
# Proposed domain: <filename>
# Confidence: high | medium | low
# Reasoning: <one sentence>
# Original content (verbatim):
# > <content line 1>
# > <content line 2>
# DEFERRED: <reason — only present if classification was 'defer'>

--- a/domains/<filename>
+++ b/domains/<filename>
@@ -<line>,<count> +<line>,<count+n> @@
 ...context line...
+new content
 ...context line...
```

Generate the unified diff with proper `@@` hunk headers. Three lines of context above and below if available. Keep the addition placed at a logical section boundary in the target domain file (read the file's structure — frontmatter, section headers — and pick the section the new content fits under).

## Daily digest format

Append to `~/.life/journal/curator/YYYY-MM-DD.md` (create if missing) the section emitted by **this run**. Auto-applied commits and supervised patches are logged side by side so the digest is the single review surface:

```
## Drain run @ <HH:MM Europe/Dublin>

**Processed:** <N> queue items.

### Auto-applied (<count>)
- `<id>` → `<domain>` (commit `<short-sha>`) — one-line reasoning
- ...

### Patched — needs review (<count>)
- `<id>` → `<domain>` (confidence: high|medium|low) — one-line reasoning
- ...

### Deferred (<count>)
- `<id>` → would-be `<domain>` — reason
- ...

### Skipped (<count>)
- `<id>` — reason (e.g. "ephemeral status", "duplicate of <other-id>")
- ...
```

If a section is empty (e.g. zero deferred), omit the section header. Every auto-applied item MUST appear in the `Auto-applied` section with its commit SHA — this is the operator's visibility into what the curator changed without their gate.

## Output discipline

- Do NOT print the patches, diff content, or digest body to the chat / stdout. Write files (and git commits, for auto-apply). Confirm at the end with a one-line summary: `Drained N items: A auto-applied, P patched, D deferred, S skipped. Digest at journal/curator/<date>.md.`
- If `queue.jsonl` is empty or has zero unprocessed items, write nothing and reply: `Queue empty. No action.`
- Never ask Denys clarifying questions — defer instead. This skill runs unattended on cron.

## Trigger examples

- "drain the queue"
- "run curator"
- "curator drain"
- The OpenClaw cron message body for the `curator_drain` job.

## Failure handling

- If `~/.life/queue.jsonl` is missing, create an empty one and exit silently.
- If a queue line is malformed JSON, write a patch-equivalent into `.curator-proposed/<line-hash>.malformed.txt` with the raw content and a `# MALFORMED:` header, and log it in the digest under Skipped.
- If a domain file is missing, log under Deferred (do not create the file — that's a separate decision). This applies whether the would-be target was on the allowlist or not.
- If an auto-apply `git commit` fails (e.g. nothing staged because the edit was a no-op, or a pre-commit hook rejected it), do NOT fall back to writing a patch. Log the item under Deferred in the digest with the failure reason and leave the working tree clean (`git reset HEAD -- domains/<domain>.md && git checkout -- domains/<domain>.md`).
