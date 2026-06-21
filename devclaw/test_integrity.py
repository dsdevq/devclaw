"""Back-compat shim. The test-integrity guard moved to
:mod:`devclaw.loom.test_integrity` during the ``loom`` extraction. Import from
:mod:`devclaw.loom` going forward; this module re-exports the same names."""

from __future__ import annotations

from .loom.test_integrity import IntegrityReport, scan_diff  # noqa: F401
