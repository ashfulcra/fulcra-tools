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


def test_remind_record_failure_degrades_loudly_but_rc_stays_zero(monkeypatch, capsys):
    _pin_clock(monkeypatch)
    t = RecordingTransport(record_ok=False)
    t.put(records.config_path("r"), '{"data_type": "MomentAnnotation/x"}')
    assert cli.main(["remind", "r", "amy", "2h", "Degraded"], transport=t) == 0
    assert len(t.records_written) == 1
    assert "emission failed" in capsys.readouterr().out
