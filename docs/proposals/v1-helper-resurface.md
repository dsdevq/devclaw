# Proposal — re-surface the v1 task-runner helper (bounded task → PR, no goal ceremony)

- **Status:** **P1 LOCKED (direction) — 2026-07-23.** The direction (re-surface the
  existing direct task-runner as a first-class daily-driver, and give it a
  caller-chosen delivery target) is endorsed — it's the named follow-on from the
  console proposal's `[OPEN] #7` lean ("commandable daily driver") and it closes the
  `speckit-handoff-gap`. All P1 `[OPEN]`s are resolved below (§7). **LOCKED = direction,
  not schedule** — the tranche is Denys's to sequence; no P1 code lands outside it.
  P2/P3 are named-but-unsized.
- **Date opened / firmed:** 2026-07-23 · **Authors:** Denys + Claude
- **Grounded on:** a read-only investigation of `main` @ `c71449e` (findings in §2 are
  quoted from code, not assumed).
- **Relates to:**
  - `helper-to-poc-drift-2026-07-22` (vault) — the drift this reconciles: devclaw v1
    was a usable parallel task-runner; durable goals became the center of gravity and
    buried it.
  - `speckit-handoff-gap-2026-07-22` (vault) — the structural block this removes: every
    action branches `goal/<id>` off `main` and PRs to `main`, so devclaw can't continue
    an existing feature branch (never sees `specs/NNN/spec.md`).
  - `console-operator-surface.md` `[OPEN] #7` — the "lean toward a commandable daily
    driver" direction that names this follow-on.
  - ADR 0003 (goal↔program unification) — the durable-goal path is untouched; this
    re-surfaces the *other* intake (direct task), which already shares the same engine.

---

## 1. Framing — this is a re-surface, not a rebuild

devclaw's first useful form was a **direct task-runner**: hand it a bounded unit of
work, it runs it in a sandbox, gates it, and opens a PR — no durable goal, no
heartbeat, no per-tick planning. That path **still exists and still works end-to-end**
(§2). What happened is not rot; it's that the *center of gravity* moved to durable
goals, and the direct path lost its surface and one ergonomic capability:

1. **No surface points at it.** The tools are MCP-visible but nothing pitches them as
   the thing you drive daily, and the console can *show* a standalone task's output but
   can't *launch* one.
2. **It can only ever branch a fresh name off `main` and PR to `main`.** It cannot
   continue an existing feature branch or target a non-`main` base — the exact
   structural wall behind `speckit-handoff-gap`.

Re-surfacing = (a) a small delivery seam so a dispatched task can aim its branch/base,
and (b) a surface (console affordance + honest docs) that points at it. The runner
itself needs no rework.

## 2. Ground truth (quoted from code, not assumed)

- **The direct tools are live and wired.** `dispatch_task` (`server/tools.py:24`) calls
  `queue.submit(...)` directly → engine → gates → `deliver_change` — **no goal, no
  decomposer, no heartbeat.** `implement_feature` / `fix_bug` / `review_repository` are
  thin `DEPRECATED` forwarders onto it; `onboard` is another direct submit. (`start_program`
  is the one that was absorbed into the goal layer as `create_goal(mode=one_shot)`.)
- **The no-goal queue path is alive, not atrophied** — it's the *same* `_run_and_settle`
  / engine / verify / review / test-integrity / deliver machinery the goal layer drives
  per action. The goal layer is a *driver on top of* the queue, not a replacement.
