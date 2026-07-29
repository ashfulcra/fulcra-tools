"""Coord v3 write side — the timer leg.

A record with a FUTURE ``recorded_at`` is accepted by the platform and stays
invisible to every "what's new" window until its time arrives (verified live
2026-07-27), so writing one IS scheduling. These tests pin the three layers:
the transport write (stdin-fed, fail-closed False), config resolution
(fail-closed None — never write into a guessed stream), and ``remind``'s
emission (durable doc first; a missing config or failed write degrades
latency, never loses the reminder).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from coord_engine import cli, records, transport as transport_mod
from coord_engine_test_helpers import FakeTransport


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv(records.ENV_DATA_TYPE, raising=False)
    monkeypatch.delenv(records.ENV_API_VERSION, raising=False)


# --- transport.record_write ---------------------------------------------------

def _capture_run(monkeypatch, rc=0, out="Recorded 1 record"):
    calls = []

    def fake_run(argv, timeout, *, stdin_data=None, **kw):
        calls.append({"argv": argv, "stdin": stdin_data})
        return rc, out, ""

    monkeypatch.setattr(transport_mod, "run_bounded", fake_run)
    return calls


def test_record_write_feeds_payload_via_stdin_with_future_recorded_at(monkeypatch):
    calls = _capture_run(monkeypatch)
    t = transport_mod.FulcraFileTransport(command=["fulcra-api"], timeout=5)
    ok = t.record_write("MomentAnnotation/abc", "v1alpha1", '{"v":1}',
                        "coord-boss", recorded_at="2026-08-01T00:00:00+00:00")
    assert ok is True
    (call,) = calls
    assert call["argv"][:3] == ["fulcra-api", "record", "MomentAnnotation/abc"]
    assert "--api-version" in call["argv"] and "v1alpha1" in call["argv"]
    assert "--source" in call["argv"] and "coord-boss" in call["argv"]
    doc = json.loads(call["stdin"])
    assert doc == {"note": '{"v":1}',
                   "recorded_at": "2026-08-01T00:00:00+00:00"}


def test_record_write_omits_recorded_at_for_immediate_events(monkeypatch):
    calls = _capture_run(monkeypatch)
    t = transport_mod.FulcraFileTransport(command=["fulcra-api"], timeout=5)
    assert t.record_write("T", "v1alpha1", "{}", "s") is True
    assert "recorded_at" not in json.loads(calls[0]["stdin"])


def test_record_write_nonzero_rc_is_false(monkeypatch):
    _capture_run(monkeypatch, rc=1)
    t = transport_mod.FulcraFileTransport(command=["fulcra-api"], timeout=5)
    assert t.record_write("T", "v1alpha1", "{}", "s") is False


def test_record_write_spawn_failure_is_false_not_a_raise(monkeypatch):
    def boom(argv, timeout, *, stdin_data=None, **kw):
        raise OSError("no binary")

    monkeypatch.setattr(transport_mod, "run_bounded", boom)
    t = transport_mod.FulcraFileTransport(command=["fulcra-api"], timeout=5)
    assert t.record_write("T", "v1alpha1", "{}", "s") is False


# --- records.load_config ------------------------------------------------------

def test_config_env_override_wins(monkeypatch):
    monkeypatch.setenv(records.ENV_DATA_TYPE, "MomentAnnotation/env")
    cfg = records.load_config(FakeTransport(), "r")
    assert cfg == {"data_type": "MomentAnnotation/env",
                   "api_version": records.DEFAULT_API_VERSION}


def test_config_from_store(monkeypatch):
    t = FakeTransport()
    t.put(records.config_path("r"),
          '{"data_type": "MomentAnnotation/store", "api_version": "v1"}')
    assert records.load_config(t, "r") == {
        "data_type": "MomentAnnotation/store", "api_version": "v1"}


def _versioned_config(**overrides):
    doc = {
        "data_type": "MomentAnnotation/store",
        "api_version": "v1alpha1",
        "protocol_version": 1,
        "cursor_schema_version": 1,
        "minimum_reader_version": "1.8.0",
        "minimum_writer_version": "1.8.0",
        "cursor_generation": 0,
        "cursor_activated_at": None,
    }
    doc.update(overrides)
    return doc


def test_versioned_authority_parses_as_one_atomic_contract():
    t = FakeTransport()
    t.put(records.config_path("r"), json.dumps(_versioned_config()))
    cfg = records.load_config(t, "r")
    assert cfg["authority_mode"] == "versioned"
    assert cfg["cursor_schema_version"] == 1


def test_partial_versioned_authority_is_invalid_not_legacy():
    t = FakeTransport()
    t.put(records.config_path("r"), json.dumps({
        "data_type": "T", "protocol_version": 1}))
    assert records.load_config(t, "r") is None


def test_version_gate_refuses_old_or_unknown_writer():
    cfg = dict(_versioned_config(), authority_mode="versioned")
    old = records.compatibility(
        cfg, engine_version="1.7.2", write_cursor=True)
    assert old["ok"] is False and "below minimum_writer_version" in old["reason"]
    unknown = records.compatibility(
        cfg, engine_version="development", write_cursor=True)
    assert unknown["ok"] is False and "unknown engine/floor" in unknown["reason"]


def test_slice1_refuses_v2_activation_before_any_cursor_write():
    cfg = dict(_versioned_config(
        cursor_schema_version=2, cursor_generation=1,
        cursor_activated_at="2026-07-29T00:00:00Z"),
        authority_mode="versioned")
    gate = records.compatibility(
        cfg, engine_version="1.8.0", write_cursor=True)
    assert gate["ok"] is False
    assert "not safe to write" in gate["reason"]


def test_v2_cursor_is_physically_isolated_from_legacy_path():
    t = FakeTransport()
    v2 = records.v2_cursor_path("r", "amy", 7)
    t.put(v2, '{"v":2,"generation":7,"committed":"safe"}')
    # v1.7.2's complete public cursor-write operation targets the legacy path.
    assert records.save_cursor(
        t, "r", "amy", last_read="2026-07-29T00:00:00Z",
        seen_ids=["legacy"]) is True
    assert json.loads(t.read(v2)) == {
        "v": 2, "generation": 7, "committed": "safe"}
    assert records.cursor_path("r", "amy") != v2


def test_fleet_census_distinguishes_presence_from_adoption_claim():
    claim = {
        "id": "c1", "recorded_at": "2026-07-29T01:00:00Z",
        "sources": ["bob"],
        "note": records.build_payload(
            to="all", kind="claim", priority="P1", slug="adopt-latest",
            stamp={"engine_version": "1.8.0", "protocol_version": 1,
                   "cursor_schema_version": 1}),
    }
    census = records.fleet_version_census([
        {"agent": "amy", "timestamp": "2026-07-29T02:00:00Z",
         "engine": {"engine_version": "1.7.2", "protocol_version": None,
                    "cursor_schema_version": None}},
    ], [claim])
    assert [(row["agent"], row["running"], row["adopted"])
            for row in census["agents"]] == [
        ("amy", True, False), ("bob", False, True)]
    assert census["mixed"] is True


@pytest.mark.parametrize("raw", [
    None, "not json", "[]", "{}", '{"data_type": 3}', '{"data_type": " "}',
    '{"data_type": "T", "api_version": 7}',
])
def test_config_fail_closed_on_absent_or_malformed(raw):
    t = FakeTransport()
    if raw is not None:
        t.put(records.config_path("r"), raw)
    assert records.load_config(t, "r") is None


# --- remind emits the future-dated record ------------------------------------

class RecordingTransport(FakeTransport):
    def __init__(self, record_ok=True):
        super().__init__()
        self.record_ok = record_ok
        self.records_written: list[dict] = []

    def record_write(self, data_type, api_version, note, source,
                     recorded_at=None):
        self.records_written.append({
            "data_type": data_type, "api_version": api_version,
            "note": note, "source": source, "recorded_at": recorded_at})
        return self.record_ok

    def write(self, path, content):  # FakeTransport in test_reconcile has write?
        self.put(path, content)
        return True


def _pin_clock(monkeypatch):
    fixed = datetime(2026, 7, 27, 18, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(cli, "_now", lambda: fixed)
    return fixed


def test_remind_emits_future_dated_record_pointing_at_the_doc(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = RecordingTransport()
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    assert cli.main(["remind", "r", "amy", "2h", "Check the oven",
                     "--from", "boss"], transport=t) == 0
    out = capsys.readouterr().out
    (rec,) = t.records_written
    assert rec["data_type"] == "MomentAnnotation/x"
    assert rec["source"] == "boss"
    assert rec["recorded_at"] == "2026-07-27T20:00:00Z"
    payload = json.loads(rec["note"])
    assert payload["to"] == "amy"
    assert payload["kind"] == "directive"
    assert payload["ptr"] == f"task/{payload['slug']}.md"
    # the record points at the directive doc that actually landed
    assert f"team/r/task/{payload['slug']}.md" in t.store
    assert "record: scheduled, due 2026-07-27T20:00:00Z" in out


def test_remind_without_config_stays_file_plane_only(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = RecordingTransport()
    assert cli.main(["remind", "r", "amy", "2h", "No config"], transport=t) == 0
    assert t.records_written == []
    assert "file plane only" in capsys.readouterr().out


def test_repeated_identical_reminder_emits_no_second_conflicting_timer(
        monkeypatch, capsys):
    """WHEN is excluded from directive identity, so a re-remind dedupes onto
    the existing doc and KEEPS its original not_before. The timer record must
    follow the doc: exactly ONE record, at the ORIGINAL time — a second record
    at the new time would deliver the same directive twice with a timer that
    disagrees with the document it points at (the round-1 finding)."""
    _pin_clock(monkeypatch)
    t = RecordingTransport()
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    assert cli.main(["remind", "r", "amy", "2h", "Same", "--from", "boss"],
                    transport=t) == 0
    assert cli.main(["remind", "r", "amy", "3h", "Same", "--from", "boss"],
                    transport=t) == 0
    out = capsys.readouterr().out
    assert len(t.records_written) == 1
    assert t.records_written[0]["recorded_at"] == "2026-07-27T20:00:00Z"
    assert "already scheduled" in out
    assert "not_before 2026-07-27T20:00:00Z" in out


def test_remind_record_failure_degrades_loudly_but_rc_stays_zero(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = RecordingTransport(record_ok=False)
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    assert cli.main(["remind", "r", "amy", "2h", "Degraded"], transport=t) == 0
    assert len(t.records_written) == 1
    assert "emission failed" in capsys.readouterr().out


# --- the queue verb: cursored read (the window rule made automatic) ----------

class QueueTransport(RecordingTransport):
    def __init__(self, window=None, record_ok=True, write_ok=True):
        super().__init__(record_ok=record_ok)
        self.window = window          # what records() returns
        self.write_ok = write_ok
        self.record_queries: list[tuple] = []

    def records(self, data_type, since, until):
        self.record_queries.append((data_type, since, until))
        return self.window

    def write(self, path, content):
        if not self.write_ok and path.endswith("records-cursor.json"):
            return False
        self.put(path, content)
        return True


def _event_rec(rid, slug, to="amy", at="2026-07-27T17:00:00+00:00"):
    note = records.build_payload(to=to, kind="directive", priority="P2",
                                 slug=slug)
    return {"id": rid, "recorded_at": at, "sources": ["boss"], "note": note}


def test_queue_first_run_uses_default_lookback_and_saves_cursor(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    t = QueueTransport(window=[_event_rec("r1", "hello")])
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 0
    out = capsys.readouterr().out
    assert "hello" in out
    (query,) = t.record_queries
    assert query[1] == "2026-07-20T18:00:00Z"   # now - 7d lookback
    assert query[2] == "2026-07-27T18:00:00Z"
    cur = json.loads(t.store[records.cursor_path("r", "amy")])
    assert cur["last_read"] == "2026-07-27T18:00:00Z"
    assert cur["seen_ids"] == ["r1"]


def test_queue_version_floor_refuses_before_query_or_cursor_write(
        monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = QueueTransport(window=[])
    t.put(records.config_path("r"), json.dumps(_versioned_config(
        minimum_reader_version="9.0.0",
        minimum_writer_version="9.0.0")))
    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 3
    assert t.record_queries == []
    assert t.read(records.cursor_path("r", "amy")) is None
    assert "INCOMPATIBLE" in capsys.readouterr().err


def test_queue_legacy_authority_is_readable_but_loud(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = QueueTransport(window=[])
    t.put(records.config_path("r"), '{"data_type":"MomentAnnotation/x"}')
    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 0
    assert "VERSION WARNING" in capsys.readouterr().err


def test_queue_cursor_window_overlaps_by_skew_and_suppresses_seen(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = QueueTransport(window=[_event_rec("r1", "old-already-seen"),
                               _event_rec("r2", "fresh")])
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    t.put(records.cursor_path("r", "amy"),
          '{"v":1,"last_read":"2026-07-27T17:30:00Z","seen_ids":["r1"]}')
    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 0
    out = capsys.readouterr().out
    assert "fresh" in out and "old-already-seen" not in out
    (query,) = t.record_queries
    assert query[1] == "2026-07-27T17:28:00Z"   # last_read - 120s skew
    cur = json.loads(t.store[records.cursor_path("r", "amy")])
    assert set(cur["seen_ids"]) == {"r1", "r2"}


def test_queue_unknown_window_is_degraded_rc3_cursor_untouched(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = QueueTransport(window=None)
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    t.put(records.cursor_path("r", "amy"),
          '{"v":1,"last_read":"2026-07-27T17:30:00Z","seen_ids":[]}')
    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 3
    assert "DEGRADED" in capsys.readouterr().err
    cur = json.loads(t.store[records.cursor_path("r", "amy")])
    assert cur["last_read"] == "2026-07-27T17:30:00Z"  # unmoved


def test_queue_malformed_cursor_widens_to_lookback_not_shrinks(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = QueueTransport(window=[])
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    t.put(records.cursor_path("r", "amy"), "not json at all")
    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 0
    (query,) = t.record_queries
    assert query[1] == "2026-07-20T18:00:00Z"   # full lookback, never a guess


def test_queue_cursor_save_failure_warns_but_rc_zero(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = QueueTransport(window=[_event_rec("r1", "x")], write_ok=False)
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 0
    assert "cursor save failed" in capsys.readouterr().err


def test_queue_without_config_is_rc2(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = QueueTransport(window=[])
    assert cli.main(["queue", "r", "--agent", "amy"], transport=t) == 2
    assert "records config" in capsys.readouterr().err


def test_degraded_first_read_cannot_cause_a_second_timer(monkeypatch, capsys):
    """Round-2 finding: a pre-read returning None (absent OR degraded — the
    reader cannot tell) must not gate emission. Only the write path's verified
    'written' outcome may emit; a dedupe detected by _write_directive's OWN
    resolution emits nothing, even when an earlier read lied."""
    _pin_clock(monkeypatch)
    t = RecordingTransport()
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    assert cli.main(["remind", "r", "amy", "2h", "Same", "--from", "boss"],
                    transport=t) == 0
    assert len(t.records_written) == 1

    # Degrade exactly one subsequent read; _write_directive still detects the
    # dedupe via its own (later) read of the same path.
    real_read = t.read
    state = {"failed": False}

    def flaky_read(path):
        if not state["failed"] and path.startswith("team/r/task/"):
            state["failed"] = True
            return None
        return real_read(path)

    t.read = flaky_read
    # With the ambiguous pre-read REMOVED, the degraded read is now the write
    # path's own read — which fails LOUD (slot present but unreadable, retry)
    # instead of silently proceeding. rc 1, zero emission: the round-2 repro
    # is structurally impossible at this head.
    assert cli.main(["remind", "r", "amy", "3h", "Same", "--from", "boss"],
                    transport=t) == 1
    assert len(t.records_written) == 1, "degraded read must not add a timer"
    err = capsys.readouterr().err
    assert "cannot verify delivery, retry" in err
    # And a clean retry converges on the dedupe outcome with no second timer.
    assert cli.main(["remind", "r", "amy", "3h", "Same", "--from", "boss"],
                    transport=t) == 0
    assert len(t.records_written) == 1
    assert "already scheduled" in capsys.readouterr().out


# --- absent vs unreadable config (live incident 2026-07-28: expired-auth host
# --- reported "config missing" for hours; the queue must say DEGRADED instead)

class ClassifiedTransport(QueueTransport):
    """QueueTransport whose read path can simulate a transport outage."""

    def __init__(self, *a, read_status="ok", **kw):
        super().__init__(*a, **kw)
        self.read_status = read_status

    def read_classified(self, path):
        if self.read_status == "error":
            return None, "error"
        content = self.read(path)
        if content is None:
            return None, "absent"
        return content, "ok"


def _queue_args(team="fulcra", agent="amy"):
    import argparse
    return argparse.Namespace(team=team, agent=agent, json=False)


def test_load_config_classified_absent_vs_error():
    t = ClassifiedTransport(read_status="ok")
    cfg, status = records.load_config_classified(t, "fulcra")
    assert cfg is None and status == "absent"

    t = ClassifiedTransport(read_status="error")
    cfg, status = records.load_config_classified(t, "fulcra")
    assert cfg is None and status == "error"

    t = ClassifiedTransport(read_status="ok")
    t.put(records.config_path("fulcra"),
          '{"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}')
    cfg, status = records.load_config_classified(t, "fulcra")
    assert status == "ok" and cfg["data_type"] == "MomentAnnotation/x"


def test_load_config_classified_env_override_wins_even_during_outage(monkeypatch):
    monkeypatch.setenv(records.ENV_DATA_TYPE, "MomentAnnotation/env")
    t = ClassifiedTransport(read_status="error")
    cfg, status = records.load_config_classified(t, "fulcra")
    assert status == "ok" and cfg["data_type"] == "MomentAnnotation/env"


def test_load_config_classified_malformed_store_config_is_invalid_not_absent():
    # Bytes exist but the authority is unusable: fail closed as incompatible.
    t = ClassifiedTransport(read_status="ok")
    t.put(records.config_path("fulcra"), "not json at all")
    cfg, status = records.load_config_classified(t, "fulcra")
    assert cfg is None and status == "invalid"


def test_load_config_classified_falls_back_when_transport_lacks_classified_read():
    # Old transports without read_classified keep the legacy absent behavior.
    t = QueueTransport()
    cfg, status = records.load_config_classified(t, "fulcra")
    assert cfg is None and status == "absent"


def test_queue_unreadable_config_is_degraded_rc3_not_missing_rc2(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = ClassifiedTransport(read_status="error")
    rc = cli.cmd_queue(_queue_args(), t)
    assert rc == 3
    err = capsys.readouterr().err
    assert "DEGRADED" in err
    assert "missing" not in err.split("DEGRADED")[0]
    # No cursor may be created off an unknown window.
    assert t.read(records.cursor_path("fulcra", "amy")) is None


def test_queue_truly_absent_config_stays_rc2(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = ClassifiedTransport(read_status="ok")
    rc = cli.cmd_queue(_queue_args(), t)
    assert rc == 2
    assert "no bus-v3 records config" in capsys.readouterr().err


# --- consumption guard (live incident 2026-07-28: an operator diagnostic run
# --- as another agent's identity consumed that agent's pending directives)

def _guarded_queue_transport():
    t = ClassifiedTransport(read_status="ok",
                            window=[_event_rec("r1", "job-1")])
    t.put(records.config_path("fulcra"),
          '{"data_type": "MomentAnnotation/x", "api_version": "v1alpha1"}')
    return t


def test_queue_as_foreign_identity_peeks_and_never_advances(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", "operator")
    t = _guarded_queue_transport()
    rc = cli.cmd_queue(_queue_args(agent="amy"), t)
    out = capsys.readouterr()
    assert rc == 0
    assert "job-1" in out.out                       # events still shown
    assert "peek" in out.err and "--consume" in out.err
    assert t.read(records.cursor_path("fulcra", "amy")) is None  # not consumed


def test_queue_consume_flag_restores_deliberate_takeover(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", "operator")
    t = _guarded_queue_transport()
    args = _queue_args(agent="amy"); args.consume = True
    rc = cli.cmd_queue(args, t)
    assert rc == 0
    assert t.read(records.cursor_path("fulcra", "amy")) is not None


def test_queue_as_self_still_consumes(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", "amy")
    t = _guarded_queue_transport()
    rc = cli.cmd_queue(_queue_args(agent="amy"), t)
    assert rc == 0
    assert t.read(records.cursor_path("fulcra", "amy")) is not None


def test_queue_explicit_peek_as_self(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", "amy")
    t = _guarded_queue_transport()
    args = _queue_args(agent="amy"); args.peek = True
    rc = cli.cmd_queue(args, t)
    out = capsys.readouterr()
    assert rc == 0 and "job-1" in out.out
    assert t.read(records.cursor_path("fulcra", "amy")) is None


def test_read_classified_requires_exact_not_found_signature():
    # codex-reviewer acceptance test 4: an auth error CONTAINING "not found"
    # must remain a transport error, never classify as an absent file.
    import subprocess
    from coord_engine import transport as T

    class FakeCLI(T.FulcraFileTransport):
        def __init__(self, stderr):
            super().__init__(["true"])
            self._stderr = stderr

        def _run(self, args, **kw):
            return subprocess.CompletedProcess(args, 1, "", self._stderr)

    assert FakeCLI("Error: File not found in Fulcra: /x")\
        .read_classified("x")[1] == "absent"
    assert FakeCLI("Error: auth token not found; run fulcra auth login")\
        .read_classified("x")[1] == "error"
    assert FakeCLI("Error: connection reset")\
        .read_classified("x")[1] == "error"
