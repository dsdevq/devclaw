# Domain modeling — CONTEXT.md and ADRs

Build and sharpen the project's domain model as you work, and write it down
where the next task will find it. You run in a fresh sandbox with no memory of
previous tasks — `CONTEXT.md` (the domain glossary) and `docs/adr/`
(architectural decision records) committed in the repo are how domain
knowledge survives between tasks. Reading them is a habit every task does;
this skill is for **changing** the model: introducing terms, resolving
ambiguity, recording decisions.

Division of labor: **AGENTS.md** holds operational facts (stack, how to
run/test, layout, gotchas). **CONTEXT.md** is a glossary and nothing else —
what the domain's words mean. Don't let implementation details leak into it.

## File structure

```
CONTEXT.md          ← the glossary (create when the first term is resolved)
docs/adr/
  0001-<slug>.md    ← one decision per file (create when the first is needed)
```

Create files lazily — only when you have something to write. If the repo has a
`CONTEXT-MAP.md` at the root, it has multiple bounded contexts; the map points
to where each context's own `CONTEXT.md` and `docs/adr/` live.

## During the work

- **Challenge the spec against the glossary.** When the task's spec uses a
  term that conflicts with `CONTEXT.md`, don't silently pick one meaning —
  resolve it from context if the evidence is clear (and record the resolution
  in your summary), or flag the conflict as an open question in your result.
- **Sharpen fuzzy terms.** When a spec term is vague or overloaded ("account"
  — the Customer or the User?), pick the precise term the code and glossary
  support, use it consistently, and add it to `CONTEXT.md`.
- **Cross-reference with code.** When the glossary says one thing and the code
  does another ("glossary says partial cancellation exists; the code cancels
  whole Orders"), surface the contradiction in your summary — don't paper
  over it.
- **Stress-test with scenarios.** When you define a term or a relationship,
  invent an edge-case scenario and check the definition holds ("what does
  'cancellation' mean after partial shipment?"). If it doesn't, the term needs
  sharpening before you build on it.
- **Update CONTEXT.md inline.** The moment a term is resolved, write it down.
  One line per term: the word, its precise meaning, what it is *not* when
  that's the common confusion. Don't batch for later — later never comes in a
  torn-down sandbox.

## ADRs — sparingly

Write an ADR only when all three hold:

1. **Hard to reverse** — changing the decision later costs real work.
2. **Surprising without context** — a future reader will ask "why on earth
   did they do it this way?"
3. **A real trade-off** — there were genuine alternatives and one was chosen
   for specific reasons.

If any is missing, skip the ADR — the decision belongs in the commit message.
Format: context (the forces), the decision, the consequences (including what
got worse). Number sequentially, never edit an accepted ADR — supersede it
with a new one.

---
*Adapted from [mattpocock/skills](https://github.com/mattpocock/skills) (MIT © 2026 Matt Pocock).*
