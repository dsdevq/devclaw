# Spec lifecycle — proposal → lock → ADR → tranche

Why this rule exists: for weeks devclaw drifted approach-to-approach because
directions were decided in conversation and never durably locked — the next
session's incident quietly became the new direction. The fix is not a toolkit
(we evaluated GitHub's spec-kit and declined it — we already own every artifact
it provides, plus `invariant-guard`, which it doesn't have); the fix is naming
the pipeline we already converged on and adding one hard discipline line.

> **Re-evaluated 2026-07-22 on a real install** (spec-kit 0.13.4, actually
> scaffolded into `.specify/` + 10 `speckit-*` skills). Verdict held: it
> duplicated this proposal→ADR pipeline with a *second* spec home (`specs/NNN/`)
> — the exact two-homes drift this rule exists to prevent — and its generic
> product-feature template can't check devclaw's invariants (no `invariant-guard`
> equivalent). We removed the scaffold and kept the one idea worth taking: the
> *slice-into-prioritized-increments* discipline below. See vault
> `projects/devclaw/speckit-decision-2026-07-22.md`.

## The pipeline

```
conversation → docs/proposals/<slug>.md        status: DRAFT
             → interrogate every [OPEN] item   (the clarify step — mandatory)
             → status: LOCKED (direction)      execution NOT implied
             → tranche scheduled by Denys      → graduate to docs/decisions/ ADR
             → tranche executes                named regression tests + invariant-guard
```

- **`docs/proposals/`** is the pre-ADR staging area: direction drafts written
  during/right after the conversation that produced them. A proposal carries a
  status line at the top: `DRAFT` → `LOCKED (direction)` → `GRADUATED → ADR
  NNNN` (or `ABANDONED — why`). The status line inside the doc is authoritative.
- **The clarify step is mandatory.** A proposal cannot flip to LOCKED while any
  `[OPEN]` item lacks either an answer or an explicit deferral-with-owner.
  Answering them one by one, in conversation, is the highest-value anti-drift
  move — don't skip it to "lock faster".
- **LOCKED means direction, not schedule.** Locking never commits Denys to a
  tranche; sequencing stays his call. A locked line is reopenable — say so
  explicitly and edit the proposal, don't silently diverge from it.
- **Graduation:** when a tranche is scheduled, distill the proposal into a
  frozen ADR in `docs/decisions/` (rationale, not current state — per
  `docs/INDEX.md` conventions), mark the proposal `GRADUATED`, and point at the
  ADR. From then on the ADR is canonical; the proposal is history.

## Sizing novel work — slice, don't estimate (the one idea taken from spec-kit)

Most devclaw work is first-of-its-kind, so up-front scope is *genuinely*
unknowable — a spec template cannot manufacture certainty that doesn't exist
yet, and estimating "how big is the whole thing" is the wrong question. Don't
size the fog; **cut it into prioritized, independently-shippable slices and
size only the first:**

- Break the goal into `P1 / P2 / P3` slices where **each `Pn` is a standalone
  increment** that ships value and is independently testable — `P1` alone is a
  viable result, not a fragment that only pays off once `P3` lands.
- **Firm and size the `P1` slice only** — in devclaw's own units (N PRs, an
  end-of-week cap; never "some unknown number of sessions"). Leave `P2`/`P3`
  named-but-unsized until `P1` lands and the fog clears.
- The clarify `[OPEN]` step decides **the `P1` boundary**, not the whole arc.
  On novel work, session 1's deliverable is *the firmed `P1` slice*, not the
  feature — mirroring the goal engine's own `investigating → firming →
  executing` (you're doing to your sessions what devclaw does to its goals).

This is the antidote to "I can't prognosticate the scope": you stop trying to,
and instead bound the smallest thing that ships.

## The hard rule

**A behavior-changing tranche starts from a LOCKED proposal or an accepted
ADR. No code before lock.**

Out of scope (these follow their existing rules, not this one): bug fixes,
incident response, mechanical refactors, docs/test-only changes, and single
bounded PRs the user directly requests. The rule targets the failure mode of
*multi-PR direction changes* built on a decision that only ever existed in one
session's context window.

## Hygiene

- A proposal landing in the repo (or changing status) updates its row in
  `docs/INDEX.md` in the same PR — same honesty contract as every other doc.
- Proposals reference, never restate, the invariants in `CLAUDE.md` /
  `architecture.md`. If a proposal needs an invariant changed, it must say so
  in its own words — that's a headline, not a footnote.
- Vault-side proposals (`~/memory/projects/devclaw/proposals/`) are Denys's
  personal staging; a proposal becomes binding on this repo only once it (or
  its distillation) lands under `docs/proposals/` or `docs/decisions/`.
