"""Minimal .env loader — no third-party dependency.

Reads ``KEY=VALUE`` lines from a ``.env`` file and sets them in ``os.environ``
**only if not already set**. So real env vars (shell, systemd, compose) always
win — ``.env`` is the per-machine default surface, not an override.

Searches, in order:
  1. ``$DEVCLAW_DOTENV`` — explicit path, set in the shell
  2. ``./.env`` — the cwd of the running process

A missing file is a quiet no-op; a malformed line is logged to stderr and
skipped. Quoted values are unquoted; ``#`` introduces a comment when not
inside a quote. No shell expansion, no continuations — keep your .env simple.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def load_dotenv() -> Path | None:
    """Locate + load a .env into ``os.environ`` (without overriding existing
    keys). Returns the path that was loaded, or ``None`` if no file was found."""
    candidates: list[Path] = []
    explicit = os.environ.get("DEVCLAW_DOTENV")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / ".env")

    for path in candidates:
        if path.is_file():
            _load_into_environ(path)
            return path
    return None


def _load_into_environ(path: Path) -> None:
    try:
        text = path.read_text()
    except OSError as err:
        sys.stderr.write(f"devclaw: could not read {path}: {err}\n")
        return
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            sys.stderr.write(f"devclaw: {path}:{lineno}: ignored (no '='): {raw!r}\n")
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _unquote(value.strip())
        if not key or any(c.isspace() for c in key):
            sys.stderr.write(f"devclaw: {path}:{lineno}: ignored (bad key): {raw!r}\n")
            continue
        if key in os.environ:
            continue  # real env wins
        os.environ[key] = value


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    # strip trailing inline-comment (only when not quoted)
    if " #" in s:
        s = s.split(" #", 1)[0].rstrip()
    return s
