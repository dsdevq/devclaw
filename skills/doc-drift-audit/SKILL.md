---
name: doc-drift-audit
description: "Weekly cognition-rung doc-drift audit across configured target repos. For each repo in `repos.yaml`, clones the repo, runs the deterministic short-circuit (`scripts/check-doc-drift.sh`) if present, and only invokes the LLM when the mechanism rung is absent or already flagging drift. Output is a proposal entry (`status: new (auto-surfaced)`) appended to the repo-scoped proposals dir under `~/.life/projects/<repo>/proposals/`, or `~/.life/system/proposals.md` as a fallback. Triggered by `routines/doc-drift-audit.yaml` (Sun 04:00 UTC) or invoked directly. `kind: research_task` — surfaces proposals; never opens auto-PRs."
---

# doc-drift-audit

You are running the cognition-rung pass of the three-rung doc-drift contract
(see `~/.life/system/proposals.md#2026-05-20-doc-drift-automation-three-rung`).

You are NOT in conversation with the operator. He may be asleep. The only
visible output is the proposal entries you append (one per drifted repo) and
the run.log.jsonl trail. No clarifying questions — if something is ambiguous,
make a defensible call and document it in `notes`.

This skill's purpose is to catch the prose-shaped drift the mechanism rung
can't parse: stale architecture explanations, claims about behavior that no
longer match code or compose, tool names renamed in code but still referenced
in README, drift in `Status`/`Limitations`/`What's inside` sections.

## Hard behavioral rules

- **Short-circuit before reasoning.** Per repo: if `scripts/check-doc-drift.sh`
  exists AND exits 0, skip the LLM call and record a `short_circuited` event in
  `run.log.jsonl`. This is what keeps the routine marginal $0 on clean weeks.
- **Output is a proposal entry, never an auto-PR.** Drift findings get
  appended as a proposal block with `status: new (auto-surfaced)`. The
  operator grades them on their own cadence.
- **One proposal block per repo per run.** Don't fragment one drifted
  README into N separate proposals — collect every finding for a repo into
  one block.
- **Silent on no-signal.** If no repo drifts and no repo errors, the routine
  is silent: no proposal entries appended, no announce sent. The audit-log
  entry in `run.log.jsonl` is enough.
- **Scratch under `/tmp/`.** All clones go under
  `/tmp/doc-drift-audit-<date>/<repo-slug>/`. Wipe before each clone — fresh
  state every run.

## Inputs

When invoked, you'll be given a path like `~/.life/tasks/<task_id>/spec.yaml`.
The frontmatter you care about:

```yaml
task_id: <id>
kind: research_task
budget:
  max_runtime_seconds: 1800
```

The repo list lives at `skills/doc-drift-audit/repos.yaml` (alongside this
SKILL.md, inside the cloned devclaw checkout — or at the corresponding path
under `~/.openclaw/workspace/skills/doc-drift-audit/` when the skill is
symlinked into the OpenClaw workspace per the devclaw README install step).

## Execution sequence

### 1. Set up

```bash
TASK_ID="<from spec.yaml>"
RUN_DATE="$(date -u +%Y-%m-%d)"
SCRATCH="/tmp/doc-drift-audit-${RUN_DATE}"
mkdir -p "$SCRATCH"
```

Append to `run.log.jsonl`:
```json
{"ts":"<iso>","actor":"doc-drift-audit","event":"started","scratch":"/tmp/doc-drift-audit-<date>"}
```

### 2. Load the repo list

Read `repos.yaml` (resolve via the same directory this SKILL.md lives in).
Each entry has at minimum a `slug: <org>/<repo>`. Optional per-entry overrides:

- `readme` — relative path to the README (default `README.md`).
- `short_circuit.script` — relative path to the doc-drift script
  (default `scripts/check-doc-drift.sh`).
- `proposals_dir` — relative to `~/.life/` (default
  `projects/<repo-basename>/proposals/`; fallback `system/proposals.md`).

### 3. For each repo: short-circuit, then audit

```bash
for entry in <repos.yaml entries>; do
  SLUG="<slug>"
  BASENAME="${SLUG##*/}"
  REPO_DIR="$SCRATCH/$BASENAME"
  rm -rf "$REPO_DIR"
  git clone "https://github.com/${SLUG}.git" "$REPO_DIR" || {
    # log clone_failed event and continue to the next repo
    continue
  }

  cd "$REPO_DIR"
  SCRIPT="${short_circuit.script:-scripts/check-doc-drift.sh}"
  if [ -x "$SCRIPT" ] && "$SCRIPT" >/dev/null 2>&1; then
    # mechanism rung passed — no LLM call for this repo this week.
    # log: {"event":"short_circuited","repo":"<slug>","script":"<SCRIPT>"}
    continue
  fi
  # else: fall through to the LLM audit
done
```

The short-circuit is load-bearing — it's what makes the routine $0 per
[[feedback-pro-subscription-is-the-design]]. Don't run the LLM call if the
deterministic check is green.

### 4. LLM audit (per non-short-circuited repo)

Read these files inside the cloned repo:

