"""Tests for _PhaseTimer — the per-phase monotonic stopwatch used by
cmd_reconcile to record wall-time in the health record.
"""
import time
from fulcra_coord.cli import _PhaseTimer


def test_phase_timer_records_labelled_deltas(monkeypatch):
    t = [100.0]
    monkeypatch.setattr("fulcra_coord.cli.time.monotonic", lambda: t[0])
    pt = _PhaseTimer()
    t[0] = 100.5; pt.mark("load")
    t[0] = 100.9; pt.mark("build")
    s = pt.summary()
    assert s["load"] == 500.0      # 0.5s -> 500ms
    assert s["build"] == 400.0     # 0.4s -> 400ms
