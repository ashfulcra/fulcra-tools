# packages/coord-engine/tests/test_continuity_audit.py
from datetime import datetime, timedelta, timezone
from coord_engine.continuity_audit import stale_agents

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)

def _p(agent, hours_ago):
    return {"agent": agent, "ts": NOW - timedelta(hours=hours_ago)}

def test_fresh_presence_no_snapshot_is_stale():
    out = stale_agents(presence=[_p("a", 1)], snapshots=[], now=NOW)
    assert [r["agent"] for r in out] == ["a"]
    assert out[0]["snapshot_age_h"] is None

def test_fresh_presence_old_snapshot_is_stale():
    out = stale_agents(presence=[_p("a", 1)], snapshots=[_p("a", 30)], now=NOW)
    assert [r["agent"] for r in out] == ["a"]
    assert out[0]["snapshot_age_h"] == 30.0

def test_fresh_presence_fresh_snapshot_is_clean():
    assert stale_agents(presence=[_p("a", 1)], snapshots=[_p("a", 2)], now=NOW) == []

def test_stale_presence_is_ignored_not_flagged():
    # a dead agent is a presence problem, not a continuity problem
    assert stale_agents(presence=[_p("a", 48)], snapshots=[], now=NOW) == []

def test_latest_snapshot_wins():
    out = stale_agents(presence=[_p("a", 1)], snapshots=[_p("a", 40), _p("a", 3)], now=NOW)
    assert out == []

def test_thresholds_are_parameters():
    out = stale_agents(presence=[_p("a", 1)], snapshots=[_p("a", 5)], now=NOW,
                       snapshot_stale_hours=4)
    assert [r["agent"] for r in out] == ["a"]
