"""W2 — engagement-aware liveness fold (wake-router build).

W1 parsed engagement inertly; W1.5 made a working agent's timestamp provably
fresh. W2 layers the truth table on top of the pure ``classify`` freshness:

- ``classify`` stays PURE (freshness only) — never consults engagement.
- ``liveness(shard, now=…)`` is the COMBINER: freshness + engagement -> a richer
  verdict ``{state, freshness, annotation, engagement}``. A ``session`` past its
  ``until`` (or a durable ``state: lapsed`` marker) renders **LAPSED** — distinct
  from stale/dead, explained, role-retaining. Dormancy (LAPSED) and staleness
  (freshness) are ORTHOGONAL axes rendered as two facts, never a merged label.
- ``engagement gate <team>`` is the deterministic mixed-fleet coverage check.
- The vacancy/escalation SEMANTIC change (LAPSED holder = explained absence)
  ships behind that gate — dormant today (gate BLOCKED), both branches pinned.

These tests are red-first: the combiner, the gate, and the gated suppression do
not exist yet.
"""

from datetime import datetime, timedelta, timezone
import json

import pytest

from coord_engine import cli, okf, presence, tasks
from coord_engine_test_helpers import FakeTransport

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat().replace("+00:00", "Z")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _shard(agent, *, age_min=1.0, engagement=None):
    """A presence shard dict as the folds see it (parsed frontmatter)."""
    s = {
        "agent": agent,
        "workstreams": [],
        "summary": "",
        "timestamp": _iso(NOW - timedelta(minutes=age_min)),
    }
    if engagement is not None:
        s["engagement"] = engagement
    return s


def _session(until_dt, *, state="active", lapsed_at=None):
    return {"mode": "session", "until": _iso(until_dt),
            "state": state, "lapsed_at": lapsed_at}


# --- classify stays PURE (unchanged) ----------------------------------------

def test_classify_ignores_engagement_entirely():
    # classify is a freshness function of the timestamp alone. A session past its
    # until with a fresh timestamp is still 'live' by classify — the LAPSED verdict
    # is the combiner's job, layered on top, never classify's.
    ts = _iso(NOW - timedelta(minutes=5))
    assert presence.classify(ts, now=NOW_ISO) == "live"
    # signature/behaviour unchanged: still three bands off the timestamp.
    assert presence.classify(_iso(NOW - timedelta(hours=5)), now=NOW_ISO) == "idle"
    assert presence.classify(_iso(NOW - timedelta(hours=30)), now=NOW_ISO) == "stale"


# --- the truth-table matrix (heart of W2) -----------------------------------

def test_truth_table_resident_is_freshness_verbatim():
    r = presence.liveness(_shard("amy", age_min=5,
                                 engagement={"mode": "resident", "until": None}),
                          now=NOW_ISO)
    assert r["state"] == "live"
    assert r["freshness"] == "live"
    assert "LAPSED" not in r["annotation"]


def test_truth_table_legacy_no_engagement_is_freshness_verbatim():
    r = presence.liveness(_shard("amy", age_min=5), now=NOW_ISO)
    assert r["state"] == "live" and r["freshness"] == "live"


def test_truth_table_occasional_adds_note_classification_unchanged():
    r = presence.liveness(_shard("amy", age_min=5,
                                 engagement={"mode": "occasional", "until": None}),
                          now=NOW_ISO)
    assert r["state"] == "live"           # classification unchanged
    assert "occasional" in r["annotation"]


def test_truth_table_session_within_window_is_freshness():
    r = presence.liveness(_shard("amy", age_min=5,
                                 engagement=_session(NOW + timedelta(hours=3))),
                          now=NOW_ISO)
    assert r["state"] == "live"
    assert "within committed window" in r["annotation"]
    assert "LAPSED" not in r["annotation"]


def test_truth_table_session_past_until_but_beating_is_LAPSED_active():
    # The load-bearing row: past-until AND still beating -> LAPSED (dormancy axis)
    # with the freshness axis honestly rendered as 'still beating', a nudge to
    # extend — NOT silently live.
    r = presence.liveness(_shard("amy", age_min=8,
                                 engagement=_session(NOW - timedelta(hours=1))),
                          now=NOW_ISO)
    assert r["state"] == "lapsed"
    assert r["freshness"] == "live"       # orthogonal: still beating
    assert "LAPSED" in r["annotation"]
    assert "beating" in r["annotation"]
    assert "extend" in r["annotation"]


