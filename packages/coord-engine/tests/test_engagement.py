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


# --- refresh-preservation: a beat must not slide TTL or clobber sweep state --

def _put_shard(t: FakeTransport, agent: str, engagement: dict) -> None:
    fm = {
        "type": "Presence", "title": f"presence — {agent}", "agent": agent,
        "workstreams": [], "summary": "",
        "timestamp": PINNED_NOW.isoformat().replace("+00:00", "Z"),
        "engagement": engagement,
    }
    t.store[_shard_path(agent)] = okf.render_frontmatter(fm) + f"\n# Presence: {agent}\n"


def test_repeated_session_beat_preserves_until(monkeypatch):
    t = FakeTransport()
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session"], transport=t) == 0
    first = presence.parse_engagement(_read_shard_fm(t, "amy"))["until"]
    # Advance the clock two hours: a second session beat with no --until must NOT
    # recompute (slide) the expiry off the new 'now' — that is the dead-session-
    # looks-alive bug this schema exists to prevent.
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW + timedelta(hours=2))
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session"], transport=t) == 0
    second = presence.parse_engagement(_read_shard_fm(t, "amy"))["until"]
    assert second == first


def test_session_beat_explicit_until_replaces_preserved():
    t = FakeTransport()
    cli.main(["presence", "beat", "r", "-a", "amy", "--engagement", "session"], transport=t)
    assert cli.main(["presence", "beat", "r", "-a", "amy", "--engagement", "session",
                     "--until", "2026-08-01T00:00:00Z"], transport=t) == 0
    assert presence.parse_engagement(_read_shard_fm(t, "amy"))["until"] == "2026-08-01T00:00:00Z"


def test_session_beat_preserves_sweep_owned_state():
    t = FakeTransport()
    _put_shard(t, "amy", {"mode": "session", "until": "2026-07-01T00:00:00Z",
                          "state": "lapsed", "lapsed_at": "2026-07-01T00:00:00Z"})
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session"], transport=t) == 0
    eng = presence.parse_engagement(_read_shard_fm(t, "amy"))
    assert eng["state"] == "lapsed"
    assert eng["lapsed_at"] == "2026-07-01T00:00:00Z"
    assert eng["until"] == "2026-07-01T00:00:00Z"   # preserved, not slid


def test_mode_change_to_session_is_a_new_session():
    t = FakeTransport()
    _put_shard(t, "amy", {"mode": "resident", "until": None,
                          "state": "active", "lapsed_at": None})
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session"], transport=t) == 0
    eng = presence.parse_engagement(_read_shard_fm(t, "amy"))
    expected = (PINNED_NOW + timedelta(hours=8)).isoformat().replace("+00:00", "Z")
    assert eng["until"] == expected           # new session -> join + 8h
    assert eng["state"] == "active" and eng["lapsed_at"] is None


def test_session_beat_fails_closed_when_existing_shard_unreadable(capsys):
    """(r3) A read failure over an EXISTING shard is an UNKNOWN prior: the
    engagement-carrying beat must refuse (rc 1) and write NOTHING — a transient
    read failure must never let a fresh active session replace a sweep-marked
    lapsed one (false liveness through the error path)."""
    class _FailingRead(FakeTransport):
        def read(self, path):
            raise RuntimeError("transport down")

    t = _FailingRead()
    _put_shard(t, "amy", {"mode": "session", "until": "2026-07-01T00:00:00Z",
                          "state": "lapsed", "lapsed_at": "2026-07-01T00:00:00Z"})
    before = t.store[_shard_path("amy")]
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session"], transport=t) == 1
    assert "retry" in capsys.readouterr().err
    assert t.store[_shard_path("amy")] == before      # untouched


def test_session_beat_fails_closed_when_listing_fails(capsys):
    """(r3) If the existence check itself fails, the prior is UNKNOWN — refuse."""
    class _FailingBoth(FakeTransport):
        def read(self, path):
            raise RuntimeError("transport down")

        def list_dir(self, prefix):
            raise RuntimeError("transport down")

    t = _FailingBoth()
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session"], transport=t) == 1
    assert "retry" in capsys.readouterr().err
    assert _no_presence_written(t)


def test_session_beat_genuinely_absent_shard_is_fresh_session():
    """(r3) Read failing but the LISTING proving absence is a legitimately fresh
    session — bootstrap must not fail closed."""
    class _FailingRead(FakeTransport):
        def read(self, path):
            raise RuntimeError("transport down")

    t = _FailingRead()                                 # store empty: listing shows absent
    assert cli.main(["presence", "beat", "r", "-a", "amy",
                     "--engagement", "session"], transport=t) == 0
    eng = presence.parse_engagement(_read_shard_fm(t, "amy"))
    expected = (PINNED_NOW + timedelta(hours=8)).isoformat().replace("+00:00", "Z")
    assert eng["until"] == expected
    assert eng["state"] == "active" and eng["lapsed_at"] is None


# --- parser: a session with no expiry is malformed --------------------------

def test_parse_engagement_session_missing_until_degrades():
    eng = presence.parse_engagement({"engagement": {"mode": "session"}})
    assert eng["mode"] == "resident"
    assert "_engagement_degraded" in eng


def test_parse_engagement_session_null_until_degrades():
    eng = presence.parse_engagement({"engagement": {"mode": "session", "until": None}})
    assert eng["mode"] == "resident"
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
