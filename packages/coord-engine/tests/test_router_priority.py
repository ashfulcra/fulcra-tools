"""P0 must route. Regression pins for the 2026-07-27 silent-demotion defect.

`PRIORITY_RANK` was restated in router.py as {"P1":1,"P2":2,"P3":3}, omitting
P0 — the fleet's most urgent class and the one the task model calls most
urgent. `decide()` then coerced any unknown priority to P2, one notch below a
default P1 interrupt floor, so every P0 in the system decided "batch" and woke
nobody. Eighteen live items were affected and the live router delivered
nothing for ten hours.

The fix derives the table from `model.VALID_PRIORITIES` and routes unknown
priorities to the fail-visible lane. These tests pin both halves.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from coord_engine import model, router


NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
PINNED_NOW = NOW


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Pin the engine clock to PINNED_NOW (repo convention; see test_threads).

    decide() takes `now` explicitly, so these cases do not read the wall clock
    today — but the convention exists because a module-level NOW plus a real
    clock is how this repo has produced boundary flakes before. Pin it so a
    later test added here inherits the guarantee instead of rediscovering it.
    """
    monkeypatch.setattr(router, "_now", lambda: PINNED_NOW, raising=False)


def _cfg(floor="P1", debounce_min=15):
    return {"priority_floor": floor, "debounce_min": debounce_min,
            "adapter": "macos-notify", "adapter_args": {},
            "executor": "host-1", "lapsed_checkin_min": 240}


def _decide(priority, cfg=None, **kw):
    kw.setdefault("presence_ts", None)
    kw.setdefault("lapsed", False)
    kw.setdefault("last_wake_at", None)
    kw.setdefault("last_delivered_at", None)
    return router.decide(item_priority=priority,
                         agent_cfg=_cfg() if cfg is None else cfg,
                         config_error=None, now=NOW, **kw)


# --- the defect itself -------------------------------------------------------

def test_p0_interrupts_at_the_default_floor():
    """THE regression. This failed before the fix: P0 decided 'batch'."""
    decision, _, reason = _decide("P0")
    assert decision == "interrupt", reason


@pytest.mark.parametrize("floor", model.VALID_PRIORITIES)
def test_p0_interrupts_at_every_configured_floor(floor):
    """P0 is the most urgent class; no floor may filter it out."""
    decision, _, reason = _decide("P0", _cfg(floor=floor))
    assert decision == "interrupt", f"floor={floor}: {reason}"


def test_p0_is_the_most_urgent_rank():
    ranks = [router.PRIORITY_RANK[p] for p in model.VALID_PRIORITIES]
    assert router.PRIORITY_RANK["P0"] == min(ranks)


# --- the table cannot drift from the canonical vocabulary again --------------

def test_rank_table_covers_exactly_the_canonical_vocabulary():
    """The root cause was a restated table. Pin that it is derived."""
    assert set(router.PRIORITY_RANK) == set(model.VALID_PRIORITIES)


def test_p0_is_accepted_as_a_priority_floor():
    """Before the fix, config validation rejected a P0 floor outright, so
    'only ever wake me for P0' was inexpressible."""
    import json
    _valid, _execs, errors = router.validate_config(
        json.dumps({"executors": ["host-1"], "a": _cfg(floor="P0")}))
    assert "a" not in errors, errors


# --- unknown priorities fail visibly rather than becoming P2 -----------------

def test_unknown_priority_is_unroutable_not_silently_p2():
    decision, _, reason = _decide("P9")
    assert decision == "unroutable"
    assert "P9" in reason and "unknown priority" in reason


def test_unknown_priority_reason_names_the_expected_vocabulary():
    _, _, reason = _decide("urgent")
    for p in model.VALID_PRIORITIES:
        assert p in reason


# --- no regression in the classes that already worked ------------------------

def test_p1_still_interrupts_at_floor_p1():
    assert _decide("P1")[0] == "interrupt"


def test_p2_still_batches_at_floor_p1():
    assert _decide("P2")[0] == "batch"


def test_p3_still_batches_at_floor_p1():
    assert _decide("P3")[0] == "batch"


def test_p2_interrupts_when_the_floor_is_lowered_to_p2():
    assert _decide("P2", _cfg(floor="P2"))[0] == "interrupt"


def test_enablement_still_outranks_priority():
    """An unconfigured agent observes even for P0 — enablement is explicit."""
    decision, _, _ = router.decide(
        item_priority="P0", agent_cfg=None, config_error=None,
        presence_ts=None, lapsed=False, last_wake_at=None,
        last_delivered_at=None, now=NOW)
    assert decision == "observe"


def test_debounce_still_outranks_p0():
    """A wake inside the debounce window already covers this item; P0 does not
    punch through, because every wake is a check-your-bus nudge."""
    recent = NOW.replace(minute=55, hour=11)
    decision, _, _ = _decide("P0", last_delivered_at=recent)
    assert decision == "debounce"
