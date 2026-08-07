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
from coord_engine import cli, router
from coord_engine.transport import TransportError

NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
PINNED_NOW = NOW
FAILSAFE = router.RATE_CAP_FAILSAFE


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Pin cli._now, per the repo clock convention. Every test here passes
    `now` explicitly, so nothing reads the real clock today — the convention
    exists because that stops being true quietly, and this repo has been bitten
    by a real-clock boundary three times."""
    from coord_engine import cli

    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


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


# --- router-pass integration (codex 554 r2) ---------------------------------
#
# The r2 CHANGES verdict: validate_caps/within_caps had NO production caller.
# `_router_pass` called decide() without caps, so caps defaulted to None and
# every live decision stayed uncapped — the policy shipped as dead code. These
# drive the REAL pass (cmd_router_run), so they fail against a router that
# merely defines the helpers.

import argparse

from coord_engine import okf, tasks
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
RP = f"team/{TEAM}/_coord/router/"
TASKP = f"team/{TEAM}/task/"
PASS_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
NOW_ISO_PASS = "2026-07-23T12:00:00Z"
AGENT = "worker-a"
CFG = {"priority_floor": "P2", "debounce_min": 15,
       "adapter": "managed-agents-message",
       "adapter_args": {"session_ref": "s-1"}}


@pytest.fixture
def pass_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PASS_NOW)


def _pass_args(**kw):
    ns = argparse.Namespace(team=TEAM, once=True, json=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _p0_task(tid):
    return okf.render_frontmatter(
        {"type": "Task", "title": tid, "id": tid, "status": "proposed",
         "priority": "P0", "assignee": AGENT,
         "timestamp": "2026-07-23T11:00:00Z"}) + f"\n# {tid}\n"


def _delivery(agent, delivered_at, shard):
    return json.dumps({"agent": agent, "delivered_at": delivered_at,
                       "source_shard": shard})


def _setup(t, *, config_doc, deliveries=()):
    """A pass poised to interrupt: P0 item, enabled agent, fresh presence.
    Debounce is dodged by dating deliveries outside the 15-min window while
    keeping them inside the cap's 1-hour window."""
    t.put(TASKP + "urgent-1.md", _p0_task("urgent-1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "cursor.json",
          json.dumps({"watermark": "2026-07-23T11:00:00Z", "processed": {}}))
    t.put(RP + "config.json", config_doc)
    t.put(f"team/{TEAM}/presence/{tasks.agent_key(AGENT)}.md",
          okf.render_frontmatter(
              {"type": "Presence", "title": f"presence — {AGENT}",
               "agent": AGENT, "timestamp": "2026-07-23T11:55:00Z"})
          + "\n# beat\n")
    for i, (agent, at) in enumerate(deliveries):
        t.put(RP + f"delivered/d{i}.json", _delivery(agent, at, f"s{i}"))


def _queued(t):
    return {p: json.loads(c) for p, c in t.store.items()
            if p.startswith(RP + "queue/")}


def _enabled(caps=None):
    doc = {AGENT: dict(CFG)}
    if caps is not None:
        doc["caps"] = caps          # top-level block, per validate_caps
    return json.dumps(doc)


DEFER_TO = "2026-07-23T13:00:00Z"   # now + 1h, the cap's defer window


def _sole_entry(t):
    (entry,) = _queued(t).values()
    return entry


def test_router_pass_defers_an_over_cap_p0(pass_clock):
    """The load-bearing one. A P0 normally interrupts; over cap it DEFERS."""
    t = FakeTransport()
    _setup(t, config_doc=_enabled({"per_agent_per_hour": 2,
                                   "global_per_hour": 50}),
           # two deliveries inside the hour, outside the 15-min debounce
           deliveries=[(AGENT, "2026-07-23T11:10:00Z"),
                       (AGENT, "2026-07-23T11:20:00Z")])
    assert cli.cmd_router_run(_pass_args(), t) == 0
    # A cap DEFERS rather than drops: the entry survives, dated to the next
    # window. Asserting an empty queue would have tested item loss instead.
    entry = _sole_entry(t)
    assert entry["not_before"] == DEFER_TO
    assert entry["not_before"] > NOW_ISO_PASS


def test_router_pass_still_interrupts_a_p0_under_cap(pass_clock):
    """Control: identical pass, cap not reached — the P0 still gets through, so
    the test above pins the CAP and not some unrelated breakage."""
    t = FakeTransport()
    _setup(t, config_doc=_enabled({"per_agent_per_hour": 5,
                                   "global_per_hour": 50}),
           deliveries=[(AGENT, "2026-07-23T11:10:00Z")])
    assert cli.cmd_router_run(_pass_args(), t) == 0
    entry = _sole_entry(t)
    assert entry["agent"] == AGENT and entry["priority"] == "P0"
    assert entry["not_before"] <= NOW_ISO_PASS, "under cap, a P0 fires now"


@pytest.mark.parametrize("config_doc, label", [
    (json.dumps({AGENT: dict(CFG)}), "no caps key at all"),
    (json.dumps({AGENT: dict(CFG), "caps": "nonsense"}), "caps not an object"),
    (json.dumps({AGENT: dict(CFG), "caps": {}}), "caps object is empty"),
    (json.dumps({AGENT: dict(CFG),
                 "caps": {"per_agent_per_hour": 0,
                          "global_per_hour": -3}}), "caps below 1"),
    (json.dumps({AGENT: dict(CFG),
                 "caps": {"per_agent_per_hour": True,
                          "global_per_hour": True}}), "caps are booleans"),
    (json.dumps({AGENT: dict(CFG),
                 "caps": {"per_agent_per_hour": 99}}), "one key present, one missing"),
])
def test_missing_or_malformed_caps_apply_the_failsafe_not_unlimited(
        pass_clock, config_doc, label):
    """codex r2: a cap config that cannot be trusted must NOT read as
    unlimited. The failsafe is 1/hour, so ONE prior delivery inside the window
    is already at the cap and the P0 defers."""
    t = FakeTransport()
    _setup(t, config_doc=config_doc,
           deliveries=[(AGENT, "2026-07-23T11:10:00Z")])
    assert cli.cmd_router_run(_pass_args(), t) == 0
    entry = _sole_entry(t)
    assert entry["not_before"] == DEFER_TO, (
        f"{label}: must fail closed to the failsafe, not read as unlimited")


def test_unreadable_delivery_evidence_fails_the_cap_closed(pass_clock):
    """UNKNOWN is not zero. If the delivered/ listing degrades we cannot know
    what was already sent, and a cap that silently lifts at that moment is the
    defect this whole branch exists to refuse."""
    t = FakeTransport()
    _setup(t, config_doc=_enabled({"per_agent_per_hour": 50,
                                   "global_per_hour": 50}))

    real_list = t.list_dir

    def flaky(path):
        if path == RP + "delivered/":
            raise TransportError("listing degraded")
        return real_list(path)

    t.list_dir = flaky
    t.put(RP + "delivered.json", json.dumps({}))  # valid view: pass proceeds
    assert cli.cmd_router_run(_pass_args(), t) == 0
    entry = _sole_entry(t)
    assert entry["not_before"] == DEFER_TO, (
        "unmeasurable counts must defer, not run uncapped")


def test_counting_window_is_one_hour_and_ambiguous_stamps_count():
    """The fold itself: outside the window is excluded, an unparseable stamp
    cannot be proven outside it and so counts."""
    per_agent, total = router.count_wakes_last_hour([
        {"agent": "a", "delivered_at": "2026-07-23T11:30:00Z"},   # in
        {"agent": "a", "delivered_at": "2026-07-23T10:30:00Z"},   # out
        {"agent": "a", "delivered_at": "not-a-timestamp"},        # ambiguous
        {"agent": "b", "delivered_at": "2026-07-23T11:59:00Z"},   # in
        {"agent": "b"},                                           # ambiguous
        {"no_agent": True},                                       # skipped
    ], now=PASS_NOW)
    assert per_agent == {"a": 2, "b": 2}
    assert total == 4
