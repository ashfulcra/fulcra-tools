from coord_engine import presence

NOW = "2026-07-02T12:00:00Z"

# clock-pin support (see #378):
import pytest
from datetime import datetime, timezone
from coord_engine import cli
PINNED_NOW = datetime(2026, 7, 2, 12, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Pin cli._now to PINNED_NOW (just after the module NOW).

    Fixtures stamp data relative to NOW, but folds/verbs compute windows and
    staleness off cli._now() against the REAL clock — so once wall-clock time
    crossed NOW + a window this suite flipped RED for good (the repo's
    date-boundary CI-flake class; template: #378 test_threads). Remedy: pin the
    clock, never weaken assertions. Tests that MOVE time monkeypatch cli._now
    themselves, overriding this."""
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _shard(agent, hours_ago, ws=None, summary=""):
    from datetime import datetime, timedelta, timezone
    n = datetime(2026, 7, 2, 12, 0, 0, tzinfo=timezone.utc)
    ts = (n - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")
    return {"agent": agent, "workstreams": ws or [], "summary": summary, "timestamp": ts}


def test_classify_bands():
    assert presence.classify(_shard("a", 0.5)["timestamp"], now=NOW) == "live"
    assert presence.classify(_shard("a", 5)["timestamp"], now=NOW) == "idle"
    assert presence.classify(_shard("a", 30)["timestamp"], now=NOW) == "stale"
    assert presence.classify(None, now=NOW) == "stale"          # undatable -> stale
    assert presence.classify("garbage", now=NOW) == "stale"


def test_roster_sorted_and_normalized():
    ros = presence.roster([
        _shard("zed", 0.2, ws="solo"),          # scalar ws normalized
        _shard("amy", 2, ws=["a", "b"], summary="doing x"),
        {"no_agent": True},                      # skipped
    ], now=NOW)
    assert [r["agent"] for r in ros] == ["amy", "zed"]
    assert ros[1]["workstreams"] == ["solo"]
    assert ros[0]["liveness"] == "idle" and ros[1]["liveness"] == "live"


def test_broadcast_roster_excludes_stale():
    ros = presence.broadcast_roster(
        [_shard("live1", 0.1), _shard("idle1", 3), _shard("gone", 100)], now=NOW)
    assert ros == ["idle1", "live1"]


def test_agents_digest_unions_presence_and_task_parties():
    rows = [
        {"status": "active", "owner": "amy", "assignee": None},
        {"status": "blocked", "owner": "amy", "assignee": "bob"},
        {"status": "done", "owner": "amy", "assignee": None},   # terminal excluded
        {"status": "proposed", "owner": None, "assignee": "*"},  # wildcard not an agent
    ]
    d = presence.agents_digest(rows, [_shard("amy", 0.1, summary="hi")], now=NOW)
    names = {a["agent"]: a for a in d}
    assert set(names) == {"amy", "bob"}
    assert names["amy"]["liveness"] == "live" and names["amy"]["open"] == {"active": 1, "blocked": 1}
    assert names["bob"]["liveness"] == "unknown" and names["bob"]["open"] == {"blocked": 1}


def test_agents_digest_omits_terminal_only_task_parties_without_presence():
    rows = [
        {"status": "done", "owner": "old-owner", "assignee": "old-assignee"},
        {"status": "abandoned", "owner": "gone", "assignee": None},
        {"status": "active", "owner": "active-owner", "assignee": None},
    ]
    d = presence.agents_digest(rows, [], now=NOW)
    names = {a["agent"]: a for a in d}
    assert set(names) == {"active-owner"}
    assert names["active-owner"]["open"] == {"active": 1}
