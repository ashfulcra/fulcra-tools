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


# --- cmd_health wiring (text path) -------------------------------------------

import json

from coord_engine import cli, continuity, okf
from coord_engine_test_helpers import FakeTransport


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _beat(t, team, agent, hours_ago):
    fm = {"type": "Presence", "agent": agent,
          "timestamp": _iso(datetime.now(timezone.utc) - timedelta(hours=hours_ago))}
    t.put(f"team/{team}/presence/{agent}.md", okf.render_frontmatter(fm) + f"\n# Presence: {agent}\n")


def _snap(t, team, agent, task, hours_ago):
    snap = continuity.build_snapshot(
        agent=agent, task=task, objective="o",
        now=_iso(datetime.now(timezone.utc) - timedelta(hours=hours_ago)))
    t.put(cli._continuity_path(team, agent, task), json.dumps(snap))


def test_health_flags_fresh_presence_missing_snapshot(capsys):
    t = FakeTransport()
    _beat(t, "r", "bob", 1)
    assert cli.main(["health", "r"], transport=t) in (0, 1)
    out = capsys.readouterr().out
    assert any("continuity-stale" in ln and "bob" in ln for ln in out.splitlines())


def test_health_clean_when_snapshot_fresh(capsys):
    t = FakeTransport()
    _beat(t, "r", "alice", 1)
    _snap(t, "r", "alice", "t1", 2)
    assert cli.main(["health", "r"], transport=t) in (0, 1)
    assert "continuity-stale" not in capsys.readouterr().out


def test_health_survives_malformed_snapshot_and_still_flags_from_valid_data(capsys):
    t = FakeTransport()
    # carol: fresh presence, only a corrupt snapshot shard -> flagged, no crash
    _beat(t, "r", "carol", 1)
    t.put(cli._continuity_path("r", "carol", "bad"), "{not json")
    # dan: fresh presence, one bad-timestamp shard + one genuinely stale one ->
    # the corrupt shard must not shadow the valid stale evidence
    _beat(t, "r", "dan", 1)
    t.put(cli._continuity_path("r", "dan", "badts"),
          json.dumps({"agent": "dan", "task": "badts", "created_at": "not-a-time"}))
    _snap(t, "r", "dan", "old", 30)
    # erin: fresh presence, corrupt shard + fresh valid one -> clean
    _beat(t, "r", "erin", 1)
    t.put(cli._continuity_path("r", "erin", "bad"), "{not json")
    _snap(t, "r", "erin", "t1", 2)
    assert cli.main(["health", "r"], transport=t) in (0, 1)
    out = capsys.readouterr().out
    flagged = [ln for ln in out.splitlines() if "continuity-stale" in ln]
    assert any("carol" in ln and "missing" in ln for ln in flagged)
    assert any("dan" in ln and "stale (30.0h)" in ln for ln in flagged)
    assert not any("erin" in ln for ln in flagged)
