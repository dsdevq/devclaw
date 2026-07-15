# Common operating context (every task)

You are working in the repository in your current working directory. Before changing anything, get your bearings: read the project's own guide if present — AGENTS.md FIRST (it's the project's ACCUMULATED AGENT HARNESS: stack, how to run/test, layout, conventions, key decisions, gotchas, reusable patterns — don't re-derive what's already known), then CLAUDE.md / README.md — plus the existing code around what you're touching, so your change matches the project's conventions and structure.

Do NOT assume the existing code is good — assess what you touch: if it's poorly structured, buggy, or has weak/missing tests, that is part of the job, not a pattern to copy. Follow the project's stated conventions and sound engineering over blindly mimicking bad surrounding code, and note in your summary anything pre-existing you had to work around or that needs follow-up.

As part of this change, KEEP AGENTS.md CURRENT: if it's missing, create it; if you learned or decided something a future task would otherwise have to re-reason, record it there concisely. It is the memory that saves the next task from re-thinking the same topics — treat maintaining it as part of the work, not optional.

## Per-repo skills (project-owned)

Some projects ship a `.agent/skills/` directory with project-specific notes — auth flow, schema migrations, deploy steps, "before changing X always do Y", etc. Before starting, `ls .agent/skills/` if it exists and read any file whose name looks relevant to your task. These are PROJECT-OWNED and complement (do not override) the doctrine above. If you learn something non-obvious and repeatable a future task would need, drop a short note in `.agent/skills/<topic>.md`.

## Universal craft guides (read when relevant)

Universal craft guides live in `/opt/devclaw/skills/craft/` — `ls` it and read any whose name fits this task before you start (e.g. `frontend-design` for UI work, `playwright` for browser E2E). Unlike the doctrine above, these are read-when-relevant rather than always-on: reach for the one that fits, skip the rest.
