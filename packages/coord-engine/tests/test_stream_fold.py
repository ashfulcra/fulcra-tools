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
        {"id": "c", "recorded_at": "2026-08-21T20:02:00+00:00",
         "sources": ["alice"],
         "note": _payload(to="coord-boss", kind="response", slug="s-1")},
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


# --- round-2 findings (codex-coder, 2026-08-21): four fail-open paths --------

def test_corrupt_state_is_unknown_nonzero_and_left_untouched():
    t = StreamOnlyTransport([]); _cfg_store(t)
    bad = '{"v": 999, "cursor": "not-a-time"}'
    t.store[stream_fold.state_path(TEAM, "alice")] = bad
    out = stream_fold.fold(t, TEAM, "alice", now=NOW)
    assert out.state is OutcomeState.UNKNOWN and out.rc != 0
    # the corrupt document is preserved for forensics, never overwritten
    assert t.store[stream_fold.state_path(TEAM, "alice")] == bad


def test_broadcast_close_is_per_recipient_not_first_responder_wins():
    open_all = _rec("a", "2026-08-21T20:00:00+00:00",
                    _payload(to=records.BROADCAST, kind="directive", slug="all-1"))
    bob_close = {"id": "b", "recorded_at": "2026-08-21T20:01:00+00:00",
                 "sources": ["bob"],
                 "note": _payload(to="owner", kind="response", slug="all-1")}
    t = StreamOnlyTransport([open_all, bob_close]); _cfg_store(t)
    out_bob = stream_fold.fold(t, TEAM, "bob", now=NOW)
    assert out_bob.rows == (), "bob responded; bob's copy closes"
    t2 = StreamOnlyTransport([open_all, bob_close]); _cfg_store(t2)
    out_alice = stream_fold.fold(t2, TEAM, "alice", now=NOW)
    assert [r["slug"] for r in out_alice.rows] == ["all-1"], (
        "bob's response must not close alice's copy of a broadcast")


def test_task_done_write_failure_emits_no_close_and_returns_nonzero(monkeypatch, capsys):
    from argparse import Namespace
    calls = []

    class T:
        def read(self, path):
            return ("---\ntype: Task\ntitle: x\nstatus: active\nowner: owner\n"
                    "assignee: alice\ntimestamp: 2026-08-21T00:00:00Z\n---\n# x\n")
        def write(self, path, content):
            return False  # the durable close never lands

    monkeypatch.setattr(cli, "_now", lambda: NOW)
    monkeypatch.setattr(cli, "_emit_response_companion",
                        lambda *a, **k: calls.append(a) or True)
    rc = cli.cmd_task_done(
        Namespace(team=TEAM, name="t-1", evidence="done", agent="alice"), T())
    assert rc != 0, "a close that never landed durably is not rc 0"
    assert calls == [], "no stream close may be emitted for a failed doc write"


def test_state_write_failure_is_unknown_nonzero_not_a_checkpoint():
    class NoPersist(StreamOnlyTransport):
        def write(self, path, content):
            return False
    rows = [_rec("a", "2026-08-21T20:00:00+00:00",
                 _payload(to="alice", kind="directive", slug="s-1"))]
    t = NoPersist(rows); _cfg_store(t)
    out = stream_fold.fold(t, TEAM, "alice", now=NOW)
    assert out.state is OutcomeState.UNKNOWN and out.rc != 0, (
        "the architecture claims a durable cursor; failing to persist it "
        "cannot be a successful checkpoint")
