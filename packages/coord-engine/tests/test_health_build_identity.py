"""`health` must say which fresh hosts run the pinned engine BUILD.

The version label cannot answer it: ``__version__`` changes only on a release,
so hosts running different commits report the same version, and a reader of
``health`` could not name the hosts that were off the fleet pin.

What is pinned here:

* the shard records ``engine_build`` explicitly (``None`` when unknowable);
* the fold classifies each host against the pin read from the adoption
  authority, and only a readable pin plus a matching build is ``current``;
* a shard with no ``engine_build`` (an older writer) is UNKNOWN, never current,
  and an unreadable pin makes every host UNKNOWN;
* the text names each host's build and verdict, and the exit code is unchanged;
* the reconcile command itself writes the build (actuator test).
"""

import json
from datetime import datetime, timezone

import pytest

from coord_engine import cli, health, pin_currency
from coord_engine.tasks import agent_key

from coord_engine_test_helpers import FakeTransport


#: Shards are stamped at NOW; `health` ages them against `cli._now()`, so the
#: clock is pinned just after NOW (tests/test_clock_pin_convention.py).
NOW = "2026-10-15T12:00:00Z"
NOW_PLUS_5M = "2026-10-15T12:05:00Z"
PINNED_NOW = datetime(2026, 10, 15, 12, 5, tzinfo=timezone.utc)

PIN = "a" * 40
OTHER = "b" * 40
ADOPT = f'#!/usr/bin/env bash\nPIN="{PIN}"   # synthetic pin\nVER=x\n'


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _shard(**over):
    base = {"schema": "coord.teams.health.v1", "host": "h", "at": NOW,
            "engine_version": "2.0.6", "advanced": True, "degraded_reason": None}
    base.update(over)
    return base


def _transport(*shards, adopt=ADOPT):
    t = FakeTransport()
    if adopt is not None:
        t.put("team/r/_coord/bus-v3/adopt-latest.sh", adopt)
    for s in shards:
        t.put(f"team/r/_coord/health/{agent_key(s['host'])}.json", json.dumps(s))
    return t


def test_build_shard_records_the_build_sha():
    got = health.build_shard(host="h", now=NOW, engine_version="2.0.6",
                             result={"tasks": 1, "warnings": []}, engine_build=PIN)
    assert got["engine_build"] == PIN


def test_build_shard_writes_the_key_even_when_the_build_is_unknowable():
    got = health.build_shard(host="h", now=NOW, engine_version="2.0.6",
                             result={"tasks": 1, "warnings": []})
    assert "engine_build" in got and got["engine_build"] is None


def test_fold_classifies_each_fresh_host_against_the_pin():
    view = health.fold(
        [_shard(host="on", engine_build=PIN),
         _shard(host="off", engine_build=OTHER),
         _shard(host="old")],  # older writer: no engine_build key
        now=NOW_PLUS_5M, pin=PIN, pin_status="ok")
    by = {h["host"]: h["pin_verdict"] for h in view["hosts"]}
    assert by == {"on": "current", "off": "mismatch", "old": "unknown-build"}
    assert (view["at_pin"], view["off_pin"], view["unknown_build"]) == (1, 1, 1)
    assert view["fleet_pin"] == PIN


def test_a_missing_or_malformed_build_is_unknown_never_current():
    view = health.fold(
        [_shard(host="none"), _shard(host="short", engine_build="aaaa"),
         _shard(host="junk", engine_build="not-a-sha-at-all"),
         _shard(host="num", engine_build=12345)],
        now=NOW_PLUS_5M, pin=PIN, pin_status="ok")
    assert {h["pin_verdict"] for h in view["hosts"]} == {"unknown-build"}
    assert view["at_pin"] == 0


def test_an_unreadable_pin_makes_every_host_unknown():
    view = health.fold([_shard(host="on", engine_build=PIN)],
                       now=NOW_PLUS_5M, pin=None, pin_status="error")
    assert view["hosts"][0]["pin_verdict"] == "unknown-pin"
    assert view["fleet_pin"] is None and view["at_pin"] == 0


def test_stale_hosts_are_not_counted_in_the_pin_rollups():
    view = health.fold([_shard(host="dead", at="2020-01-01T00:00:00Z", engine_build=OTHER)],
                       now=NOW_PLUS_5M, pin=PIN, pin_status="ok")
    assert (view["at_pin"], view["off_pin"], view["unknown_build"]) == (0, 0, 0)


def test_health_text_names_each_build_and_the_rollup(capsys):
    rc = cli.main(["health", "r"], transport=_transport(
        _shard(host="on-host", engine_build=PIN),
        _shard(host="off-host", engine_build=OTHER),
        _shard(host="old-host")))
    out = capsys.readouterr().out
    assert rc == 0, "build currency is reported, it does not change the exit code"
    assert f"engine pin {PIN[:12]}: 1 fresh host(s) at the pin, 1 on a different build, 1 build UNKNOWN" in out
    lines = {l.split(" — ")[0].split("] ")[-1]: l for l in out.splitlines() if "] " in l}
    assert f"build {PIN[:12]} = pin" in lines["on-host"]
    assert f"build {OTHER[:12]} NOT the pin" in lines["off-host"]
    assert "build UNKNOWN" in lines["old-host"]


def test_health_text_says_when_the_pin_cannot_be_read(capsys):
    cli.main(["health", "r"], transport=_transport(_shard(host="on-host", engine_build=PIN),
                                                   adopt=None))
    out = capsys.readouterr().out
    assert "engine pin UNKNOWN (adoption authority absent)" in out
    assert "= pin" not in out, "no host may read as current without a readable pin"


def test_health_json_carries_the_build_facts(capsys):
    cli.main(["health", "r", "--json"], transport=_transport(_shard(host="on-host", engine_build=PIN)))
    view = json.loads(capsys.readouterr().out)
    assert view["fleet_pin"] == PIN and view["at_pin"] == 1
    assert view["hosts"][0]["engine_build"] == PIN
    assert view["hosts"][0]["pin_verdict"] == "current"


def test_reconcile_writes_the_running_build_into_its_shard(monkeypatch):
    """Actuator test: the real reconcile command, not only build_shard."""
    monkeypatch.setattr(pin_currency, "build_sha", lambda *a, **k: PIN)
    transport = FakeTransport()
    transport.put("team/r/task/a.md", "---\ntype: Task\ntitle: A\nstatus: active\n---\n")
    cli.main(["reconcile", "r"], transport=transport)
    shards = [p for p in transport.store if p.startswith("team/r/_coord/health/")]
    assert shards
    assert json.loads(transport.store[shards[0]])["engine_build"] == PIN
