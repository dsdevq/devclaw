"""task_intake — convert a natural-language Telegram intent into a structured TaskSpec.

This is the ONLY runner whose output isn't a Result. It's a Pydantic TaskSpec, written to disk as `~/.life/tasks/<task_id>/spec.yaml` (atomic) or `~/.life/projects/<project>/tasks/<task_id>/spec.yaml` (project-bound) — the dispatch cron (or per-task graph) picks it up from there.

Same subprocess shape as the other runners — `claude --print` with a tightly-scoped prompt that asks Claude to emit a single JSON line that we validate as a TaskSpec.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from pydantic import ValidationError

from orchestrator.dispatch import load_spec, now_utc, persist_spec
from orchestrator.runners._subprocess import run_claude
from orchestrator.state.models import (
    Budget,
    RequesterRoute,
    TaskKind,
    TaskSpec,
    TaskStatus,
)
from orchestrator.sweep import detect_cycle, find_all_specs

logger = logging.getLogger(__name__)

INTAKE_INDEX_FILE = "intake_index.json"


# Events announce callback shape — matches PR #21's daemon.AnnounceCallback
# (channel, target, message). Imported lazily inside the function bodies to
# avoid a top-level daemon import (intake is imported by mcp_server, which
# should not pull in the daemon's threading machinery).
_EventsAnnounce = Callable[[str, str, str], None]


def _noop_events_announce(channel: str, target: str, message: str) -> None:  # noqa: ARG001
    return None


# Markers that indicate a code task is likely to touch the shared SPA root —
# editing any of these from two parallel branches reliably produces a merge
# conflict. Compared case-insensitively against verbatim_intent and
# acceptance_criteria of both the new and prior specs. See
# proposals-approved/2026-05-19-intake-parallel-frontend-guard.md.
SPA_ROOT_MARKERS: tuple[str, ...] = (
    "app.tsx",
    "app.jsx",
    "main.tsx",
    "index.tsx",
    "api.ts",
    "api.js",
    "client.ts",
    "fetch.ts",
    "routes/",
    "router.tsx",
    "auth middleware",
    "auth header",
    "fetch wrapper",
    "toastprovider",
    "queryclient",
    "context provider",
    "theme provider",
)

# Statuses that count as "in flight" for the purposes of the parallel guard —
# a sibling spec in any of these is still occupying the shared SPA root.
_ACTIVE_STATUSES: set[TaskStatus] = {
    TaskStatus.ready,
    TaskStatus.dispatched_subagent,
    TaskStatus.dispatched_build,
    TaskStatus.dispatched_human,
}

# Doc-drift acceptance-criterion auto-append (Spec B of
# proposals/2026-05-20-doc-drift-automation-three-rung).
#
# Whenever a code task's `verbatim_intent` mentions a user-visible surface —
# README, docker-compose comments, dashboard UI, public CLI command/help text,
# a service rename — the runner must keep README.md and compose comments in
# sync with the post-change state. The intake step auto-appends a literal
# acceptance criterion so the contract is in scope from the start; the
# deterministic CI gate in spec A enforces it at PR time.
#
# Keep this keyword list deliberately small. False negatives are preferable
# to false positives: the CI gate is the safety net.
USER_VISIBLE_SURFACE_MARKERS: tuple[str, ...] = (
    "readme",
    "docker-compose",
    "compose/",
    "compose.yml",
    "compose.yaml",
    "dashboard ui",
    "--help",
    "public cli command",
    "rename the service",
    "service rename",
)

# The exact literal acceptance-criterion string. Must match what the
# deterministic CI gate (`scripts/check-doc-drift.sh` in spec A) asserts —
# anything that says the README and compose comments reflect post-change
# state passes both intake and CI.
DOC_DRIFT_ACCEPTANCE_CRITERION: str = (
    "README.md and compose comments reflect the post-change state "
    "(no stale service lists, command signatures, or claims)"
)


def _mentions_user_visible_surface(verbatim_intent: str) -> bool:
    blob = (verbatim_intent or "").lower()
    return any(marker in blob for marker in USER_VISIBLE_SURFACE_MARKERS)


def _has_doc_drift_criterion(criteria: list[str]) -> bool:
    """True if any criterion already promises README + compose stay in sync.

    Loose match (case-insensitive): both "readme" and one of "compose"/"docker-compose"
    appearing in the same criterion line is enough — we don't want to double-add a
    criterion the operator (or Claude) already wrote in their own words.
    """
    for c in criteria or []:
        low = c.lower()
        if "readme" in low and ("compose" in low or "docker-compose" in low):
            return True
    return False


def _spec_text_blob(spec: TaskSpec) -> str:
    parts = [spec.verbatim_intent or ""]
    parts.extend(spec.acceptance_criteria or [])
    return "\n".join(parts).lower()


def _mentions_spa_root(spec: TaskSpec) -> bool:
    blob = _spec_text_blob(spec)
    return any(marker in blob for marker in SPA_ROOT_MARKERS)


def _find_parallel_frontend_conflict(
    spec: TaskSpec, existing_by_id: dict[str, TaskSpec]
) -> str | None:
    """Return the task_id of the most-recent sibling spec that would conflict
    on the SPA root, or None if no conflict.

    Triggers when:
      - new spec has a target_repo, and
      - new spec's intent / acceptance criteria mention a SPA-root marker, and
      - at least one in-flight sibling spec (same target_repo) also mentions one.
    """
    if not spec.target_repo:
        return None
    if not _mentions_spa_root(spec):
        return None

    candidates: list[TaskSpec] = []
    for other_id, other in existing_by_id.items():
        if other_id == spec.task_id:
            continue
        if other.target_repo != spec.target_repo:
            continue
        if other.status not in _ACTIVE_STATUSES:
            continue
        if not _mentions_spa_root(other):
            continue
        candidates.append(other)

    if not candidates:
        return None
    candidates.sort(key=lambda s: s.created_at, reverse=True)
    return candidates[0].task_id


def _short_hex(n: int = 4) -> str:
    return secrets.token_hex(n // 2)


def _find_project_for_repo(life_root: Path, target_repo: str) -> str | None:
    """Look up which project owns `target_repo` via `projects/*/settings.yaml`'s `github_repo` field.

    Returns the project slug (directory name) or None if no match.
    On multiple matches, picks the project whose settings.yaml was modified most recently
    and logs a WARN.
    """
    matches: list[tuple[float, str]] = []
    for settings_path in life_root.glob("projects/*/settings.yaml"):
        try:
            data = yaml.safe_load(settings_path.read_text()) or {}
        except (yaml.YAMLError, OSError) as exc:
            logger.warning("task_intake: failed to read %s: %s", settings_path, exc)
            continue
        if not isinstance(data, dict):
            continue
        if data.get("github_repo") == target_repo:
            matches.append((settings_path.stat().st_mtime, settings_path.parent.name))

    if not matches:
        return None
    if len(matches) > 1:
        matches.sort(reverse=True)
        chosen = matches[0][1]
        logger.warning(
            "task_intake: target_repo=%s matched multiple projects %s; picking most recent: %s",
            target_repo,
            [m[1] for m in matches],
            chosen,
        )
        return chosen
    return matches[0][1]


def _build_intake_prompt(verbatim_intent: str) -> str:
    return f"""You are a task-spec generator. Convert the user's natural-language intent into a structured task spec JSON.

