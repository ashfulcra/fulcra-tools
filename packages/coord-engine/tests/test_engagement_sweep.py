"""W3 — zero-token lapse sweep (wake-router build).

``coord-engine engagement sweep <team>`` is a host-tick, model-free pass that
marks a session past its ``until`` as LAPSED by writing EXACTLY two fields into
the presence shard — ``engagement.state: "lapsed"`` and ``engagement.lapsed_at``
(the sweep's evaluation time). This is the ONE sanctioned exception to
agent-owned presence writes, scoped to those two names.

Non-negotiables pinned here (all red-first — the sweep does not exist yet):
- MARK iff mode==session AND until present AND now>=until AND state==active AND
  engagement well-formed. Everything else SKIPS (resident/occasional/within-
  until/already-lapsed=idempotent-noop/degraded/unreadable/unparseable).
- The write changes ONLY state+lapsed_at; mode/until/timestamp/workstreams/
  summary/body are preserved (the sweep is NOT a beat — no timestamp bump, no
  until slide).
- Idempotent: a second sweep with no time change writes nothing (pinned with a
  write-count fake transport).
- READ-CONTRACT LENS: ``list_dir`` RAISES on failure -> the sweep is UNKNOWN-
  degraded (rc nonzero, loud, never a silent "0 lapsed"); per shard, read None /
  unparseable / _engagement_degraded -> SKIP (a failed read never causes a write).
- NEVER parks, NEVER releases roles — only the presence shard is ever written.
- ``--dry-run`` writes nothing.
"""

from datetime import datetime, timedelta, timezone

from coord_engine import cli, okf, presence, tasks
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat().replace("+00:00", "Z")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _pin(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: NOW)


def _shard_path(agent: str) -> str:
    return f"team/r/presence/{tasks.agent_key(agent)}.md"


def _put_presence(t, agent, *, age_min=5, engagement=None, workstreams=None,
                  summary="on the web workstream"):
    """Render a presence shard exactly the way ``presence beat`` does, so the
    round-trip the sweep performs is exercised against engine-shaped bytes."""
    fm = {
        "type": "Presence", "title": f"presence — {agent}", "agent": agent,
        "workstreams": workstreams if workstreams is not None else ["web"],
        "summary": summary,
        "timestamp": _iso(NOW - timedelta(minutes=age_min)),
    }
    if engagement is not None:
        fm["engagement"] = engagement
    t.store[_shard_path(agent)] = okf.render_frontmatter(fm) + f"\n# Presence: {agent}\n"


def _session(until_dt, *, state="active", lapsed_at=None):
    eng = {"mode": "session", "until": _iso(until_dt),
           "state": state, "lapsed_at": lapsed_at}
    return eng


class CountingTransport(FakeTransport):
    """Records every write path so idempotency and blast-radius can be pinned."""

    def __init__(self):
        super().__init__()
        self.writes: list[str] = []

    def write(self, path, content):
        self.writes.append(path)
        return super().write(path, content)


# --- the pure predicate ------------------------------------------------------

def test_sweep_decision_marks_expired_active_session():
    fm = {"engagement": _session(NOW - timedelta(hours=1))}
    d = presence.sweep_decision(fm, now=NOW_ISO)
    assert d["action"] == "mark"


def test_sweep_decision_boundary_now_equals_until_marks():
    # now >= until is boundary-INCLUSIVE (mirrors the W2 _is_lapsed truth table).
    fm = {"engagement": _session(NOW)}
    assert presence.sweep_decision(fm, now=NOW_ISO)["action"] == "mark"


def test_sweep_decision_within_until_skips():
    fm = {"engagement": _session(NOW + timedelta(hours=1))}
    d = presence.sweep_decision(fm, now=NOW_ISO)
    assert d["action"] == "skip" and d["reason"] == "within-until"


def test_sweep_decision_already_lapsed_is_noop():
    fm = {"engagement": _session(NOW - timedelta(hours=1), state="lapsed",
                                 lapsed_at=_iso(NOW - timedelta(minutes=30)))}
    assert presence.sweep_decision(fm, now=NOW_ISO)["action"] == "noop"


def test_sweep_decision_resident_and_occasional_skip():
    for mode in ("resident", "occasional"):
        d = presence.sweep_decision({"engagement": {"mode": mode, "until": None}},
                                    now=NOW_ISO)
        assert d["action"] == "skip" and d["reason"] == mode


def test_sweep_decision_legacy_no_engagement_skips_resident():
    d = presence.sweep_decision({"agent": "amy"}, now=NOW_ISO)
    assert d["action"] == "skip" and d["reason"] == "resident"


def test_sweep_decision_degraded_engagement_never_marks():
    # A session missing its required until degrades in parse_engagement — the
    # dead-session-looks-alive shape. The sweep must NEVER manufacture a lapse
    # from it.
    fm = {"engagement": {"mode": "session", "state": "active", "lapsed_at": None}}
    d = presence.sweep_decision(fm, now=NOW_ISO)
    assert d["action"] == "skip" and d["reason"] == "degraded"