def test_truth_table_session_past_until_and_stale_is_LAPSED_stale():
    r = presence.liveness(_shard("amy", age_min=30 * 60,      # 30h -> stale
                                 engagement=_session(NOW - timedelta(hours=40))),
                          now=NOW_ISO)
    assert r["state"] == "lapsed"
    assert r["freshness"] == "stale"
    assert "LAPSED" in r["annotation"]
    assert "stale" in r["annotation"]


def test_truth_table_durable_lapsed_marker_is_honored_even_within_until():
    # W3 writes engagement.state=lapsed. Honor the durable marker even if the
    # real-time until has not yet passed.
    r = presence.liveness(
        _shard("amy", age_min=5,
               engagement=_session(NOW + timedelta(hours=3), state="lapsed",
                                   lapsed_at=_iso(NOW - timedelta(hours=1)))),
        now=NOW_ISO)
    assert r["state"] == "lapsed"


def test_lapsed_is_boundary_inclusive_now_equals_until():
    # now >= until per the truth table.
    r = presence.liveness(_shard("amy", age_min=5, engagement=_session(NOW)),
                          now=NOW_ISO)
    assert r["state"] == "lapsed"


# --- dormancy is DISTINCT from stale/dead -----------------------------------

def test_lapsed_state_is_never_the_stale_label():
    r = presence.liveness(_shard("amy", age_min=8,
                                 engagement=_session(NOW - timedelta(hours=1))),
                          now=NOW_ISO)
    # LAPSED is its own primary state; it must not collapse to 'stale' (a live
    # beat) nor hide the freshness axis.
    assert r["state"] == "lapsed"
    assert r["state"] != r["freshness"]   # two independent facts


# --- CONCUR: stale-nudge visible --------------------------------------------

def test_stale_row_shows_a_nudge():
    r = presence.liveness(_shard("amy", age_min=30 * 60), now=NOW_ISO)   # 30h
    assert r["state"] == "stale"
    assert "nudge" in r["annotation"]


# --- CONCUR: exact-id matching (lapsed_holder) ------------------------------

def test_lapsed_holder_matches_exact_id_only():
    shards = [_shard("coord-maintainer", age_min=8,
                     engagement=_session(NOW - timedelta(hours=1)))]
    # exact id -> found
    assert presence.lapsed_holder(["coord-maintainer"], shards, now=NOW_ISO) \
        == "coord-maintainer"
    # a near-miss id must NOT match (no substring/fuzzy — the corrupt-id lesson)
    assert presence.lapsed_holder(["coord-maintaine"], shards, now=NOW_ISO) is None
    assert presence.lapsed_holder(["coord-maintainer-2"], shards, now=NOW_ISO) is None


def test_lapsed_holder_ignores_non_lapsed_holders():
    shards = [_shard("amy", age_min=5, engagement=_session(NOW + timedelta(hours=3)))]
    assert presence.lapsed_holder(["amy"], shards, now=NOW_ISO) is None


# --- roster / digest carry the combiner additively --------------------------

def test_roster_carries_state_freshness_annotation_additively():
    ros = presence.roster(
        [_shard("amy", age_min=8, engagement=_session(NOW - timedelta(hours=1)))],
        now=NOW_ISO)
    row = ros[0]
    # back-compat: liveness stays the PURE freshness value
    assert row["liveness"] == "live"
    assert row["freshness"] == "live"
    # W2: engagement-aware state + annotation added
    assert row["state"] == "lapsed"
    assert "LAPSED" in row["annotation"]


def test_agents_digest_carries_state_and_annotation():
    d = presence.agents_digest(
        [], [_shard("amy", age_min=8, engagement=_session(NOW - timedelta(hours=1)))],
        now=NOW_ISO)
    row = {a["agent"]: a for a in d}["amy"]
    assert row["liveness"] == "live"      # back-compat
    assert row["state"] == "lapsed"
    assert "LAPSED" in row["annotation"]


# --- the engagement gate (pure fold) ----------------------------------------

