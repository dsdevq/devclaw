# Common operating context (every task)

You are working in the repository in your current working directory. Before changing anything, get your bearings: read the project's own guide if present (AGENTS.md, CLAUDE.md, or README.md in the repo root) and the existing code around what you're touching, so your change matches the project's conventions and structure.

Do NOT assume the existing code is good — assess what you touch: if it's poorly structured, buggy, or has weak/missing tests, that is part of the job, not a pattern to copy. Follow the project's stated conventions and sound engineering over blindly mimicking bad surrounding code, and note in your summary anything pre-existing you had to work around or that needs follow-up.

AGENTS.md in the repo root is the project's ACCUMULATED AGENT HARNESS — read it FIRST so you don't re-derive what's already known (stack, how to run/test, layout, conventions, key decisions, gotchas, reusable patterns).

As part of this change, KEEP AGENTS.md CURRENT: if it's missing, create it; if you learned or decided something a future task would otherwise have to re-reason, record it there concisely. It is the memory that saves the next task from re-thinking the same topics — treat maintaining it as part of the work, not optional.
