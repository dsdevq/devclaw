# Proposal — issue-driven continuous development: GitHub Issues as the control plane, devclaw as the engine

- **Status:** **DRAFT — deliberately split (2026-07-23).** After an honest scope
  review (§0.5) the grand "control plane" vision (§0–§5) is **recorded but
  DEFERRED** — a good direction that is *early*, kept intact for a future return.
  Only a small **NOW slice** (§6) is live work: collapse the source of truth + make
  the loop we already have legible. No `[OPEN]` resolved yet; the clarify step (§7)
  is only needed if/when the deferred vision is revived.
- **Date opened:** 2026-07-23 · **Authors:** Denys + Claude (captured from a
  four-message design conversation)
- **Relates to:**
  - [`self-issue-filing.md`](./self-issue-filing.md) — the **embryo** of this: the
    GATHER→FILE→FIX→CLOSE loop already files issues on the devclaw repo and (Stage 2
    P2-pickup, live 2026-07-23) picks up `accepted`-labeled issues and proposes fixes.
    This proposal **generalizes that pickup** from *self-filed issues on one repo* to
    *any labeled issue on any owned repo*.
  - [ADR 0003](../decisions/0003-goal-program-unification.md) — one primitive, one
    dial (`create_goal(mode=long_lived|one_shot)`). The "issue = bounded unit" intake
    reuses the `one_shot` shape.
  - [ADR 0007](../decisions/0007-gate-strictness-dial.md) — the per-goal trust dial;
    the per-repo manifest (§4) hosts a repo-level default for it.
  - `console-operator-surface.md` P2 — the problem-lifecycle tracker + analytics that
    the §3 projection layer feeds.
  - Backlog: **#329** (Backlog = GitHub Issues, the decided substrate).

---

## 0. What this is (framing)

devclaw does two *kinds* of work, and today they're tangled. Name them, separate
them, and give the second one a real front door.

```
  Thread 1 — GOALS                    Thread 2 — the QUEUE (per-repo pipeline)
  ────────────────                    ────────────────────────────────────────
  durable, exploratory                bounded, reactive
  "pursue this functionality,         a GitHub Issue → picked up → executed
   build the app"                      → PR → issue closed
  re-planned per tick                 one-shot per issue (mostly)
  devclaw works ON THE WORLD          devclaw works THROUGH A DEFINED UNIT
```

The two threads must **not overlap**: goals build products; the queue executes
discrete units of work expressed as GitHub Issues. But they feed **one execution
engine** (layers 4→5) — this is two *intake fronts*, not two engines.

The instance of the queue pointed at the **devclaw repo itself** is
*self-improvement* (the existing self-issue-filing loop). But the mechanism is
generic: **`(repo, label) → dispatch → PR → close`.** Pointed at **finance-sentry**
(which devclaw owns), the same machinery is *product maintenance / continuous
development*. **Name the instances; keep the mechanism generic** — the same
discipline the ops-agent north star already demands.

**The headline this proposal is really about:** *file a GitHub issue on an owned
repo, an autonomous engine ships you a reviewed PR.* That is a strong legibility /
CV artifact (scores well on the learning+portfolio scoreboard) — far more legible
than invisible reliability work.

---

## 0.5 Reality check — what's NOW vs DEFERRED (honest scope review, 2026-07-23)

Denys asked for a blunt verdict before committing time. Verdict: **the thinking is
sound, but building the full vision now is priority-inverted.** The grand version is
a *distribution layer for an engine that isn't reliable enough to distribute yet.*
Four reasons it's early:

1. **Front door on a cracked foundation.** devclaw's own #1 problem is "hits a stupid
   error and just fails" (`reliability-trust-regression`). A per-repo pipeline just
   feeds work into a loop that still wedges — faster failures, not fewer. Intake is
   not the bottleneck; the engine is.