def test_gate_pass_when_every_live_agent_has_engagement():
    shards = [
        _shard("amy", age_min=5, engagement={"mode": "resident", "until": None}),
        _shard("bob", age_min=5, engagement=_session(NOW + timedelta(hours=3))),
    ]
    res = presence.engagement_gate(shards, {}, now=NOW_ISO, defaults_ok=True)
    assert res["status"] == "PASS"
    assert all(a["coverage"] == "COVERED" for a in res["agents"])


def test_gate_blocked_when_a_live_agent_is_uncovered():
    shards = [
        _shard("amy", age_min=5, engagement={"mode": "resident", "until": None}),
        _shard("bob", age_min=5),                       # legacy, no engagement
    ]
    res = presence.engagement_gate(shards, {}, now=NOW_ISO, defaults_ok=True)
    assert res["status"] == "BLOCKED"
    cov = {a["agent"]: a["coverage"] for a in res["agents"]}
    assert cov == {"amy": "COVERED", "bob": "UNCOVERED"}


def test_gate_defaults_map_covers_a_legacy_agent():
    shards = [_shard("bob", age_min=5)]                 # legacy, no engagement
    res = presence.engagement_gate(shards, {"bob": "occasional"}, now=NOW_ISO,
                                   defaults_ok=True)
    assert res["status"] == "PASS"
    a = res["agents"][0]
    assert a["coverage"] == "COVERED" and a["via"] == "defaults"


def test_gate_stale_and_idle_agents_never_block():
    shards = [
        _shard("amy", age_min=5, engagement={"mode": "resident", "until": None}),
        _shard("old", age_min=30 * 60),                 # stale legacy -> excluded
        _shard("idl", age_min=5 * 60),                  # idle legacy -> excluded
    ]
    res = presence.engagement_gate(shards, {}, now=NOW_ISO, defaults_ok=True)
    assert [a["agent"] for a in res["agents"]] == ["amy"]   # only the live agent
    assert res["status"] == "PASS"


def test_gate_degraded_when_defaults_unknown_even_if_all_have_engagement():
    # Fail-closed: an UNKNOWN defaults map never PASSes, even when every live agent
    # carries its own engagement field.
    shards = [_shard("amy", age_min=5, engagement={"mode": "resident", "until": None})]
    res = presence.engagement_gate(shards, {}, now=NOW_ISO, defaults_ok=False)
    assert res["status"] == "DEGRADED"


def test_gate_malformed_engagement_is_not_coverage():
    # A shard carrying a malformed engagement field degrades in parse_engagement;
    # coverage is UNKNOWN, so it does not count as covered.
    shards = [_shard("amy", age_min=5, engagement={"mode": "bogus"})]
    res = presence.engagement_gate(shards, {}, now=NOW_ISO, defaults_ok=True)
    assert res["status"] == "BLOCKED"
    assert res["agents"][0]["coverage"] == "UNCOVERED"


def test_gate_degraded_when_roster_unknown_even_if_present_agents_covered():
    # Fail-closed on the ROSTER read too: an UNKNOWN roster enumeration never
    # PASSes, even when the shards we DID read are all covered — the agents we
    # could not enumerate/read are unknowable coverage.
    shards = [_shard("amy", age_min=5, engagement={"mode": "resident", "until": None})]
    res = presence.engagement_gate(shards, {}, now=NOW_ISO, defaults_ok=True,
                                   roster_ok=False)
    assert res["status"] == "DEGRADED"


# --- the engagement gate CLI + defaults read-contract -----------------------

def _pin(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: NOW)


def _put_presence(t, agent, *, age_min=5, engagement=None):
    fm = {"type": "Presence", "title": f"presence — {agent}", "agent": agent,
          "workstreams": [], "summary": "",
          "timestamp": _iso(NOW - timedelta(minutes=age_min))}
    if engagement is not None:
        fm["engagement"] = engagement
    t.store[f"team/r/presence/{tasks.agent_key(agent)}.md"] = \
        okf.render_frontmatter(fm) + f"\n# Presence: {agent}\n"


def _defaults_path():
    return "team/r/_coord/router/engagement-defaults.json"


