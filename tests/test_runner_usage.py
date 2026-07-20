"""Runner-side per-task usage capture (mission-control borrow item 2).

The runner flattens the SDK's combined conversation metrics into a plain
``usage`` dict that rides the result payload to the host (→ result_json →
per-goal rollups). Usage is telemetry, never a gate: schema drift, absent
stats, or an all-zero report must degrade to None (block omitted from the
payload), never fail a finished run.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_RUNNER_PATH = Path(__file__).resolve().parents[1] / "openhands-runner" / "runner.py"


@pytest.fixture(scope="module")
def runner():
    spec = importlib.util.spec_from_file_location("oh_runner_usage", _RUNNER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # top-level only; openhands imports live in main()
    return mod


class _FakeTokens:
    prompt_tokens = 1200
    completion_tokens = 340
    cache_read_tokens = 9000


class _FakeMetrics:
    accumulated_cost = 0.0421
    accumulated_token_usage = _FakeTokens()


class _FakeStats:
    def __init__(self, metrics):
        self._metrics = metrics

    def get_combined_metrics(self):
        return self._metrics


class _FakeConversation:
    def __init__(self, metrics):
        self.conversation_stats = _FakeStats(metrics)


def test_collect_usage_flattens_conversation_metrics(runner):
    usage = runner._collect_usage(_FakeConversation(_FakeMetrics()))
    assert usage == {
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_read_tokens": 9000,
        "cost_usd": 0.0421,
    }


def test_collect_usage_all_zero_reads_as_unknown_not_free(runner):
    class ZeroTokens:
        prompt_tokens = 0
        completion_tokens = 0
        cache_read_tokens = 0

    class ZeroMetrics:
        accumulated_cost = 0.0
        accumulated_token_usage = ZeroTokens()

    assert runner._collect_usage(_FakeConversation(ZeroMetrics())) is None


def test_collect_usage_never_raises_on_schema_drift(runner):
    class Exploding:
        @property
        def conversation_stats(self):
            raise RuntimeError("sdk drifted")

    assert runner._collect_usage(Exploding()) is None

    class NoTokens:
        accumulated_cost = 0.5
        accumulated_token_usage = None  # tokens absent, cost still present

    usage = runner._collect_usage(_FakeConversation(NoTokens()))
    assert usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cost_usd": 0.5,
    }
