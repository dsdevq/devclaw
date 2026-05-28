"""Shared pytest fixtures for the orchestrator test suite.

After `2026-05-27-runtime-knowledge-split`, flat-bucket `tasks/`, `orchestrator.sqlite`,
and `intake_index.json` live under `LIFEKIT_STATE_DIR` instead of `~/.life/`. Tests
typically use `tmp_path` for the life_root; we point `LIFEKIT_STATE_DIR` at the same
tmp_path so `state_tasks_dir() == tmp_path/"tasks"`, matching the pre-split layout
that the tests assume. This keeps test data and assertions on one path while the
production code reads via the env-driven helper.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_lifekit_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LIFEKIT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    yield
