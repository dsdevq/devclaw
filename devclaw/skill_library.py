"""Per-project skill library — provision tech-stack briefings into a workspace.

Background. The runner (``openhands-runner/runner.py``) loads three sources
of skill markdown into the agent prompt before each task:

  1. ``/opt/devclaw/skills/`` — universal devclaw doctrine (baked into the
     sandbox image: ``_common.md`` + ``_writes-code/`` + ``<kind>/``).
  2. ``<workspace>/.agent/skills/`` — per-repo observations (PR #135: e.g.
     "App.tsx is a 1827-line monolith"). The repo commits these.
  3. Whatever the runner's prompt-builder otherwise pulls in (AGENTS.md, etc.).

What was MISSING: a way for a GOAL to declare "I need React + .NET briefings
even though this repo doesn't ship any" without committing those into the
repo (they don't belong there — they're tech-stack doctrine, not repo facts).

This module is the missing layer:

  - A skill **library** on the host, default ``/opt/devclaw/skill-library/``,
    env-overridable via ``DEVCLAW_SKILL_LIBRARY`` (a directory of single-file
    skills: ``<library>/<slug>.md``). Library is curated, not auto-grown.
  - A **provisioning** call that copies the requested skill files into
    ``<workspace>/.agent/skills/<slug>.md`` so the runner's existing per-repo
    catch-all (any ``*.md`` at the root) picks them up. No runner change
    required.
  - ``Goal.skills_required`` (list of slugs) declares what to provision.
  - Admission validates that declared slugs exist in the library.

v1 limit (deliberate): one file per skill. The runner's per-repo loader has
a tiered layout (``_common.md`` / ``_writes-code/`` / ``<kind>/``) but
preserving that requires either a runner change or nested catch-all logic
that doesn't exist. A single concise file per skill is enough to start and
forces concision (good).

Empty/missing library is a soft failure: provisioning is a no-op and
admission emits a warning (not a rejection) so dev environments without a
populated library still work. Adding the library + skills is its own task.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

#: default library location. Mirrors the runner's ``/opt/devclaw/skills/``
#: convention so a host operator can ship both with one provisioning step.
DEFAULT_LIBRARY = "/opt/devclaw/skill-library"


def library_path() -> Path:
    """The active skill-library directory. Env override for dev/tests; the
    default points at the production location baked into the host image."""
    return Path(os.environ.get("DEVCLAW_SKILL_LIBRARY") or DEFAULT_LIBRARY)


def list_available(library: Path | None = None) -> list[str]:
    """Return slugs of every skill in the library, sorted. A slug is the
    basename (sans ``.md``) of any ``*.md`` file directly under the library
    root. Returns ``[]`` when the library doesn't exist (the dev-env case)."""
    root = library or library_path()
    if not root.is_dir():
        return []
    return sorted(
        p.stem for p in root.glob("*.md")
        if p.is_file() and not p.name.startswith("_")
    )


@dataclass(frozen=True)
class ProvisionResult:
    """Outcome of a provisioning call. ``provisioned`` is the slugs whose
    files were copied; ``missing`` is the slugs the caller requested that
    weren't in the library (caller decides whether to surface as warning or
    error — admission does both: warns when the library is empty, includes
    the slug in `unknown_skill_required` when the library has things but
    not this one)."""

    provisioned: list[str]
    missing: list[str]
    library_existed: bool


def provision(workspace_dir: str | Path, skills_required: list[str]) -> ProvisionResult:
    """Copy each requested skill's markdown into the workspace so the runner's
    per-repo loader picks it up. Idempotent: re-running overwrites the
    existing file at the same path. Never deletes (an operator-committed
    repo skill at the same name takes precedence on the next call if the
    caller doesn't request it again, since we only write requested slugs)."""
    library = library_path()
    if not skills_required:
        return ProvisionResult(provisioned=[], missing=[], library_existed=library.is_dir())
    if not library.is_dir():
        # No library → everything is "missing" from the caller's view, but
        # we don't error. Admission's warning surfaces this to the operator.
        return ProvisionResult(
            provisioned=[], missing=list(skills_required), library_existed=False,
        )

    target_dir = Path(workspace_dir) / ".agent" / "skills"
    target_dir.mkdir(parents=True, exist_ok=True)

    provisioned: list[str] = []
    missing: list[str] = []
    for slug in skills_required:
        src = library / f"{slug}.md"
        if not src.is_file():
            missing.append(slug)
            continue
        shutil.copyfile(src, target_dir / f"{slug}.md")
        provisioned.append(slug)

    return ProvisionResult(
        provisioned=provisioned, missing=missing, library_existed=True,
    )
