"""Phase-handler registry — the dispatcher's lookup.

Tick asks ``handler_for(lifecycle_name)`` when it sees a phase that's owned by
a handler (today: only ``firming``). Adding a new phase = registering its
handler here; no tick edits. A handler is a process-singleton (handlers carry
their own LLM caller binding, which is bound lazily on first use)."""

from __future__ import annotations

from . import PhaseHandler
from .firming import FirmingHandler

_REGISTRY: dict[str, PhaseHandler] = {
    "firming": FirmingHandler(),
}


def handler_for(name: str) -> PhaseHandler | None:
    """Return the registered handler for ``name`` (typically a lifecycle
    value), or ``None`` if no handler exists (tick falls back to its built-in
    branches for the legacy phases)."""
    return _REGISTRY.get(name)


def register(name: str, handler: PhaseHandler) -> None:
    """Override/install a handler. Used by tests that want to inject a stub
    handler without touching tick."""
    _REGISTRY[name] = handler


def reset() -> None:
    """Restore the default registry. Tests call this in teardown after a
    ``register`` override."""
    _REGISTRY.clear()
    _REGISTRY["firming"] = FirmingHandler()
