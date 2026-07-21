# ADR 0005 — Generic sandbox: project-declared toolchain, not per-stack images

- **Status:** accepted 2026-07-21 (Denys). Tranche scheduled same day —
  graduated from the vault proposal
  `~/memory/projects/devclaw/proposals/2026-07-11-generic-sandbox-project-declared-toolchain.md`
  under the spec lifecycle (direction agreed in principle 2026-07-11: "it
  should be generic… we should not hardcode it"; the four open items were
  answered in the 2026-07-21 clarify step, recorded below). This record
  freezes the *decision and rationale*; system snapshots inside reflect their
  writing date.
- **Supersedes:** the per-stack sandbox image family — `devclaw-sandbox` +
  `.sandcastle/Dockerfile.dotnet` (`devclaw-sandbox-dotnet`), whose own header
  called itself "the pragmatic first step" pending exactly this fix.
- **Relates:** the per-project-runner ownership boundary (vault proposal
  2026-07-01 — same principle: which SDK a project needs is the *project's*
  fact); lifekit-stack#93 (deploy verified the image nothing spawns — the
  failure class this ADR removes structurally); ADR 0002 (engine mode — the
  provisioning pre-step lives in the runner, so it rides every engine that
  runs `runner.py`).

## Context

Production tasks ran in `devclaw-sandbox-dotnet:local`, an image hand-built
2026-06-28 from `Dockerfile.dotnet` with no compose service, no build script,
no owner. Meanwhile `deploy.sh` rebuilt and md5-verified `devclaw-sandbox` —
the image nothing spawned — so two weeks of deploys reported ✓ while workers
ran a June-28 `runner.py` with no skills bundle at all (silent legacy
fallback).

Per-stack images are the wrong curve:

- **Unmanaged by construction.** Every new stack = a new multi-GB image
  (dotnet is 5.6 GB) + a new unmanaged build + a new verify gap.
- **Wrong ownership.** Which SDK a project needs is the project's fact
  (`global.json`, `package.json` engines, `.tool-versions`). Baking it into a
  devclaw image makes devclaw the owner of every target stack.
- **Worse reproducibility.** A shared image pins ONE SDK set for all projects
  — dropping .NET 9 from the image broke the finance-sentry-v3 run mid-flight
  (2026-06-26) even though that project's `global.json` never changed.

## Decision

**One lean base image = the harness layer only. The toolchain is a
project-declared fact, provisioned by mise inside the sandbox at task start.**

1. **Base image** carries python + node + claude CLI + ACP + the runner + a
   pinned `mise` binary + the small apt set of system libraries mise cannot
   install (the ICU/OpenSSL/krb5 set .NET needs). That apt list is the one
   place stack knowledge legitimately remains, and it stays small.
2. **Provisioning is a runner pre-step** (in-sandbox, before the agent
   starts) — the host stays generic and never learns stack facts. The runner
   detects the project's declaration and provisions it, then exports the
   resulting environment (`mise env`) into its own process env so the agent's
   shells AND the verify gate inherit the same toolchain — "`dotnet test`
   must find the mise-installed SDK" is handled structurally, not per-stack.
3. **Detection order:** a mise-native file (`.mise.toml` / `mise.toml` /
   `.tool-versions`) wins and is installed as-is. Otherwise idiomatic
   declarations are *translated* — `global.json` `sdk.version` →
   `dotnet@<major.minor>` (fuzzy, matching `rollForward` reality),
   `package.json` `engines.node` → `node@<version prefix>` — into mise
   config written in the container home, **never into `/workspace`** (a
   generated file in the workspace would dirty the diff the review gate and
   delivery see). No declaration → no-op: the base python+node, zero
   provisioning latency — today's default-image behavior.
4. **Fail CLOSED, legibly.** Any provisioning failure — including a declared
   toolchain with no `mise` on PATH (a stale image / deploy skew, exactly the
   lifekit-stack#93 class) and a declared-but-unparseable declaration file —
   settles the task `error` with a `toolchain_provision_failed:` reason. It
   is never a silent skip: silently running a .NET goal on a python+node box
   is the silent degradation this repo's hardening philosophy forbids. In
   host mode (`DEVCLAW_ENGINE=host`) this makes mise a host prerequisite for
   projects that declare a toolchain; the error says so.
5. **Per-project cache volume.** Each sandbox mounts a named docker volume
   `devclaw-toolchains-<slug>-<hash>` (derived from the host workspace path —
   the project identity axis) at mise's data dir. First task per toolchain
   version pays the download; subsequent tasks start in seconds. Volumes are
   per-project by decision (clarify step): isolation over cross-project cache
   reuse — one project's task can never poison another project's toolchain.
6. **Per-project `sandbox_image` override** in the project registry (PR 2) —
   the exotic-needs escape hatch and the migration bridge (.NET projects pin
   `devclaw-sandbox-dotnet:local` until the live gate passes).
7. **Greenfield goals** (a goal implies a stack the repo doesn't declare):
   the decomposer/firming prompts direct the first checklist item to create
   the declaration file — the project then owns it forever (PR 3, in this
   tranche by decision).

### Clarify-step answers (2026-07-21, Denys)

| Open item | Decision |
|---|---|
| Where provisioning runs | **Runner pre-step** (in-sandbox, fail-closed) — host stays generic |
| .NET backend | **mise only, no per-ecosystem seam** — purest form; if the community dotnet plugin proves flaky at the live gate, that is an explicit ADR reopen, not a silent workaround |
| Cache volume | **Per-project volumes** — isolation over shared-cache reuse |
| Greenfield declaration | **In this tranche** — decomposer/firming emit the declaration file as the first checklist item |

### Rejected alternatives

| Option | Verdict |
|---|---|
| One fat everything-image | Permanent staleness + size; still devclaw-owned toolchains |
| Per-stack variants (status quo) | What failed; doesn't scale past 2 stacks |
| Dynamic per-project image generation | Hold — a cold-start optimization *of* the mise path (bake the provisioned toolchain into a cached image later if measured latency hurts), not a competing architecture |
| Respect project `.devcontainer/` | Later escape hatch — same declaration principle, industry standard; not the first move |
| mise + official `dotnet-install.sh` seam for .NET | Declined in the clarify step — one backend, no seam; reopen explicitly if the live gate refutes it |

## Consequences

- **Deploy simplification falls out**: one image to build, one to verify —
  lifekit-stack#93's failure class disappears structurally.
- **Cold start**: first task per toolchain version downloads SDKs (minutes),
  then cached per project. The runner emits a provisioning event with tool
  list + duration so the cost is *measured* before anyone optimizes it away
  with baked images.
- **Network dependency at sandbox start** — already exists (claude OAuth).
- **Per-project volumes accumulate** — `docker volume ls
  --filter name=devclaw-toolchains-` + prune is an operator concern, not
  auto-reaped (a toolchain cache is cheap to lose but also cheap to keep).
- **Concurrent tasks on one project share a volume** — mise's own file
  locking serializes concurrent installs of the same tool.

## Tranche shape

1. `feat(engine)` — this ADR + mise in the base image + the runner
   provisioning pre-step + the per-project cache volume mount; stubbed
   regression tests.
2. `feat(registry)` — per-project `sandbox_image` override riding
   `EngineRequest`.
3. `feat(goal)` — the greenfield declaration nudge in decomposer/firming.
4. **Gated:** drop `Dockerfile.dotnet` + lifekit-stack deploy variant
   handling — only after one real .NET goal (finance-sentry class) runs
   green on the mise-provisioned path. Until then the dotnet image stays and
   .NET projects ride the `sandbox_image` override.
