"""The stream fold: forward-from-cursor, zero enumeration, honest coverage.

Pinned against the 2026-08-21 live measurements: the file fold and the stream
disagreed on 231 of 260 obligations because (a) `tell --fyi` events carried no
fyi flag and replayed as permanent opens, and (b) `task done` emitted no close
event at all. Both are protocol fixes in this change; both are pinned here.
"""
from datetime import datetime, timezone
import json

import pytest

from coord_engine import cli, records, stream_fold
from coord_engine.outcome import CoverageState, OutcomeState

NOW = datetime(2026, 8, 21, 21, 0, tzinfo=timezone.utc)
TEAM = "fulcra"


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    # Every fold call here passes now=NOW explicitly, but the convention wants
    # the module clock pinned so a future test that forgets cannot drift onto
    # the real clock (the repo's third real-clock boundary bit CI 2026-07-14).
    monkeypatch.setattr(cli, "_now", lambda: NOW)


def _rec(rid, ts, note):
    return {"id": rid, "recorded_at": ts, "note": note}


def _payload(**kw):
    kw.setdefault("priority", "P1")
    return records.build_payload(**kw)


class StreamOnlyTransport:
    """A transport that HARD-FAILS any enumeration — the architecture, as a test double."""

    def __init__(self, rows):
        self.rows = rows
        self.store = {}

    def list_dir(self, path):  # pragma: no cover - the assertion IS the point
        raise AssertionError(f"fold enumerated a directory: {path}")

    def read(self, path):
        return self.store.get(path)

    def write(self, path, content):
        self.store[path] = content
        return True

    def records(self, data_type, since, until):
        return self.rows

    def record_read(self, *a, **k):
        raise AssertionError("unexpected record_read")


def _cfg_store(t):
    t.store[f"team/{TEAM}/_coord/bus-v3/records.json"] = json.dumps(
        {"data_type": "MomentAnnotation/chan", "api_version": "v1alpha1"})


def test_directive_opens_and_response_closes_in_one_batch():
    rows = [
        _rec("a", "2026-08-21T20:00:00+00:00",
             _payload(to="alice", kind="directive", slug="s-1")),
        _rec("b", "2026-08-21T20:01:00+00:00",
             _payload(to="alice", kind="directive", slug="s-2")),
        _rec("c", "2026-08-21T20:02:00+00:00",
             _payload(to="coord-boss", kind="response", slug="s-1")),
    ]
    t = StreamOnlyTransport(rows); _cfg_store(t)
    out = stream_fold.fold(t, TEAM, "alice", now=NOW)
    assert out.state is OutcomeState.DATA and out.rc == 0
    assert [r["slug"] for r in out.rows] == ["s-2"]


def test_fyi_directive_opens_nothing():
    rows = [_rec("a", "2026-08-21T20:00:00+00:00",
                 _payload(to="alice", kind="directive", slug="note-1", fyi=True))]
    t = StreamOnlyTransport(rows); _cfg_store(t)
    out = stream_fold.fold(t, TEAM, "alice", now=NOW)
    assert out.state is OutcomeState.CLEAR and out.rc == 0
    assert out.rows == ()


def test_broadcast_reaches_every_agent():
    rows = [_rec("a", "2026-08-21T20:00:00+00:00",
                 _payload(to=records.BROADCAST, kind="directive", slug="all-1"))]
    t = StreamOnlyTransport(rows); _cfg_store(t)
    out = stream_fold.fold(t, TEAM, "anyone", now=NOW)
    assert [r["slug"] for r in out.rows] == ["all-1"]


def test_failed_stream_read_is_unknown_nonzero_and_never_serves_stale():
    t = StreamOnlyTransport(None); _cfg_store(t)
    out = stream_fold.fold(t, TEAM, "alice", now=NOW)
    assert out.state is OutcomeState.UNKNOWN and out.rc != 0
    assert out.rows == ()
    assert "not served as fresh" in (out.coverage[0].reason or "")


def test_missing_records_config_is_unknown_not_empty():
    t = StreamOnlyTransport([])
    out = stream_fold.fold(t, TEAM, "alice", now=NOW)
    assert out.state is OutcomeState.UNKNOWN and out.rc != 0


def test_warm_rerun_cost_is_new_events_and_state_round_trips():
    rows = [_rec("a", "2026-08-21T20:00:00+00:00",
                 _payload(to="alice", kind="directive", slug="s-1"))]
    t = StreamOnlyTransport(rows); _cfg_store(t)
    out1 = stream_fold.fold(t, TEAM, "alice", now=NOW)
    assert "+1 events" in (out1.coverage[0].reason or "")
    out2 = stream_fold.fold(t, TEAM, "alice", now=NOW)
    assert "+0 events" in (out2.coverage[0].reason or "")
    assert [r["slug"] for r in out2.rows] == ["s-1"]


def test_state_write_failure_is_declared_not_silent():
    class NoPersist(StreamOnlyTransport):
        def write(self, path, content):
            return False
    rows = [_rec("a", "2026-08-21T20:00:00+00:00",
                 _payload(to="alice", kind="directive", slug="s-1"))]
    t = NoPersist(rows); _cfg_store(t)
    out = stream_fold.fold(t, TEAM, "alice", now=NOW)
    assert "STATE WRITE FAILED" in (out.coverage[0].reason or "")


def test_fyi_flag_round_trips_through_payload():
    note = records.build_payload(to="a", kind="directive", priority="P2",
                                 slug="s", fyi=True)
    ev = records.parse_payload(note)
    assert ev is not None and ev["fyi"] is True
    plain = records.parse_payload(
        records.build_payload(to="a", kind="directive", priority="P2", slug="s"))
    assert plain is not None and plain["fyi"] is False
    # and the flag never leaks into non-fyi payloads' JSON
    assert '"fyi"' not in records.build_payload(
        to="a", kind="directive", priority="P2", slug="s")
