# Skill library (host-side, per-goal provisioning)

Curated single-file skills that a goal can request via `Goal.skills_required`.
`devclaw/skill_library.py` copies each requested `<slug>.md` into the
workspace's `.agent/skills/` before dispatch, where the runner's per-repo
loader picks it up. Slugs are basenames; files starting with `_` (like this
one) are not skills.

**Deployment:** production reads `/opt/devclaw/skill-library/` — either copy
this directory there or point `DEVCLAW_SKILL_LIBRARY` at a checkout of it.

## Curation rules

- **One file per skill** (the v1 contract in `skill_library.py`) — no bundled
  resources, no subdirectories. Forces concision.
- **Plain markdown, model-agnostic** — no YAML frontmatter, no agent-specific
  tool calls, no slash-command references. The same file must work whatever
  agent runs in the sandbox.
- **Autonomy-shaped** — the reader is an unattended worker. Never "ask the
  user": decisions derive from the task contract (spec / `done_when`); genuine
  ambiguity is recorded in the task summary or fails loudly.
- `tests/test_skill_library_content.py` enforces the mechanical parts of the
  above — keep it in sync when the rules change.

## Provenance

`tdd`, `codebase-design`, `domain-modeling`, and `resolving-merge-conflicts`
are adapted from [mattpocock/skills](https://github.com/mattpocock/skills)
(MIT, © 2026 Matt Pocock) — reshaped for autonomous workers per the rules
above. The same source's `diagnosing-bugs` lives in the baked
`openhands-runner/skills/fix_bug/` tier instead, because every bug-fix task
should get it without opting in.