- **Base=`main` lives in exactly two spots**, neither caller-controllable:
  `_default_base_ref` (`delivery/__init__.py:241`, `origin/HEAD`→`main`→`master`) and the
  **`--base`-less `gh pr create`** (`delivery/__init__.py:437`, so GitHub defaults the PR
  to the repo's default branch).
- **The branch-reuse capability already exists** — it's just gated to goals. Delivery's
  "stay on this branch + reuse its single PR" mode is switched on by
  `current.startswith("goal/")` (`delivery/__init__.py:330`). Widen that predicate and the
  same machinery serves any caller-pinned branch.
- **The workspace primitive is already generic.** `prepare_workspace(branch=...)`
  (`workspace.py:50`) fetches + checks out *any* branch and resets to its origin tip if it
  exists. The only reason a direct task doesn't get this is the queue never calls it
  (prep is a goal-layer step today).
- **The seam was pre-planned.** `goal/delivery_strategy.py` states it "is the seam a
  second topology (per-task PRs to main) plugs into later, instead of threading a new
  conditional through every call site," and already ships a `PerActionStrategy`.

**Conclusion:** the change is localized to ~one delivery file + one tool signature + one
queue wire. Not systemic.

## 3. The branch-off-main seam (the crux)

Give a dispatched task two optional delivery inputs:

- **`base_branch`** (default `main`) — the PR base. Threaded `dispatch_task` →
  `deliver_change` → `gh pr create --base <base_branch>`, and into `_default_base_ref`
  for the ahead-count/diff range.
- **`target_branch`** (default: the auto-derived `feat/…`/`fix/…` name, today's behavior)
  — when set, devclaw **continues that branch**: prepare the workspace on it
  (`prepare_workspace(branch=target_branch)`), commit onto it, push, and **reuse/open its
  single PR** by widening the `startswith("goal/")` reuse gate to "on a caller-pinned
  branch." If `target_branch` doesn't exist yet, create it off `base_branch`.

That is the whole structural fix. It turns "always a fresh branch → main" into "a
caller-chosen base, optionally continuing an existing branch" — which is exactly what
`speckit-handoff-gap` needs (continue a feature branch, PR to a feature base).

## 4. Slices (each `Pn` standalone-shippable; firm P1 only)

- **P1 (LOCKED, firmed §6) — the branch-target delivery seam.** `dispatch_task` gains
  `base_branch` + `target_branch`; thread them through `deliver_change` → `gh pr create
  --base` + the widened branch-reuse gate; wire `prepare_workspace(branch=target_branch)`
  on the direct path so continuing a branch actually works end-to-end. Backend-only,
  fully testable, ships value alone: devclaw can now continue an existing feature branch
  and target a non-`main` base — the `speckit-handoff-gap` wall comes down.
- **P2 (named, unsized) — the surface.** The one create-affordance the console lacks: a
  "file a task" panel (MCP `dispatch_task` under the hood) + honest docs pitching the
  direct runner as the daily-driver (not just the goal layer's substrate). Depends on
  P1 for the branch/base inputs to be worth exposing.
- **P3 (named, unsized) — direct-path prep ergonomics.** Generalize workspace prep on the
  direct path (clone/reset like the goal path) so a bare `dispatch_task` against a fresh
  repo url "just works," and any remaining polish (per-task cleanup, cap interplay).

## 5. P1 — firmed scope + task shape

**Deliverable:** a dispatched task can target a caller-chosen base and continue an
existing branch, end-to-end through the real gates and delivery.

**Task shape (devclaw units, not calendar):** ~**2–3 PRs**, end-of-week cap:
1. Delivery seam: `base_branch`/`target_branch` params through `deliver_change` +
   `gh pr create --base` + widen the branch-reuse predicate (`delivery/__init__.py`).
   Named regression test (a task with `target_branch` continues it + reuses its PR; a
   task with `base_branch` PRs to that base).
2. Wire the params through `dispatch_task` (`server/tools.py`) + call
   `prepare_workspace(branch=target_branch)` on the direct path (`task_queue.py`). Test:
   a direct task lands on the target branch.
3. (If needed) doc the flow in `docs/flows/task-execution.md` + a short "direct task"
   note; INDEX currency.

**Non-negotiable invariants (P1 must hold — §8):** delivery still fails CLOSED (#183 — a
task that can't push/PR fails, never a silent success); the gates are unchanged; no new
metered/OAuth surface; `main`-branch guard behavior unchanged for devclaw's *own* repo.

## 6. Why this is worth doing (scoreboard fit)

Primary value is **usability / reconciling the drift**, and it lights up the
`[OPEN] #7` daily-driver direction: "hand devclaw a bounded task on a branch I choose,
get a reviewed PR" is the v1 helper Denys actually used, restored — and it's the
capability that lets devclaw take **speckit-shaped, continue-the-branch work** it
structurally couldn't touch. It's a small, high-leverage unlock, not invisible
hardening.

## 7. Clarify step — `[OPEN]` (resolved for P1; the direction is delegated-and-endorsed)

- **[RESOLVED] O1 — direction.** Re-surface the direct runner as a daily-driver *and*
  fix the branch seam? **Yes** — endorsed via console `[OPEN] #7` + Denys's "do what you
  recommend" (2026-07-23).
- **[RESOLVED] O2 — P1 boundary.** P1 = the **delivery seam only** (`base_branch` +
  `target_branch` + the prep wire that makes them work). The console affordance is P2;
  direct-path clone ergonomics are P3. Rationale: the seam is the standalone value (the
  wall coming down); the surface is worth more once the seam exists.
- **[RESOLVED] O3 — `target_branch` that doesn't exist.** Create it off `base_branch`
  (mirrors today's fresh-branch behavior, just with a caller-chosen name/base).
- **[RESOLVED] O4 — PR reuse.** Reuse the existing goal-mode "one branch → one PR" reuse
  code path, widened from `startswith("goal/")` to "on a caller-pinned branch." No new
  reuse logic.
- **[RESOLVED] O5 — default behavior unchanged.** Both params optional; omitted ⇒
  byte-identical to today (fresh `feat/…`/`fix/…` branch → `main`). Existing callers and
  the goal layer are unaffected.
- **[DEFERRED → P2, owner: Denys] O6 — console create-surface + auth.** The "file a task"
  UI and its auth story (tailnet today) is a P2 concern, same as the console proposal's
  mutation-reach deferral.
- **[DEFERRED → P3, owner: Denys] O7 — direct-path workspace prep.** Whether a bare
  `dispatch_task` should clone/reset a fresh repo url itself (vs assume a prepped
  checkout) is a P3 ergonomics decision; P1 only preps the target branch of an
  already-known workspace.

## 8. Invariants (references, not restatements)

- **Broken delivery fails, never "done without a PR" (#183).** The new base/branch
  params must not open a silent-success path — a push/PR failure on a caller-chosen
  branch still settles the task `failed`. Named test.
- **Verification stays fail-CLOSED.** The gates (verify / review / test-integrity /
  browser-E2E) run unchanged on a direct task; the seam only changes *where the result
  is delivered*, not whether it's gated.
- **OAuth-only / model-agnostic / single-writer** — untouched; this is delivery +
  tool-signature plumbing, no cognition, no state-machine change, no new billing surface.
- **`main`-branch guard** (devclaw's own dev harness) is unrelated — this governs
  delivery to *target* repos, not commits to devclaw itself.

## 9. Out of scope

The durable-goal path (untouched), the console co-pilot chat (console P3.2, deferred),
and any change to the gate stack. This proposal re-surfaces an existing intake and adds
a delivery seam; it does not touch the goal state machine, the heartbeat, or cognition.