- The README (path from `entry.readme`, default `README.md`).
- `docker-compose*.yml`, `compose/*.yml`, `compose.yaml` — if any exist.
- Top-level service or runtime config (`Dockerfile`, `pyproject.toml`,
  `package.json`, etc. — whatever the repo type suggests as "the code
  the README is describing").

Now reason about prose drift. For each claim in the README, ask:

- Is the named service/tool/file still present under that name?
- Does the described count match what compose actually defines?
- Are the listed environment variables / flags still consumed by the code?
- Does the architecture diagram match the current module/service layout?
- Are the listed limitations still current (or already addressed)?
- Are the "Status" / "version" claims plausibly current?

For each drift you find, capture:

- File + line number (or section heading + line) of the README claim.
- The claim itself (quoted).
- The current contradicting state (the code/compose/file you checked).
- A 1-line suggested fix.

A finding is only worth surfacing if it's a real contradiction or a
materially-stale claim. Don't flag prose that's merely "a bit terse" or
"could be clearer" — that's editorial polish, not drift.

### 5. Append proposal entries

For each repo with at least one finding:

1. Determine the proposals destination:
   - Try `~/.life/projects/<repo-basename>/proposals/` first.
   - If that dir doesn't exist, fall back to appending a block to
     `~/.life/system/proposals.md`.
2. Compose a single proposal block:

```markdown
## <YYYY-MM-DD>-<repo-basename>-doc-drift-auto-surfaced
- Status: new (auto-surfaced)
- Author: doc-drift-audit (cognition rung)
- Target: <org>/<repo>
- Mechanism rung: <script ran + exited N | script absent>
- Findings: <N>

<one-line framing of the drift pattern, e.g.
 "README still describes BuildEngine port; code retired it 2026-05-18.">

### Drift detail

1. **`<file>:<line>` — <one-line claim summary>**
   > <quoted README claim>

   Current state: <what code/compose actually shows>.
   Suggested fix: <1-line correction>.

2. ...

### Suggested next step

Grade this proposal. If accepted, a `code` task can land the README edits;
the matching mechanism-rung assertion (if missing) should be added in the
same PR so this drift gets caught deterministically next time.
```

If destination is a directory, write the block to a new file named
`<YYYY-MM-DD>-<repo-basename>-doc-drift-auto-surfaced.md`. If destination is
the single `system/proposals.md`, append the block.

Append a `proposal_appended` event to `run.log.jsonl` for each block.

### 6. Write result.json (write-once)

```json
{
  "task_id": "<task_id>",
  "kind": "research_task",
  "status": "done",
  "completed_at": "<iso>",
  "repos_scanned": <int>,
  "repos_short_circuited": <int>,
  "repos_with_drift": <int>,
  "proposals_appended": ["<absolute path>", "..."],
  "notes": "<defensible-call documentation, drive-bys>",
  "runtime_seconds": <int>
}
```

On failure: `status: blocked` plus `blocker` from
`{clone_failed_all_repos, repos_yaml_invalid, time_budget_exceeded, unknown_<short>}`,
with a `to_resume` 1-2 sentence handoff.

### 7. Update spec + announce

Follow the same two-step close-out as `research-task`:

- **7a.** Edit spec.yaml to set `status: done`, `completed_at: <iso>`,
  `result_summary: "<N> repos scanned · <M> short-circuited · <K> proposals appended"`.
- **7b.** Announce via `openclaw message send`. SUPPRESS the announce when
  `repos_with_drift == 0 AND repos_with_errors == 0` — silent on no-signal.
  When announcing, the message is one line:
  `"📋 doc-drift-audit · <K> proposals across <K_repos> repos: <comma-joined slugs>"`.

## Failure modes

| Failure | Action |
|---|---|
| `git clone` fails for one repo | Log `clone_failed` event; continue to the next repo. Don't block the whole audit on one bad repo. |
| `git clone` fails for ALL repos | Block with `clone_failed_all_repos`. Almost certainly a gh-auth or network problem. |
| `repos.yaml` missing or unparseable | Block with `repos_yaml_invalid`. |
| Time budget exceeded mid-audit | Stop, append a `time_budget_exceeded` partial result, list the repos that were scanned vs. skipped in `notes`, exit. Don't try to compress remaining repos. |
| Proposals destination dir doesn't exist AND fallback file is also missing | Create `~/.life/system/proposals.md` with just the new block (it's already the canonical fallback per the operator's convention). |

## What this skill is not

- Not for catching parseable drift — that's the mechanism rung
  (`scripts/check-doc-drift.sh` inside each target repo, filed as spec A).
- Not an auto-PR runner. Findings are proposals; the operator grades them.
- Not for more-than-weekly cadence. Doc drift accumulates slowly; daily would
  be both wasteful and noisy.
- Not for arbitrary code review. Scope is README ↔ code/compose drift, not
  "is this code good".

## Why this exists

Per the operator's `[[denys_mechanism_cognition_split]]`: LLM only when
reasoning actually pays. Prose-shaped drift (e.g. "the README still
explains a tool that was renamed three weeks ago") can't be caught by
`grep` or `wc -l`; that's where this rung earns its keep. Everything
deterministic stays on the mechanism rung in each target repo's
`scripts/check-doc-drift.sh`.
