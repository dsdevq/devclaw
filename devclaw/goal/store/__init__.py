"""The durable goal store — split into a package for legibility.

Behavior-preserving decomposition of the old ``goal/store.py`` god-file:

- :mod:`.base` — module regexes + ``parse_duration`` / ``_default_now``, and
  :class:`GoalStore` itself (construction, discovery, transaction/mirror
  discipline, goal facts, clock helpers).
- :mod:`.status` — :class:`GoalStatusMixin`, the single-writer / CAS choke
  point (``transition`` / ``force_block`` / the STATUS.md view).
- :mod:`.content` — :class:`GoalContentMixin` + :class:`GoalDocCorrupt` (log,
  settlements, deliveries, checklist, firmed-draft, inbox/steering).

Every public name the pre-split ``store.py`` exported is re-exported here, so no
importer changes. ``LEGAL`` is re-exported too (and ``transition`` reads it off
this package namespace) so a test's ``monkeypatch.setattr(goal.store, "LEGAL",
...)`` regression keeps working exactly as before.
"""

from __future__ import annotations

from ..transitions import LEGAL
from .base import GoalStore, _default_now, parse_duration
from .content import GoalDocCorrupt

__all__ = [
    "GoalStore",
    "GoalDocCorrupt",
    "parse_duration",
    "LEGAL",
    "_default_now",
]