def test_sweep_decision_unknown_mode_degraded_never_marks():
    fm = {"engagement": {"mode": "bogus", "until": _iso(NOW - timedelta(hours=1))}}
    d = presence.sweep_decision(fm, now=NOW_ISO)
    assert d["action"] == "skip" and d["reason"] == "degraded"


# --- the write: two fields only, everything else preserved -------------------

def test_mark_writes_two_fields_and_preserves_the_rest(monkeypatch):
    _pin(monkeypatch)
    t = CountingTransport()
    until = NOW - timedelta(hours=2)
    _put_presence(t, "amy", age_min=7, engagement=_session(until),
                  workstreams=["web", "infra"], summary="shipping W3")
    before = t.store[_shard_path("amy")]

    assert cli.main(["engagement", "sweep", "r"], transport=t) == 0

    after = t.store[_shard_path("amy")]
    fm = okf.parse_frontmatter(after)
    # the two — and only the two — mutated fields
    assert fm["engagement"]["state"] == "lapsed"
    assert fm["engagement"]["lapsed_at"] == NOW_ISO
    # everything else byte-preserved through the round-trip
    assert fm["engagement"]["mode"] == "session"
    assert fm["engagement"]["until"] == _iso(until)        # until NOT slid
    assert fm["timestamp"] == _iso(NOW - timedelta(minutes=7))  # NOT bumped — not a beat
    assert fm["workstreams"] == ["web", "infra"]
    assert fm["summary"] == "shipping W3"
    # body preserved verbatim
    assert okf.split_frontmatter(before)[1] == okf.split_frontmatter(after)[1]
    assert t.writes == [_shard_path("amy")]


def test_idempotent_second_sweep_writes_nothing(monkeypatch):
    _pin(monkeypatch)
    t = CountingTransport()
    _put_presence(t, "amy", engagement=_session(NOW - timedelta(hours=1)))
    assert cli.main(["engagement", "sweep", "r"], transport=t) == 0
    assert t.writes == [_shard_path("amy")]
    lapsed_at_1 = okf.parse_frontmatter(t.store[_shard_path("amy")])["engagement"]["lapsed_at"]

    # second sweep, no time change -> already-lapsed, no further write
    t.writes.clear()
    assert cli.main(["engagement", "sweep", "r"], transport=t) == 0
    assert t.writes == []
    lapsed_at_2 = okf.parse_frontmatter(t.store[_shard_path("amy")])["engagement"]["lapsed_at"]
    assert lapsed_at_1 == lapsed_at_2       # stamp untouched


# --- the SKIP matrix: NO write for any --------------------------------------

def test_skip_matrix_never_writes(monkeypatch):
    _pin(monkeypatch)
    t = CountingTransport()
    _put_presence(t, "res", engagement={"mode": "resident", "until": None})
    _put_presence(t, "occ", engagement={"mode": "occasional", "until": None})
    _put_presence(t, "future", engagement=_session(NOW + timedelta(hours=3)))
    _put_presence(t, "already", engagement=_session(NOW - timedelta(hours=1),
                  state="lapsed", lapsed_at=_iso(NOW - timedelta(hours=1))))
    _put_presence(t, "legacy")                       # no engagement field
    assert cli.main(["engagement", "sweep", "r"], transport=t) == 0
    assert t.writes == []                            # nothing marked, nothing written


def test_degraded_engagement_shard_is_skipped_not_marked(monkeypatch):
    _pin(monkeypatch)
    t = CountingTransport()
    # session missing until — degrades, must NOT be written
    _put_presence(t, "amy", engagement={"mode": "session", "state": "active",
                                        "lapsed_at": None})
    rc = cli.main(["engagement", "sweep", "r"], transport=t)
    assert t.writes == []
    assert rc != 0                                   # degraded shard is fail-visible


def test_unparseable_frontmatter_shard_is_skipped(monkeypatch):
    _pin(monkeypatch)
    t = CountingTransport()
    t.store["team/r/presence/bogus.md"] = "not frontmatter at all\njust prose\n"
    rc = cli.main(["engagement", "sweep", "r"], transport=t)
    assert t.writes == []
    assert rc != 0


def test_unreadable_shard_read_none_is_skipped(monkeypatch):
    _pin(monkeypatch)

    class _UnreadableShard(CountingTransport):
        def read(self, path):
            if path.startswith("team/r/presence/") and path.endswith(".md"):
                return None                          # listed but unreadable
            return super().read(path)

    t = _UnreadableShard()
    # listed (so enumeration succeeds) but reads None — a failed read must never
    # cause a write.
    _put_presence(t, "amy", engagement=_session(NOW - timedelta(hours=1)))
    rc = cli.main(["engagement", "sweep", "r"], transport=t)
    assert t.writes == []
    assert rc != 0