2. **P2 would industrialize the worst recent failure.** finance-sentry-ui was
   *cancelled* 2026-07-23 as a tar-pit (gate-passable-but-subtly-wrong PRs, worker
   routing around constraints — #358). Auto-piping issues into finance-sentry now
   mass-produces exactly those PRs, unattended.
3. **The interface isn't even used.** Denys doesn't drive devclaw through Telegram
   ("fine for now"). A control plane is architecture for a fleet of one; nobody feels
   the intake-ergonomics problem it solves.
4. **Against the scoreboard.** Most of the vision is invisible plumbing
   (reconciliation, webhooks, manifest schemas) dressed as legibility. The *actually*
   legible artifact — "file an issue → reviewed PR" — already exists ~80% as
   self-issue-filing on the devclaw repo; it's just not surfaced.

**The split:**

- **NOW (small, real, CV-positive)** — §6 "NOW slice": collapse the source of truth
  (`problems` → Issues) + make the existing self-issue-filing loop *legible* in the
  console. Days, not a tranche.
- **DEFERRED (recorded, unscheduled)** — the full control plane: per-repo pipelines,
  the `.devclaw/` manifest (§4), webhook discovery, multi-repo, intake unification.
  Good direction, kept intact below for a future return. **Precondition for reviving
  it: the reliability wall is down** (amnesiac-retry etc.) — that's what would make an
  automated multi-repo pipeline *safe*, and it comes first regardless.

Everything from §1 onward is the **full vision as recorded** — read it as the deferred
target, not the immediate build. The immediate build is §6 "NOW slice" only.

---

## 1. The source-of-truth decision (the starting question)

Today there are **two** stores of "what's wrong / what to do": devclaw's internal
`problems` catalog (`StateStore.record_problem`, `list_problems`) and GitHub Issues
on the devclaw repo. Denys wants **one**. Decision (consistent with #329, already
made — just never finished): **GitHub Issues is canonical.**

**But split by *what kind of truth*** — this is the load-bearing nuance:

| Truth | Canonical store | Why |
|---|---|---|
| **Intent / backlog** (what to do, bugs, ideas) | **GitHub Issues** | legible, human-labelable, portable, one substrate |
| **Execution state** (in-flight, transitions, CAS) | **SQLite** (`GoalStore`/`TaskQueue`) | GitHub API has **no compare-and-swap**, rate limits, eventual consistency |

⚠️ **The invariant this protects:** "Single writer to state" +
`GoalStore.transition()` as the CAS'd choke point (CLAUDE.md). GitHub Issues as the
*real state store* would break the guarantee that stops two writers clobbering each
other — **there is no CAS in the GitHub API.** So Issues are an **intent + trigger
surface** (like `STATUS.md` today: a legible view, written *to*, never read back for
a transition decision). devclaw **reconciles** issue state into SQLite and executes
there.

The internal `problems` catalog does **not** vanish — it becomes the *gatherer* that
*feeds* Issues (exactly the self-issue-filing GATHER→FILE edge), not a second
backlog a human reads.

---

## 2. Two intake modes, one engine

An issue is usually **smaller** than a durable goal — one bounded fix. That's the
**v1 task-runner shape** (`one_shot` / `fix_bug` / `dispatch_task`) that got buried
under durable goals (`helper-to-poc-drift`). So:

- **Default: an issue dispatches as a bounded one-shot task/program** — pick up,
  do, PR, close. Reuses v1; tolerates branch-off-`main` cleanly (sidesteps the
  `speckit-handoff-gap` "can't continue a feature branch" snag, which only bites the
  durable-goal path).
- **Escalate to a full durable goal only when the issue is genuinely open-ended.**

This is the key `[OPEN]` that decides the **P1 boundary** (see §7 O1).

---

## 3. Don't be limited by GitHub's native surface (the analytics ask)

Denys: "I don't want to be limited by GitHub statistics — get all issues, closed,
resolved, aggregated." GitHub Issues is canonical for *intent*, but its native
console view is thin. Keep a **thin projection / read-layer** over Issues (state
counts, open/closed/resolved, aggregates, recurrence) so the console isn't capped by
GitHub's surface. This is where console P2's **problem-lifecycle tracker**
(identified→filed→fixing→resolved) already points — reuse it, don't build a second.

---

## 4. Governance: the in-repo manifest (how a repo is "governed by devclaw")

Denys's instinct — and it's *right*: devclaw governing a repo should be a **visible,
opt-in, in-repo contract**, not invisible magic reaching in from outside. And the
repo must **live without devclaw** — rip the tool out and the project still builds,
tests, and runs.

**This extends a commitment the codebase already made.** Layer 5 is model-agnostic on
purpose: skills are plain markdown in `.agent/skills/`, hooks are bash, discovery is
`ls + cat`, "swap the agent, only the caller changes." The manifest is the **same
idea one level up** — an inert, declarative, in-repo file describing the *governance
contract*, never a runtime dependency:

```
.devclaw/config.yml          # illustrative — schema is [OPEN] O3
  triggers:
    accepted: fix            # which label dispatches which kind of work
  verify_cmd: "dotnet test"
  review: strict | default   # per-repo default for the ADR 0007 trust dial
  branch: goal/<id>          # or a feature-branch policy
  # optional: done_when defaults, allowed paths, escalate-to-goal rules
```

Two things this buys, both resolving open problems:

1. **Governance decentralizes to the repo.** "Auto-pickup across arbitrary repos =
   blast radius" — the manifest **is** the allowlist: *presence of `.devclaw/config.yml`
   = the repo opted in, here are its rules.* Consent travels with the repo. Far more
   legible than a hidden central list.
2. **"Lives without devclaw" is literally true.** The manifest is inert yaml/markdown
   describing intent, never code. Delete devclaw → the repo is untouched. Same
   model-agnostic invariant, applied to the governance surface.

**The one honest split (don't collapse it):**

- **In-repo `.devclaw/config.yml`** = *how to treat this repo* (the contract; opt-in;
  portable).
- **A thin central side** = *which repos to watch* (a subscription list, or better a
  **GitHub webhook** so an issue-label event *pushes* to devclaw instead of devclaw
  polling — reuses the `event-driven-loop` idea seed).

The manifest answers "what are the rules"; the central/webhook side answers "who's in
the club." Neither is invisible.

---

## 5. The pipeline (the loop this creates)

```
  issue labeled `accepted`  ──▶  discovery (webhook / poll)  ──▶  reconcile into SQLite
        │                                                                │
        ▼                                                                ▼
   read .devclaw/config.yml (verify_cmd, review, triggers)      dispatch bounded task (§2)
        │                                                                │
        ▼                                                                ▼
   sandbox execute (layer 5)  ──▶  gates (per-repo trust dial)  ──▶  PR  ──▶  close issue
```

This is **issue-driven continuous development**: the FILE→FIX half already exists for
the devclaw repo; §4–§5 generalize it to any owned repo behind the manifest.

---

## 6. Sizing — the NOW slice vs the DEFERRED arc

### NOW slice (the only live work — small, real, CV-positive)

Two bounded pieces, filed as GitHub Issues on the devclaw repo. Neither is a
multi-PR direction tranche; both are the kind of single bounded change the
spec-lifecycle rule explicitly leaves *out* of the lock requirement.

- **N1 — collapse the source of truth.** GitHub Issues become canonical for
  *intent* (Issues for backlog, SQLite for execution state — §1). Demote the internal
  `problems` catalog to the **gatherer that feeds Issues** (the existing FILE edge),
  not a `list_problems`-read second backlog. Hygiene finishing an already-made
  decision (#329).
- **N2 — make the loop we already have legible.** The self-issue-filing FILE→FIX
  loop is live but invisible; surface it as the CV artifact in the console (it feeds
  the console P2 problem-lifecycle tracker: identified→filed→fixing→resolved). No new
  engine work — expose what exists.

*Everything else about the manifest, per-repo pipelines, and webhooks is NOT in the
NOW slice.*

### DEFERRED arc (recorded, unscheduled — revive only after the reliability wall is down)

Kept intact in §1–§5 as the target direction. **Do not schedule until amnesiac-retry
/ worker-integrity reliability work has landed** — that is the precondition that makes
an automated multi-repo pipeline safe, and it comes first regardless.

- **D1 — the per-repo pipeline**, defined by the `.devclaw/` **manifest** (§4) +
  webhook/subscription discovery; first external instance = finance-sentry. The
  "continuous development" moment — and the one that, done early, would mass-produce
  the subtly-wrong PRs that got finance-sentry-ui cancelled (§0.5).
- **D2 — intake unification + analytics projection.** Firm issue→one-shot vs
  issue→goal (§2, `[OPEN]` O1); build the projection/read-layer over Issues (§3).

When D1/D2 are revived, the §7 clarify step becomes mandatory again.

---

## 7. Clarify step — `[OPEN]` (applies to the DEFERRED arc only; mandatory before D1/D2 revive)

*The NOW slice (§6 N1/N2) is bounded single changes, out of the lock requirement.
These `[OPEN]`s gate the deferred control-plane arc when it's picked back up.*

- **[OPEN] O1 (decides the P1 boundary).** Does an issue dispatch as a **bounded
  one-shot task** or always spawn a **durable goal**? *Claude's recommendation:
  bounded-by-default, escalate to goal only when open-ended — reuses v1, sidesteps
  branch-off-main, keeps goals for genuine product-building.*
- **[OPEN] O2.** Retire the internal `problems` catalog outright, or keep it as a
  pure **gatherer that mirrors into Issues** (never a human-read backlog)?
  *Recommendation: keep as gatherer/mirror — it's the recurrence signal the FILE edge
  already needs; only its status as a `list_problems`-read backlog is retired.*
- **[OPEN] O3.** Manifest schema + location: `.devclaw/config.yml`, or fold into the
  existing `.agent/` surface? Minimum viable fields? *Recommendation: separate
  `.devclaw/` (governance ≠ worker-craft), start tiny (`triggers`, `verify_cmd`,
  `review`), grow only as a slice needs it.*
- **[OPEN] O4.** Discovery: **webhook-push** vs **poll** for labeled issues?
  *Recommendation: webhook (event-driven-loop reuse); poll as the reconciliation
  fallback — mirrors the heartbeat-as-fallback pattern.*
- **[OPEN] O5.** Goal↔issue **collision** on the same repo — a durable goal and an
  issue-pickup both branching off `main` of e.g. finance-sentry. Ownership/locking
  rule? (One-shot-per-issue tolerates it; two writers to the same branch don't.)
- **[OPEN] O6.** Auto-close semantics: close the issue on **PR merge** (human merges)
  or on **PR open**? Age-out of stale issues stays as self-issue-filing already
  defines it? *Lean: close on merge; the human merge is the backstop (ADR 0007).*
- **[OPEN] O7.** Does the per-repo trust default in the manifest (§4) *override* or
  *seed* the per-goal `set_goal_strictness` dial (ADR 0007)? *Lean: seed — the goal-
  level dial still wins.*
- **[OPEN] O8.** Scope guard: is **Telegram/OpenClaw intake** in scope here? Denys
  parked it ("fine for now"). *Recommendation: explicitly out of scope; Issues are
  the front door, chat stays the console co-pilot (console P3).*

---

## 8. Invariants this must respect (references, not restatements — per hygiene)

- **Single writer to state + CAS choke point** — Issues never become the
  transactional store (§1). SQLite stays canonical for execution state.
- **OAuth only** — the pickup worker runs the same `claude`-over-OAuth path; no new
  metered surface.
- **Model-agnostic worker layer** — the manifest is inert declarative config (§4),
  not vendor tool-wiring; same philosophy as `.agent/skills/`.
- **Verification fails closed** (recalibrated by the ADR 0007 trust dial per repo).
- **Zero-token idle guard** — webhook/poll discovery must not add an LLM call to an
  idle tick path; dispatch cognition runs only when there's a real issue to act on.
- **Domain specifics at the edges** — "GitHub Issues", "PR", "labels" are edge
  concepts (worker skills, delivery, this pipeline), not new plumbing baked into
  layers 1–4. The loop stays domain-agnostic.

---

## 9. Out of scope

Telegram intake (O8), a second cognition primitive, and any change to the durable-goal
re-planning cadence. This proposal adds an *intake front + governance contract*; it
does not touch the goal state machine or the heartbeat.
