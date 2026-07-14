# Cognition callers & prompt files — conventions

Applies to `devclaw/prompts/*.md` and the layer-3 caller modules
(`goal/{planner,evaluator,decomposer,research,world_research}.py`,
`goal/phases/firming.py`, `devclaw/planner.py`, `devclaw/quality/`).

## Prompt templates (`devclaw/prompts/*.md`)

- Templates render through `load_prompt(slug, **kwargs)` with `str.format` —
  literal braces must be escaped `{{ }}`. A new `{placeholder}` needs the
  matching kwarg at EVERY call site or rendering raises.
- **Grounding pattern (the #227 shape — copy it, don't reinvent):** repo facts
  enter as a `REPOSITORY CONTEXT` block + a prompt clause forbidding inference
  "from your own working directory, the host/Claude process context, or any
  repository you have seen before; absent ⇒ unknown". Every repo-reasoning
  prompt has one; a new cognition caller must too.
- Don't quote a section header like `## Repository context` inside a prompt's
  instruction text — it leaks the literal header into every rendering and makes
  omission tests vacuous (bit us in #234). Reference it as `Repository context`
  without the `##`.

## Caller modules

- **Snapshot collectors** follow one shape: an `asyncio.to_thread` wrapper
  around `task_git._review_repo_context_sync`, defined as a module global in
  the CALLER's module (so tests patch it there), **best-effort and
  never-raises** — a git hiccup degrades to `""` (section omitted), it never
  fails the step. Collection happens at the layer-2 call site, outside any
  fail-closed `try`.
- **Zero-token idle guard is sacred**: no LLM call — and ideally no
  subprocess — on an idle or blocked tick path. New per-tick work goes AFTER
  the `should_plan` / phase gates. The tests asserting `FakeClaude.calls == 0`
  must stay green.
- Optional prompt inputs are blank-safe kwargs (`repo_context: str | None =
  None`; `if x and x.strip():` renders the section) so existing call sites and
  test stubs stay byte-unaffected.
- Cognition callers return parsed output; layer 2 persists it. Never write to
  the goal store or reach into the task queue from a caller module.
- Fixing a bad cognition outcome means **grounding the reasoning, never
  weakening a gate** — verification stays fail-CLOSED; an undocumented model
  output field is ignored, not honored (cf. the removed planner `verify_cmd`
  override, #233).
