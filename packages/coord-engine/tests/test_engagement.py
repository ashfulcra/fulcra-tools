"""W1 — inert engagement schema (wake-router build).

The presence shard gains an ``engagement: {mode, until, state, lapsed_at}``
object. W1 is a pure no-op to every existing output: a beat may now CARRY the
field, folds PARSE it, but NOTHING acts on it. ``state``/``lapsed_at`` are
parse-only here (the W3 sweep is their sole writer). These tests pin the write
decision, the defensive parser, and — the heart of W1 — the inert-fold guarantee.
"""

from datetime import datetime, timedelta, timezone

import pytest

from coord_engine import cli, okf, presence, tasks
from coord_engine_test_helpers import FakeTransport

PINNED_NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _shard_path(agent: str) -> str:
    return f"team/r/presence/{tasks.agent_key(agent)}.md"


def _read_shard_fm(t: FakeTransport, agent: str) -> dict:
    return okf.parse_frontmatter(t.store[_shard_path(agent)])


def _no_presence_written(t: FakeTransport) -> bool:
    return not any(p.startswith("team/r/presence/") for p in t.store)


# --- okf: nested-map round-trip (the shard serialization) -------------------

def test_okf_nested_map_round_trips():
    fm = {
        "type": "Presence",
        "engagement": {"mode": "session", "until": "2026-07-23T09:00:00Z",
                       "state": "active", "lapsed_at": None},
    }
    text = okf.render_frontmatter(fm) + "\nbody\n"
    parsed = okf.parse_frontmatter(text)
    assert parsed["engagement"] == {
        "mode": "session", "until": "2026-07-23T09:00:00Z",
        "state": "active", "lapsed_at": None,
    }


def test_okf_block_list_still_parses_after_nested_map_support():
    # regression guard: nested-map support must not break block lists.
    fm = okf.parse_frontmatter(
        "---\ntype: Task\ntags:\n  - workstream:x\n  - kind:bug\n---\n"
    )
    assert fm["tags"] == ["workstream:x", "kind:bug"]


# --- CLI write decision -----------------------------------------------------

def test_beat_engagement_session_with_until():
    t = FakeTransport()
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session",
                     "--until", "2026-07-23T09:00:00Z"], transport=t) == 0
    eng = presence.parse_engagement(_read_shard_fm(t, "amy"))
    assert eng["mode"] == "session"
    assert eng["until"] == "2026-07-23T09:00:00Z"
    assert eng["state"] == "active"
    assert eng["lapsed_at"] is None


def test_beat_engagement_session_default_until_is_join_plus_8h():
    t = FakeTransport()
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session"], transport=t) == 0
    eng = presence.parse_engagement(_read_shard_fm(t, "amy"))
    expected = (PINNED_NOW + timedelta(hours=8)).isoformat().replace("+00:00", "Z")
    assert eng["mode"] == "session"
    assert eng["until"] == expected


def test_beat_engagement_resident_has_null_until():
    t = FakeTransport()
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "resident"], transport=t) == 0
    eng = presence.parse_engagement(_read_shard_fm(t, "amy"))
    assert eng["mode"] == "resident"
    assert eng["until"] is None


def test_beat_without_engagement_writes_byte_identical_legacy_shard():
    t = FakeTransport()
    assert cli.main(["presence", "beat", "r", "-a", "amy", "-w", "web",
                     "-s", "shipping"], transport=t) == 0
    content = t.store[_shard_path("amy")]
    # No engagement field at all — inert step means today's exact bytes.
    assert "engagement" not in content
    legacy_fm = {
        "type": "Presence", "title": "presence — amy", "agent": "amy",
        "workstreams": ["web"], "summary": "shipping",
        "timestamp": PINNED_NOW.isoformat().replace("+00:00", "Z"),
    }
    expected = okf.render_frontmatter(legacy_fm) + "\n# Presence: amy\n"
    assert content == expected


def test_until_without_engagement_is_validation_error(capsys):
    t = FakeTransport()
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--until", "2026-07-23T09:00:00Z"], transport=t) == 2
    assert "--until" in capsys.readouterr().err
    assert _no_presence_written(t)


def test_until_with_non_session_mode_is_validation_error(capsys):
    t = FakeTransport()
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "resident",
                     "--until", "2026-07-23T09:00:00Z"], transport=t) == 2
    assert "session" in capsys.readouterr().err
    assert _no_presence_written(t)


def test_beat_bad_until_format_writes_nothing(capsys):
    t = FakeTransport()
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session",
                     "--until", "not-a-ts"], transport=t) == 2
    assert "ISO-8601" in capsys.readouterr().err
    assert _no_presence_written(t)


# --- defensive parser (never raises) ----------------------------------------

def test_parse_engagement_absent_field_is_legacy_default():
    eng = presence.parse_engagement({"agent": "amy"})
    assert eng == {"mode": "resident", "until": None,
                   "state": "active", "lapsed_at": None}
    assert "_engagement_degraded" not in eng


def test_parse_engagement_unknown_mode_degrades_no_raise():
    eng = presence.parse_engagement({"engagement": {"mode": "bogus"}})
    assert eng["mode"] == "resident"
    assert eng["state"] == "active"
    assert "_engagement_degraded" in eng


def test_parse_engagement_non_dict_degrades_no_raise():
    eng = presence.parse_engagement({"engagement": "session"})
    assert eng["mode"] == "resident"
    assert eng["state"] == "active"
    assert "_engagement_degraded" in eng


def test_parse_engagement_bad_until_degrades_no_raise():
    eng = presence.parse_engagement(
        {"engagement": {"mode": "session", "until": "whenever"}})
    assert eng["mode"] == "resident"
    assert eng["state"] == "active"
    assert "_engagement_degraded" in eng


# --- the inert-fold guarantee (heart of W1) ---------------------------------

def test_folds_do_not_act_on_engagement():
    """A shard with engagement={session, until in the past, state:active} yields
    the IDENTICAL liveness classification as the same shard with no engagement
    field. None of the folds act on engagement in W1."""
    ts = (PINNED_NOW - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    baseline = {"agent": "amy", "workstreams": [], "summary": "", "timestamp": ts}
    with_eng = dict(baseline)
    with_eng["engagement"] = {
        "mode": "session",
        "until": (PINNED_NOW - timedelta(hours=100)).isoformat().replace("+00:00", "Z"),
        "state": "active", "lapsed_at": None,
    }
    now = PINNED_NOW.isoformat().replace("+00:00", "Z")

    r_base = presence.roster([baseline], now=now)
    r_eng = presence.roster([with_eng], now=now)
    assert r_base[0]["liveness"] == r_eng[0]["liveness"]
    assert presence.broadcast_roster([baseline], now=now) == \
        presence.broadcast_roster([with_eng], now=now)
