"""Rate caps: an absent brake must read as the TIGHTEST brake, never as none.

The cap this replaces was the one-agent allowlist — a de-facto brake nobody had
written down, which silently came off when the router config grew to five agents
plus executors. The failure mode that made it urgent is that "the cap is gone"
was indistinguishable from "the cap is fine", so every degenerate input here
resolves to the failsafe rather than to unlimited.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from coord_engine import router

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
FAILSAFE = router.RATE_CAP_FAILSAFE


@pytest.mark.parametrize("raw, why", [
    (None, "absent config"),
    ("{", "unparseable JSON"),
    ("[1,2]", "not an object"),
    ("{}", "no caps block"),
    ('{"caps": 5}', "caps not an object"),
    ('{"caps": {"per_agent_per_hour": "2", "global_per_hour": 4}}', "string value"),
    ('{"caps": {"per_agent_per_hour": 0, "global_per_hour": 4}}', "zero"),
    ('{"caps": {"per_agent_per_hour": -3, "global_per_hour": 4}}', "negative"),
    ('{"caps": {"per_agent_per_hour": true, "global_per_hour": 4}}', "bool"),
    ('{"caps": {"global_per_hour": 4}}', "missing key"),
])
def test_every_degenerate_input_fails_CLOSED(raw, why):
    caps, err = router.validate_caps(raw)
    assert err is not None, f"{why}: silently accepted"
    assert caps["per_agent_per_hour"] <= FAILSAFE["per_agent_per_hour"], why
    assert caps["per_agent_per_hour"] >= 1, "a cap of 0 would stop the router entirely"


def test_a_wellformed_cap_is_honoured():
    caps, err = router.validate_caps(
        json.dumps({"caps": {"per_agent_per_hour": 6, "global_per_hour": 20}}))
    assert err is None
    assert caps == {"per_agent_per_hour": 6, "global_per_hour": 20}


def test_a_partial_cap_holds_only_the_bad_key_at_the_failsafe():
    caps, err = router.validate_caps(
        json.dumps({"caps": {"per_agent_per_hour": 6, "global_per_hour": "lots"}}))
    assert caps["per_agent_per_hour"] == 6, "the valid key must still apply"
    assert caps["global_per_hour"] == FAILSAFE["global_per_hour"]
    assert "global_per_hour" in (err or "")


def test_either_limit_alone_can_refuse():
    caps = {"per_agent_per_hour": 2, "global_per_hour": 10}
    assert router.within_caps(caps=caps, agent_wakes_last_hour=2,
                              global_wakes_last_hour=0)[0] is False
    caps = {"per_agent_per_hour": 10, "global_per_hour": 3}
    assert router.within_caps(caps=caps, agent_wakes_last_hour=0,
                              global_wakes_last_hour=3)[0] is False
    assert router.within_caps(caps=caps, agent_wakes_last_hour=0,
                              global_wakes_last_hour=2)[0] is True


def _cfg(**kw):
    base = dict(priority_floor="P1", debounce_min=0, adapter="managed-agents-message",
                adapter_args={}, lapsed_checkin_min=60, executor="decision-plane",
                active_hours=None)
    base.update(kw)
    return base


def _decide(**kw):
    base = dict(item_priority="P0", agent_cfg=_cfg(), config_error=None,
                presence_ts=None, lapsed=False, last_wake_at=None,
                last_delivered_at=None, now=NOW)
    base.update(kw)
    return router.decide(**base)


def test_the_cap_DEFERS_rather_than_drops_and_binds_even_a_P0():
    """A brake binds every class, or it is not a brake. Deferring rather than
    dropping is what makes capping a P0 acceptable: the item rides to the next
    window instead of being lost."""
    decision, not_before, why = _decide(
        caps={"per_agent_per_hour": 1, "global_per_hour": 5},
        agent_wakes_last_hour=1)
    assert decision == "defer"
    assert not_before == NOW + timedelta(hours=1)
    assert "per-agent cap" in why


def test_under_the_cap_a_P0_still_interrupts():
    """Over-correction guard: the cap must not become a blanket suppressor."""
    decision, _, _ = _decide(
        caps={"per_agent_per_hour": 5, "global_per_hour": 5},
        agent_wakes_last_hour=0, global_wakes_last_hour=0)
    assert decision == "interrupt"


def test_a_caller_that_passes_no_caps_is_unchanged():
    """Wiring is incremental: an un-migrated caller keeps today's behaviour
    rather than silently acquiring the failsafe and throttling itself."""
    assert _decide()[0] == "interrupt"


def test_debounce_still_precedes_the_cap():
    """Ordering is a correctness property here — a debounced item must not be
    reported as capped, or the cap counts get blamed for coalescing."""
    decision, _, why = _decide(
        agent_cfg=_cfg(debounce_min=30), last_wake_at=NOW - timedelta(minutes=5),
        caps={"per_agent_per_hour": 1, "global_per_hour": 1},
        agent_wakes_last_hour=99, global_wakes_last_hour=99)
    assert decision == "debounce"
    assert "cap" not in why