USER INTENT:
\"\"\"
{verbatim_intent}
\"\"\"

Decide:
  1. `kind` — one of: code (writes code, opens PR), research (synthesizes findings), draft (drafts a document), chore (mechanical bulk edit), decision (asks the user a yes/no/multi-choice). Default to `research` if unclear.
  2. `target_repo` — `<org>/<repo>` ONLY if kind is `code` AND the intent names a repo. Else null.
  3. `target_branch` — default "main" unless intent specifies.
  4. `project` — `<slug>` ONLY if intent clearly names an existing project (lifekit-stack, devclaw, finance-sentry, lifekit, swarm). Else null.
  5. `acceptance_criteria` — testable / observable criteria, max 5. For code: criteria the verifier can check via bash. For research/draft: criteria like "findings.md exists" or "covers section X". For chore: similar to code.
     If the intent touches a user-visible surface (README, docker-compose, dashboard UI, public CLI command name/help text, service rename), include a criterion of the form: "README.md and compose comments reflect the post-change state (no stale service lists, command signatures, or claims)" — intake also auto-appends this deterministically, so it's fine if you forget; don't double-list it.
  6. `budget_seconds` — default 1800 (30 min) for code/research/draft/chore. 3600 (60 min) for non-trivial research. Cap 14400 (4h).

