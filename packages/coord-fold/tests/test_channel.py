import json
import subprocess
import pytest
from coord_fold import channel
from coord_fold.transport import CliPointerWriter
from coord_fold_fakes import FakeReader, FakeStore

CFG = "team/r/_coord/bus-v4/records.json"


def test_resolves_data_type_from_the_config_document():
    st = FakeStore({CFG: json.dumps({"data_type": "MomentAnnotation/abc", "api_version": "v1alpha1"})}, [])
    assert channel.resolve(FakeReader(st), "r")["data_type"] == "MomentAnnotation/abc"


def test_absent_and_unreadable_config_both_raise_with_different_words():
    with pytest.raises(channel.ChannelUnresolved, match="absent"):
        channel.resolve(FakeReader(FakeStore({}, [])), "r")
    st = FakeStore({}, []); st.fail_reads = True
    with pytest.raises(channel.ChannelUnresolved, match="error"):
        channel.resolve(FakeReader(st), "r")


def test_config_missing_data_type_raises():
    with pytest.raises(channel.ChannelUnresolved):
        channel.resolve(FakeReader(FakeStore({CFG: json.dumps({"api_version": "v1alpha1"})}, [])), "r")


def test_write_event_matches_the_old_transport_s_record_invocation(monkeypatch):
    """GOLDEN: copied from coord_engine/transport.py record_write (line 414 at 631ba497), which is what the real CLI
    accepts: data type, api version and source travel as ARGUMENTS; only `note` and `recorded_at` travel on stdin.
    The earlier version of this test asserted five stdin keys that were never read from the engine — a guess that
    the proof's permissive fake confirmed and the live store refused (rc 2, 2026-09-05). Never guess."""
    seen = {}
    class R:
        returncode, stdout, stderr = 0, "", ""
    def fake_run(argv, input=None, **kw):
        seen["argv"], seen["doc"] = argv, json.loads(input)
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    CliPointerWriter(cli=["true"]).write_event({"data_type": "D", "api_version": "v1alpha1"},
        {"v": 1, "at": "T", "from": "a", "to": "b", "kind": "note", "slug": "s", "pri": "P3", "ptr": None}, sender="a")
    assert seen["argv"] == ["true", "record", "D", "--api-version", "v1alpha1", "--source", "a"]
    assert set(seen["doc"]) == {"note", "recorded_at"}