def test_briefing_feed_delta_keeps_session_presence_time_dirty(monkeypatch, capsys):
    """E2 chooses ratification option (a): presence stays time-dirty.

    Even when the data-updates feed reports no byte changes, briefing re-reads the
    session shard and evaluates ``now >= until`` at the current clock.  Therefore
    its full-roster self-check cannot drift behind ``presence show`` on LAPSED.
    """
    t = FakeTransport()
    _put_presence(
        t,
        "amy",
        engagement=_session(NOW + timedelta(minutes=30)),
    )
    # Seed a readable aggregate so E2's feed-delta row path is active.
    from coord_engine import reconcile
    reconcile.reconcile(t, "r", now=NOW_ISO, today=NOW_ISO[:10], host="h")
    calls = []

    def updates(since, *, team=None):
        calls.append((since, team))
        return []

    t.updates = updates
    monkeypatch.setattr(cli, "_now", lambda: NOW + timedelta(hours=1))

    assert cli.main(["briefing", "r", "--agent", "amy", "--json"],
                    transport=t) == 0
    out = json.loads(capsys.readouterr().out)

    amy = next(row for row in out["presence"] if row.get("agent") == "amy")
    assert amy["state"] == "lapsed"
    assert "LAPSED" in amy["annotation"]
    assert calls and calls[0][1] == "r"


def test_cli_gate_pass_rc0(monkeypatch, capsys):
    _pin(monkeypatch)
    t = FakeTransport()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    assert cli.main(["engagement", "gate", "r"], transport=t) == 0
    assert "PASS" in capsys.readouterr().out


def test_cli_gate_blocked_rc1(monkeypatch, capsys):
    _pin(monkeypatch)
    t = FakeTransport()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    _put_presence(t, "bob")                             # legacy, uncovered
    assert cli.main(["engagement", "gate", "r"], transport=t) == 1
    out = capsys.readouterr().out
    assert "BLOCKED" in out and "UNCOVERED" in out


def test_cli_gate_defaults_absent_confirmed_via_listing_is_empty_map(monkeypatch):
    # The defaults file genuinely does not exist. transport.read returns None; the
    # RAISING list_dir shows the router dir does not contain it -> confirmed absent
    # -> empty defaults map -> a covered fleet PASSes (never fails just because the
    # optional file is missing).
    _pin(monkeypatch)
    t = FakeTransport()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    defaults, ok = cli._load_engagement_defaults(t, "r")
    assert defaults == {} and ok is True
    assert cli.main(["engagement", "gate", "r"], transport=t) == 0


def test_cli_gate_defaults_present_but_unreadable_is_DEGRADED(monkeypatch, capsys):
    # READ-CONTRACT LENS: read() returns None on BOTH missing and transient
    # failure. Here the file IS present (list_dir shows it) but read() fails ->
    # UNKNOWN -> fail closed: the gate is DEGRADED/BLOCKED, never PASS.
    _pin(monkeypatch)

    class _UnreadableDefaults(FakeTransport):
        def read(self, path):
            if path == _defaults_path():
                return None                    # transient failure masquerading as absent
            return super().read(path)

    t = _UnreadableDefaults()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    # make the file appear in the router listing (present-but-unreadable)
    t.store[_defaults_path()] = '{"amy": "resident"}'
    defaults, ok = cli._load_engagement_defaults(t, "r")
    assert ok is False                          # UNKNOWN, not confirmed-absent
    rc = cli.main(["engagement", "gate", "r"], transport=t)
    assert rc == 1                              # fail closed
    assert "DEGRADED" in capsys.readouterr().out


def test_cli_gate_defaults_present_but_unparseable_is_DEGRADED(monkeypatch):
    _pin(monkeypatch)
    t = FakeTransport()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    t.store[_defaults_path()] = "{not json"
    defaults, ok = cli._load_engagement_defaults(t, "r")
    assert ok is False
    assert cli.main(["engagement", "gate", "r"], transport=t) == 1