Print a JSON object to stdout on the LAST line of your output (and nothing after it):
  {{"kind": "...", "target_repo": "..." or null, "target_branch": "main", "project": "..." or null, "acceptance_criteria": [...], "budget_seconds": 1800, "notes": "<one-line clarification of what you decided and why>"}}

Be conservative — if the intent is ambiguous, prefer kind=research and add a clarifying note. Don't invent acceptance criteria the user didn't imply.
"""


def intake(
    verbatim_intent: str,
    *,
    requester_route: RequesterRoute,
    life_root: Path | None = None,
    task_id: str | None = None,
    created_by: str = "task_intake",
) -> TaskSpec | None:
    """Convert NL intent into a TaskSpec and write it under ~/.life/tasks/.

    Returns the TaskSpec on success, None on failure.

    Failure modes — return None, log:
      - Claude subprocess timed out / errored / produced no parseable JSON
      - Claude's output fails Pydantic validation as a partial-TaskSpec
    """
    life_root = life_root or Path("~/.life").expanduser()

    sub = run_claude(_build_intake_prompt(verbatim_intent), timeout_seconds=300)
    if sub.parsed_json is None:
        logger.warning("task_intake: claude returned no parseable JSON: %s", sub.blocker)
        return None

    data = sub.parsed_json
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    derived_task_id = task_id or f"{today}-intake-{_short_hex(4)}"

    try:
        spec = TaskSpec(
            task_id=derived_task_id,
            created_at=now_utc(),
            created_by=created_by,
            requester_route=requester_route,
            verbatim_intent=verbatim_intent,
            kind=TaskKind(data.get("kind", "research")),
            acceptance_criteria=data.get("acceptance_criteria") or [],
            budget=Budget(max_runtime_seconds=int(data.get("budget_seconds", 1800))),
            target_repo=data.get("target_repo"),
            target_branch=data.get("target_branch", "main") or "main",
            project=data.get("project"),
            status=TaskStatus.ready,
            depends_on=list(data.get("depends_on") or []),
            parallel_safe=bool(data.get("parallel_safe", False)),
        )
    except (ValidationError, ValueError) as exc:
        logger.warning("task_intake: claude output failed validation: %s", exc)
        return None

    # Doc-drift auto-append: if the verbatim intent mentions a user-visible
    # surface (README, compose, dashboard UI, public CLI command name, service
    # rename), make sure the README/compose acceptance criterion is in scope.
    # See USER_VISIBLE_SURFACE_MARKERS above. Spec B of proposal
    # 2026-05-20-doc-drift-automation-three-rung.
    if spec.kind == TaskKind.code and _mentions_user_visible_surface(verbatim_intent):
        if not _has_doc_drift_criterion(spec.acceptance_criteria):
            new_criteria = list(spec.acceptance_criteria) + [
                DOC_DRIFT_ACCEPTANCE_CRITERION
            ]
            spec = spec.model_copy(update={"acceptance_criteria": new_criteria})
            logger.info(
                "task_intake: auto-appended doc-drift acceptance criterion for %s "
                "(detected user-visible-surface marker)",
                spec.task_id,
            )

    # Parallel-frontend-conflict guard: code tasks that touch the React SPA
    # root from two parallel branches reliably produce merge conflicts (see
    # 2026-05-19 lifekit-dashboard Tasks 5+6 incident). Force serial dispatch
    # via depends_on when we detect overlap with an in-flight sibling, unless
    # the operator explicitly opted in with parallel_safe: true.
    if spec.kind == TaskKind.code and not spec.parallel_safe:
        existing_by_id: dict[str, TaskSpec] = {}
        for sp in find_all_specs(life_root):
            try:
                loaded = load_spec(sp)
            except Exception:  # noqa: BLE001
                continue
            existing_by_id[loaded.task_id] = loaded
        prior_id = _find_parallel_frontend_conflict(spec, existing_by_id)
        if prior_id is not None:
            new_depends_on = list(spec.depends_on)
            if prior_id not in new_depends_on:
                new_depends_on.append(prior_id)
            new_notes = list(spec.notes)
            new_notes.append(
                f"Forced serial via parallel-frontend-guard: detected shared "
                f"SPA-root references with {prior_id}. Set parallel_safe: true "
                f"to override."
            )
            spec = spec.model_copy(
                update={"depends_on": new_depends_on, "notes": new_notes}
            )
            logger.info(
                "task_intake: parallel-frontend-guard forced %s to wait on %s",
                spec.task_id,
                prior_id,
            )

    # If the new spec declares dependencies, refuse to write it on disk when
    # the resulting graph would contain a cycle. Pure read of existing specs;
    # no writes happen until this check passes.
    if spec.depends_on:
        existing: dict[str, TaskSpec] = {}
        for sp in find_all_specs(life_root):
            try:
                loaded = load_spec(sp)
            except Exception:  # noqa: BLE001
                continue
            existing[loaded.task_id] = loaded
        cycle = detect_cycle(spec, existing)
        if cycle is not None:
            logger.warning(
                "task_intake: depends_on introduces a cycle %s — refusing to write spec %s",
                " -> ".join(cycle),
                spec.task_id,
            )
            return None

    # Pick the destination directory by looking up target_repo in projects/*/settings.yaml.
    # If target_repo is missing or no project owns it, fall through to the flat bucket.
    project_slug: str | None = None
    if spec.target_repo:
        project_slug = _find_project_for_repo(life_root, spec.target_repo)

    if project_slug:
        task_dir = life_root / "projects" / project_slug / "tasks" / spec.task_id
    else:
        task_dir = life_root / "tasks" / spec.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    persist_spec(spec, task_dir / "spec.yaml")

    logger.info(
        "task_intake: spec written at %s (kind=%s, project=%s, target_repo=%s)",
        task_dir / "spec.yaml",
        spec.kind.value,
        project_slug or spec.project,
        spec.target_repo,
    )
    return spec


# ─── Shared intake-from-prose surface (CLI + MCP + Telegram) ─────────────────


@dataclass
class IntakeResult:
    """Return shape of `intake_from_prose` — used by both CLI and MCP surfaces."""

    task_id: str
    spec_path: Path
    budget_min: int
    target_repo: str | None
    state: str  # "new" | "duplicate"


def _intake_hash(prose: str, from_surface: str) -> str:
    """Stable SHA-256 hash of (prose, from_surface) for idempotency keying.

    We use SHA-256 explicitly because Python's built-in `hash()` is salted
    per-process (PYTHONHASHSEED) and would produce a different value on every
    fresh interpreter — useless as a persistent dedup key.
    """
    h = hashlib.sha256()
    h.update(prose.encode("utf-8"))
    h.update(b"\x00")
    h.update(from_surface.encode("utf-8"))
    return h.hexdigest()


def _load_intake_index(life_root: Path) -> dict[str, str]:
    """Read the intake-dedup index. Returns {} if absent or malformed."""
    idx_path = life_root / INTAKE_INDEX_FILE
    if not idx_path.is_file():
        return {}
    try:
        data = json.loads(idx_path.read_text())
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("task_intake: failed to read %s: %s", idx_path, exc)
    return {}


def _save_intake_index(life_root: Path, index: dict[str, str]) -> None:
    idx_path = life_root / INTAKE_INDEX_FILE
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = idx_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, indent=2, sort_keys=True))
    tmp.replace(idx_path)


def intake_from_prose(
    prose: str,
    *,
    from_surface: str = "cli",
    life_root: Path | None = None,
    task_id: str | None = None,
    progress: callable = None,  # type: ignore[valid-type]
    events_announce: _EventsAnnounce = _noop_events_announce,
    events_chat_id: str = "default",
) -> IntakeResult | None:
    """Shared intake entrypoint used by every surface (CLI, MCP, Telegram handler).

    Idempotency: a SHA-256 of `prose + from_surface` keys the result. A second
    call with byte-identical inputs returns the SAME `task_id` and
    `state="duplicate"` without writing a new spec.yaml.

    `from_surface` is stored as `requester_route.to` for provenance (e.g.
    `pc-kit`, `telegram`, `cron`). `requester_route.channel` is always `cli`
    when invoked through this surface, since the validated channel literals
    are constrained.

    `progress`, if provided, is invoked with one-line human-readable strings
    at each major step — used by the CLI to narrate to stderr.

    Returns:
      IntakeResult on success (including duplicate).
      None if the intake LangGraph couldn't infer a usable spec.
    """
    life_root = life_root or Path("~/.life").expanduser()
    life_root.mkdir(parents=True, exist_ok=True)

    def _say(msg: str) -> None:
        if progress is not None:
            progress(msg)

    prose = prose.strip()
    if not prose:
        _say("intake: empty prose — aborting")
        return None

    fingerprint = _intake_hash(prose, from_surface)
    _say(f"intake: fingerprint={fingerprint[:12]} from={from_surface}")

    index = _load_intake_index(life_root)
    if fingerprint in index:
        existing_path = Path(index[fingerprint])
        if existing_path.is_file():
            _say(f"intake: duplicate — reusing {existing_path}")
            try:
                existing = load_spec(existing_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("task_intake: stale index entry %s: %s", existing_path, exc)
            else:
                return IntakeResult(
                    task_id=existing.task_id,
                    spec_path=existing_path,
                    budget_min=max(1, existing.budget.max_runtime_seconds // 60),
                    target_repo=existing.target_repo,
                    state="duplicate",
                )
        # Index pointed at a vanished spec — drop the stale entry and continue.
        logger.info("task_intake: dropping stale index entry %s", fingerprint)
        index.pop(fingerprint, None)
        _save_intake_index(life_root, index)

    _say("intake: invoking LangGraph intake node (claude --print)")
    route = RequesterRoute(channel="cli", to=from_surface or "cli")
    spec = intake(
        prose,
        requester_route=route,
        life_root=life_root,
        task_id=task_id,
        created_by=f"intake:{from_surface or 'cli'}",
    )
    if spec is None:
        _say("intake: LangGraph returned no usable spec")
        return None

    # Discover where intake() wrote the spec.
    spec_path = _locate_spec_path(life_root, spec.task_id)
    if spec_path is None:
        logger.error("task_intake: spec persisted for %s but path not found", spec.task_id)
        _say("intake: spec written but its on-disk path could not be located")
        return None

    index[fingerprint] = str(spec_path)
    _save_intake_index(life_root, index)

    _say(
        f"intake: new spec at {spec_path} "
        f"(kind={spec.kind.value}, target_repo={spec.target_repo})"
    )
    # Lifecycle event: task_intake → spec_created. Fires only on the
    # state="new" path so a duplicate intake doesn't re-announce.
    try:
        from orchestrator.events import emit_queued

        emit_queued(
            task_id=spec.task_id,
            target_repo=spec.target_repo,
            chat_id=events_chat_id,
            announce=events_announce,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("events emit_queued failed for %s: %s", spec.task_id, exc)

    return IntakeResult(
        task_id=spec.task_id,
        spec_path=spec_path,
        budget_min=max(1, spec.budget.max_runtime_seconds // 60),
        target_repo=spec.target_repo,
        state="new",
    )


def _locate_spec_path(life_root: Path, task_id: str) -> Path | None:
    flat = life_root / "tasks" / task_id / "spec.yaml"
    if flat.is_file():
        return flat
    for candidate in life_root.glob(f"projects/*/tasks/{task_id}/spec.yaml"):
        if candidate.is_file():
            return candidate
    return None
