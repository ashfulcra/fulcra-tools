"""Run the independent Slice 2 gates unchanged against production code.

Gates 8-10 remain in ``test_queue_contract_gates.py`` against the independent
reference model because they specify the later obligation-fold and takeover
slices.  The Slice 2 queue boundary is gates 1-7; each wrapper below calls the
original gate function without modifying its body.
"""

from __future__ import annotations

import json

import pytest

import test_queue_contract_gates as gates
from acceptance.contract import FakeStore, config_path
from acceptance.engine_adapter import EngineQueueAdapter, engine_cursor_path


@pytest.fixture
def store() -> FakeStore:
    value = FakeStore()
    value.seed(
        config_path(gates.TEAM),
        json.dumps({
            "data_type": "MomentAnnotation/test",
            "api_version": "v1alpha1",
        }),
    )
    return value


def _bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gates, "ReferenceQueue", EngineQueueAdapter)
    monkeypatch.setattr(gates, "cursor_path", engine_cursor_path)


def test_engine_gate_1(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    _bind(monkeypatch)
    queue = EngineQueueAdapter(
        store, gates.TEAM, gates.AGENT, records=gates.EVENTS)
    gates.test_gate_1_crash_after_print_before_processing_replays(queue)


def test_engine_gate_2(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    _bind(monkeypatch)
    queue = EngineQueueAdapter(
        store, gates.TEAM, gates.AGENT, records=gates.EVENTS)
    gates.test_gate_2_crash_before_commit_replay_is_idempotent(queue)


def test_engine_gate_3(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    _bind(monkeypatch)
    gates.test_gate_3_concurrent_wakes_lose_no_cursor_update(store)


def test_engine_gate_4(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    _bind(monkeypatch)
    queue = EngineQueueAdapter(
        store, gates.TEAM, gates.AGENT, records=gates.EVENTS)
    gates.test_gate_4_stale_token_cannot_advance_coverage(queue)


def test_engine_gate_5(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    _bind(monkeypatch)
    gates.test_gate_5_corrupt_config_is_invalid_never_absent(store)


def test_engine_gate_6(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    _bind(monkeypatch)
    queue = EngineQueueAdapter(
        store, gates.TEAM, gates.AGENT, records=gates.EVENTS)
    gates.test_gate_6_transport_failure_is_unknown_and_preserves_coverage(
        store, queue)


def test_engine_gate_7(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    _bind(monkeypatch)
    gates.test_gate_7_old_writer_against_new_cursor_is_rejected(store)


class _AgingEngineQueueAdapter(EngineQueueAdapter):
    """Adapter whose engine clock advances with the gate's ``now``.

    The elapsed-time gate is only meaningful if time actually moves for the code
    under test. The base adapter pins the clock to the newest fixture event, so
    binding gate 2b to it unchanged would test a thirty-day gap by running the
    engine twice at the same instant — green, and worthless. This maps the gate's
    ``now`` onto the engine's clock so the gap is real.
    """

    def read(self, now: float):
        self.clock_offset_s = max(0, int(now - gates.T0))
        return super().read(now)

    def commit(self, token: str, now: float):
        self.clock_offset_s = max(0, int(now - gates.T0))
        return super().commit(token, now)


def test_engine_gate_2b(monkeypatch: pytest.MonkeyPatch, store: FakeStore):
    """Gate 2b against production: elapsed time loses no work, commits once.

    Deliberately silent about token identity — the shipped engine never expires a
    pending batch and replays the original token, the reference model re-stages
    under a fresh one, and the spec mandates neither.
    """
    monkeypatch.setattr(gates, "ReferenceQueue", _AgingEngineQueueAdapter)
    monkeypatch.setattr(gates, "cursor_path", engine_cursor_path)
    queue = _AgingEngineQueueAdapter(
        store, gates.TEAM, gates.AGENT, records=gates.EVENTS)
    gates.test_gate_2b_elapsed_time_replay_still_commits_exactly_once(queue)
    # Non-vacuity: if the aging ever stops working, this gate silently becomes a
    # zero-elapsed-time gate that passes forever. Prove time actually moved.
    assert queue.clock_offset_s >= 30 * 24 * 3600, (
        f"engine clock advanced only {queue.clock_offset_s}s; the elapsed-time "
        "gate did not exercise elapsed time"
    )