def test_cli_gate_defaults_listing_failure_is_DEGRADED(monkeypatch):
    # If even the disambiguating listing fails, the prior is UNKNOWN -> fail closed.
    _pin(monkeypatch)

    class _FailRouterListing(FakeTransport):
        def read(self, path):
            if path == _defaults_path():
                return None
            return super().read(path)

        def list_dir(self, prefix):
            if prefix == "team/r/_coord/router/":
                from coord_engine.transport import TransportError
                raise TransportError("router dir unreadable")
            return super().list_dir(prefix)

    t = _FailRouterListing()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    defaults, ok = cli._load_engagement_defaults(t, "r")
    assert ok is False
    assert cli.main(["engagement", "gate", "r"], transport=t) == 1


# --- roster read-contract: an UNKNOWN presence roster is DEGRADED, never PASS -

class _FailPresenceListing(FakeTransport):
    """Presence-dir enumeration raises; every OTHER listing/read still works (so
    the defaults read confirms absent normally). Isolates the roster-read hole."""
    def list_dir(self, prefix):
        if prefix == "team/r/presence/":
            from coord_engine.transport import TransportError
            raise TransportError("presence dir unreadable")
        return super().list_dir(prefix)


class _UnreadablePresenceShard(FakeTransport):
    """A listed presence shard whose read returns None — present-but-unreadable,
    that agent's coverage unknowable. Everything else reads normally."""
    def read(self, path):
        if path.startswith("team/r/presence/") and path.endswith(".md"):
            return None
        return super().read(path)


def test_cli_gate_failing_presence_listing_is_DEGRADED(monkeypatch, capsys):
    _pin(monkeypatch)
    t = _FailPresenceListing()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    shards, ok = cli._presence_shards_status(t, "r")
    assert ok is False                          # roster enumeration UNKNOWN
    assert cli.main(["engagement", "gate", "r"], transport=t) == 1   # not PASS
    assert "DEGRADED" in capsys.readouterr().out


def test_cli_gate_listed_but_unreadable_shard_is_DEGRADED(monkeypatch, capsys):
    _pin(monkeypatch)
    t = _UnreadablePresenceShard()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    shards, ok = cli._presence_shards_status(t, "r")
    assert ok is False                          # listed but unreadable -> UNKNOWN
    assert cli.main(["engagement", "gate", "r"], transport=t) == 1
    assert "DEGRADED" in capsys.readouterr().out


def test_cli_gate_confirmed_empty_roster_passes_vacuously(monkeypatch, capsys):
    # The distinguishing case: a CONFIRMED-empty roster (listing succeeded, nothing
    # there) may still PASS — unlike an UNKNOWN roster, which must not.
    _pin(monkeypatch)
    t = FakeTransport()
    shards, ok = cli._presence_shards_status(t, "r")
    assert (shards, ok) == ([], True)
    assert cli.main(["engagement", "gate", "r"], transport=t) == 0
    assert "PASS" in capsys.readouterr().out


def test_engagement_gate_passes_false_on_failing_presence_listing(monkeypatch):
    # The escalation predicate must return False on an UNKNOWN roster (the bug:
    # _presence_shards swallowed the TransportError to [], so no exception fired
    # and the empty-list all(...) reported PASS -> True -> fail-open suppression).
    _pin(monkeypatch)
    t = _FailPresenceListing()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    assert cli._engagement_gate_passes(t, "r", now=NOW_ISO) is False


def test_cli_gate_listed_but_unparseable_shard_is_DEGRADED(monkeypatch, capsys):
    # r2 variant: a listed shard that READS fine but has unparseable frontmatter
    # must NOT synthesize a timestampless phantom row (classified stale, silently
    # excluded from the live population while roster_ok stays True -> PASS). Its
    # freshness/coverage is UNKNOWN -> the roster read is degraded, same as an
    # unreadable shard. Covered live agent present to prove the fail-OPEN: without
    # the fix the malformed shard is excluded and amy alone PASSes.
    _pin(monkeypatch)
    t = FakeTransport()
    _put_presence(t, "amy", engagement={"mode": "resident", "until": None})
    t.store["team/r/presence/bogus.md"] = "not frontmatter at all\njust prose\n"
    shards, ok = cli._presence_shards_status(t, "r")
    assert ok is False                          # parse failure -> UNKNOWN coverage
    assert [s.get("agent") for s in shards] == ["amy"]   # no phantom row emitted
    assert cli.main(["engagement", "gate", "r"], transport=t) == 1
    assert "DEGRADED" in capsys.readouterr().out