# --- READ-CONTRACT: enumeration failure is loud, never a silent clean sweep --

def test_enumeration_raises_is_degraded_rc_nonzero(monkeypatch, capsys):
    _pin(monkeypatch)

    class _FailListing(CountingTransport):
        def list_dir(self, prefix):
            if prefix == "team/r/presence/":
                raise TransportError("presence dir unreadable")
            return super().list_dir(prefix)

    t = _FailListing()
    _put_presence(t, "amy", engagement=_session(NOW - timedelta(hours=1)))
    rc = cli.main(["engagement", "sweep", "r"], transport=t)
    assert rc != 0
    err = capsys.readouterr()
    combined = err.out + err.err
    assert "DEGRADED" in combined
    # must NOT read as swept-clean
    assert "0 marked" not in err.out
    assert t.writes == []


def test_enumeration_degraded_json_flags_it(monkeypatch, capsys):
    _pin(monkeypatch)

    class _FailListing(CountingTransport):
        def list_dir(self, prefix):
            if prefix == "team/r/presence/":
                raise TransportError("boom")
            return super().list_dir(prefix)

    t = _FailListing()
    rc = cli.main(["engagement", "sweep", "r", "--json"], transport=t)
    assert rc != 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["enumeration_ok"] is False


# --- never parks, never releases roles --------------------------------------

def test_never_touches_role_or_continuity_docs(monkeypatch):
    _pin(monkeypatch)
    t = CountingTransport()
    _put_presence(t, "amy", engagement=_session(NOW - timedelta(hours=1)))
    # a role lease + a continuity doc held by the lapsing agent
    t.store["team/r/roles/reviewer.md"] = "---\ntype: RoleLease\nrole: reviewer\nagent: amy\n---\n"
    t.store["team/r/_coord/continuity/amy.md"] = "---\ntype: Continuity\nagent: amy\n---\n"
    role_before = t.store["team/r/roles/reviewer.md"]
    cont_before = t.store["team/r/_coord/continuity/amy.md"]

    assert cli.main(["engagement", "sweep", "r"], transport=t) == 0

    # ONLY the presence shard was written; role + continuity untouched byte-for-byte
    assert t.writes == [_shard_path("amy")]
    assert t.store["team/r/roles/reviewer.md"] == role_before
    assert t.store["team/r/_coord/continuity/amy.md"] == cont_before


# --- --dry-run writes nothing -----------------------------------------------

def test_dry_run_writes_nothing_but_reports_would_mark(monkeypatch, capsys):
    _pin(monkeypatch)
    t = CountingTransport()
    _put_presence(t, "amy", engagement=_session(NOW - timedelta(hours=1)))
    rc = cli.main(["engagement", "sweep", "r", "--dry-run"], transport=t)
    assert rc == 0
    assert t.writes == []                            # nothing written
    # shard still active (unchanged)
    assert okf.parse_frontmatter(t.store[_shard_path("amy")])["engagement"]["state"] == "active"
    out = capsys.readouterr().out
    assert "amy" in out and "DRY-RUN" in out


def test_dry_run_json_marks_would_be_marked(monkeypatch, capsys):
    _pin(monkeypatch)
    t = CountingTransport()
    _put_presence(t, "amy", engagement=_session(NOW - timedelta(hours=1)))
    rc = cli.main(["engagement", "sweep", "r", "--dry-run", "--json"], transport=t)
    assert rc == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert out["marked"] == ["amy"]
    assert t.writes == []


# --- summary output ----------------------------------------------------------

def test_summary_buckets_reasons(monkeypatch, capsys):
    _pin(monkeypatch)
    t = CountingTransport()
    _put_presence(t, "mark1", engagement=_session(NOW - timedelta(hours=1)))
    _put_presence(t, "res", engagement={"mode": "resident", "until": None})
    _put_presence(t, "future", engagement=_session(NOW + timedelta(hours=1)))
    _put_presence(t, "already", engagement=_session(NOW - timedelta(hours=1),
                  state="lapsed", lapsed_at=_iso(NOW - timedelta(hours=1))))
    rc = cli.main(["engagement", "sweep", "r"], transport=t)
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 marked" in out
    assert "1 already-lapsed" in out


def test_json_result_shape(monkeypatch, capsys):
    _pin(monkeypatch)
    t = CountingTransport()
    _put_presence(t, "mark1", engagement=_session(NOW - timedelta(hours=1)))
    _put_presence(t, "res", engagement={"mode": "resident", "until": None})
    rc = cli.main(["engagement", "sweep", "r", "--json"], transport=t)
    assert rc == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["team"] == "r"
    assert out["marked"] == ["mark1"]
    assert out["enumeration_ok"] is True
    assert "resident" in out["skipped"]
