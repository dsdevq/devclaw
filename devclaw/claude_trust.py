"""Claude Code workspace-trust seeding — the config twin of the mise trust
seam (openhands-runner's ``_mise_run`` / ``MISE_TRUSTED_CONFIG_PATHS``).

Claude Code began hard-enforcing *workspace trust* around 2026-07: in a
directory whose ``projects[<abs-path>].hasTrustDialogAccepted`` is not set in
the user's ``.claude.json``, it **ignores that workspace's
``.claude/settings.json`` permissions**, and a non-interactive
``claude --print`` exits non-zero on the untrusted-workspace guard. Devclaw
runs ``claude`` in two spots the host ``.claude.json`` never listed (its
``projects`` map is keyed by *host* paths):

- **cognition** — ``claude --print`` for the planner/evaluator/summary, run
  from the devclaw-mcp container's cwd (``/app``); and
- **the worker** — the in-sandbox agent, at ``/workspace``.

Both dead-stopped once trust started being enforced. Seeding that one flag
restores the prior behavior. This is pure Claude *config*: no ``ANTHROPIC_*``
key (the OAuth-only pillar is untouched), no permission *widening* — the
curated allowlist is exactly what gets *restored* — and no devclaw state.
Everything here is best-effort: a config we can't read/write is left alone so
the claude call still fails loudly on its own if trust was truly the blocker;
we never mask a real failure.
"""

from __future__ import annotations

import json
import os
import tempfile

#: The trust key Claude Code checks per workspace path.
_TRUST_KEY = "hasTrustDialogAccepted"


def apply_trust(cfg: dict, workspace_path: str) -> bool:
    """Mark ``workspace_path`` trusted inside a parsed ``.claude.json`` dict,
    in place. Idempotent — returns True only if it actually changed something
    (so callers can skip a redundant write). Preserves every other field,
    including the account identity (``oauthAccount``/``userID``) the ACP loop
    needs."""
    projects = cfg.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        cfg["projects"] = projects
    entry = projects.get(workspace_path)
    if not isinstance(entry, dict):
        entry = {}
        projects[workspace_path] = entry
    changed = entry.get(_TRUST_KEY) is not True
    entry[_TRUST_KEY] = True
    # Also pre-clear the first-run onboarding prompt so a fresh workspace never
    # blocks a non-interactive run on it either.
    entry.setdefault("hasCompletedProjectOnboarding", True)
    return changed


def config_path_for(config_dir: str | None = None) -> str:
    """Resolve the ``.claude.json`` path the way Claude Code does:
    ``$CLAUDE_CONFIG_DIR/.claude.json`` when the config dir is set, else
    ``~/.claude.json``. Pass ``config_dir`` explicitly to bypass the env."""
    cfg_dir = config_dir if config_dir is not None else os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg_dir:
        return os.path.join(cfg_dir.rstrip("/"), ".claude.json")
    return os.path.expanduser("~/.claude.json")


def _load(path: str) -> dict | None:
    """Parse ``.claude.json`` at ``path``. Returns None (not ``{}``) when the
    file is missing or unparseable, so the caller can tell "no config" from
    "empty config" — the sandbox copy must fall back rather than ship an
    identity-less config."""
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return cfg if isinstance(cfg, dict) else None


def ensure_trusted_in_place(config_path: str, workspace_path: str) -> bool:
    """Idempotently seed trust for ``workspace_path`` in the ``.claude.json`` at
    ``config_path``, writing atomically (temp + ``os.replace``). For the host
    cognition config, which devclaw owns and may edit in place. Best-effort:
    returns False on any I/O problem or when already trusted; True when a write
    landed."""
    cfg = _load(config_path) or {}
    if not apply_trust(cfg, workspace_path):
        return False
    tmp = f"{config_path}.devclaw-trust-tmp"
    try:
        parent = os.path.dirname(config_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        os.replace(tmp, config_path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def write_trusted_copy(src_config_path: str, workspace_path: str) -> str | None:
    """Write a COPY of the ``.claude.json`` at ``src_config_path`` with
    ``workspace_path`` trusted, to a temp file, and return its path. For the
    sandbox, whose bound config is read-only so it can't be edited in place —
    the caller binds this copy read-only and deletes it after the container
    exits.

    Returns None when the source can't be read/parsed, so the caller falls back
    to binding the raw host file (today's behavior — never a *regression*, and
    never an identity-less config that would hang the ACP loop)."""
    cfg = _load(src_config_path)
    if cfg is None:
        return None
    apply_trust(cfg, workspace_path)
    try:
        fd, path = tempfile.mkstemp(prefix="devclaw-claude-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        # The sandbox agent user may differ in uid from the devclaw-mcp process
        # that writes this; the copy carries only identity + trust (already
        # deemed safe to expose to the sandbox), so world-readable is fine.
        os.chmod(path, 0o644)
    except OSError:
        return None
    return path