def test_engagement_gate_passes_false_on_unparseable_shard(monkeypatch):
    _pin(monkeypatch)
    t = FakeTransport()
    t.store["team/r/presence/bogus.md"] = "not frontmatter at all\njust prose\n"
    assert cli._engagement_gate_passes(t, "r", now=NOW_ISO) is False


# --- the gated vacancy/escalation semantic change (both branches) -----------

def _role_and_stale_session_lease(t, *, role="reviewer", holder="amy"):
    """A registered role whose only lease is STALE (past SLA), held by a session
    agent whose PRESENCE is LAPSED (past until, still beating). The role is VACANT
    past SLA -> escalation_due; the holder's session is explained absence."""
    t.store[f"team/r/roles/{role}.md"] = \
        "---\ntype: Role\npolicy: shared\nsla_hours: 24\nmaintainer: boss\n---\n"
    lease_fm = {"type": "Lease", "agent": holder,
                "timestamp": _iso(NOW - timedelta(hours=48))}    # stale lease -> VACANT
    t.store[f"team/r/roles/{role}/leases/{tasks.agent_key(holder)}.md"] = \
        okf.render_frontmatter(lease_fm) + "\nholding\n"
    # holder's presence: fresh beat, session past until -> LAPSED + live
    _put_presence(t, holder, age_min=8,
                  engagement=_session(NOW - timedelta(hours=1)))


def _escalation_written(t, role="reviewer"):
    today = NOW.strftime("%Y-%m-%d")
    return f"team/r/roles/{role}/escalations/{today}.md" in t.store


def test_escalate_gate_BLOCKED_runs_todays_behavior_and_escalates(monkeypatch, capsys):
    # Gate is BLOCKED (an uncovered live agent exists) -> the semantic change is
    # DORMANT -> today's behavior verbatim: a VACANT role past SLA escalates, even
    # though its holder's session has lapsed.
    _pin(monkeypatch)
    t = FakeTransport()
    _role_and_stale_session_lease(t)
    _put_presence(t, "stranger")                # legacy live agent -> gate BLOCKED
    assert cli._engagement_gate_passes(t, "r", now=NOW_ISO) is False
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert _escalation_written(t)               # escalated as today
    assert "escalated reviewer" in capsys.readouterr().out


def test_escalate_gate_PASS_suppresses_lapsed_holder_explained(monkeypatch, capsys):
    # Gate PASSES (every live agent covered) -> the semantic change ACTIVATES: the
    # VACANT role's holder is a LAPSED session -> explained absence, role-retaining
    # -> escalation SUPPRESSED, and SAID so (never silently).
    _pin(monkeypatch)
    t = FakeTransport()
    _role_and_stale_session_lease(t)            # amy: covered (session engagement)
    assert cli._engagement_gate_passes(t, "r", now=NOW_ISO) is True
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert not _escalation_written(t)           # suppressed
    err = capsys.readouterr().err
    assert "amy" in err and "lapsed" in err.lower() and "suppress" in err.lower()


def test_escalate_gate_PASS_still_escalates_a_truly_vacant_role(monkeypatch, capsys):
    # Gate PASSES but the vacant role's holder is NOT lapsed (a genuinely dark
    # holder) -> the suppression must NOT fire; escalate as normal.
    _pin(monkeypatch)
    t = FakeTransport()
    t.store["team/r/roles/reviewer.md"] = \
        "---\ntype: Role\npolicy: shared\nsla_hours: 24\nmaintainer: boss\n---\n"
    lease_fm = {"type": "Lease", "agent": "amy",
                "timestamp": _iso(NOW - timedelta(hours=48))}
    t.store[f"team/r/roles/reviewer/leases/{tasks.agent_key('amy')}.md"] = \
        okf.render_frontmatter(lease_fm) + "\nholding\n"
    # amy's presence: session WITHIN window (not lapsed) but covered -> gate PASS
    _put_presence(t, "amy", age_min=5, engagement=_session(NOW + timedelta(hours=3)))
    assert cli._engagement_gate_passes(t, "r", now=NOW_ISO) is True
    assert cli.main(["escalate", "r"], transport=t) == 0
    assert _escalation_written(t)               # not suppressed — holder isn't lapsed
