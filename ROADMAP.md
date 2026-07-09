# Roadmap — versions as promises

A **version is a frozen promise**, not a bucket of features. Each version has three parts, written **before** any code:

1. **Promise** — one sentence of user-facing capability.
2. **Proof** — the measurement that shows the promise is true. Pass/fail threshold decided up front; no advancing on vibes.
3. **Not-list** — things explicitly out of this version. Work discovered mid-version goes to the candidate list below, never into the version. Only bugs that block the current promise may interrupt.

A version ends with a **verdict**: run the proof, write it up (promise / score / failure buckets / passed-or-failed / implication for the next promise), tag the repo, cut a release. A failed proof means the next version is *the same promise* with fixes.

Failure buckets for autopsies:

- **Harness** — planner/gate/context did the wrong thing → devclaw's job, fix it.
- **Model** — the model can't do it even with perfect scaffolding → stop building that part, wait for models.
- **Spec** — the ticket was under-specified → upstream of the harness; fix ticket-writing, not devclaw.

Sizing: 1–2 weeks per version, one promise each. Work on devclaw itself is only dispatched if it serves the **current** version's promise.

---

## ▶ Current — v0.1 "Bounded tasks, proven"

Tracking: [#178](https://github.com/dsdevq/devclaw/issues/178) · Milestone: [v0.1](https://github.com/dsdevq/devclaw/milestone/1)

**Promise:** A bounded ticket (`fix_bug` / `implement_feature`) on a registered project produces a PR merged **without rework** at least **6/10** of the time.

**Proof:** 10 real tickets across ≥2 registered projects; score = merged-without-rework / 10 (rework = any human commit on the PR branch, or a caused follow-up fix ticket within 48h); every failure autopsied into harness/model/spec. **Pass: ≥6/10 with ≤2 harness failures.**

**Not-list:** goal-layer improvements · firming phase / PhaseHandler migration · E2E test layer · ops-agent autonomy · self-hosting rungs 2–3 · multi-subagent toolbox · new console features.

**Verdict:** _pending._

## Ladder

Ordered by risk, not architecture. Each rung's three parts get written only when it becomes current, informed by the previous verdict.

- **v0.2 — Bounded tasks, hardened.** Fix v0.1's harness-bucket failures; re-run the proof at ≥7/10. (Skipped if v0.1 passes clean.)
- **v0.3 — Programs earn their keep.** A multi-task `start_program` completes at the v0.1 per-task bar with no human sequencing.
- **v0.4 — The goal layer earns its keep.** A standing goal over ≥1 week produces net-positive merged work vs. the same tickets dispatched by hand.
- **v0.5 — Unsupervised overnight.** A night-window run ships merged work with zero morning cleanup, N nights in a row.

## Candidate list

Parked work, considered only at version boundaries:

- Firming phase + PhaseHandler pattern → likely v0.2/v0.3
- E2E test layer → likely v0.2
- Ops-agent stuck-owner loop → v0.5
- Self-hosting ladder rungs 2–3 (canary, auto-rollback)
- Multi-subagent toolbox (judge panel, fan-out) — only if a proof failure points at review quality
- devclaw-owns-its-Dockerfile move — tied to the portability spike
