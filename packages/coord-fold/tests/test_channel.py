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


def test_write_event_stdin_document_matches_the_old_transport_keys(monkeypatch):
    """GOLDEN: the key set is copied from coord_engine/transport.py record_write (~line 385 at 5db5c3e5).
    If the old transport's keys differ, change BOTH the writer and this set. Never guess."""
    seen = {}
    class R:
        returncode, stdout, stderr = 0, "", ""
    def fake_run(argv, input=None, **kw):
        seen["doc"] = json.loads(input)
        return R()
    monkeypatch.setattr(subprocess, "run", fake_run)
    CliPointerWriter(cli=["true"]).write_event({"data_type": "D", "api_version": "v1alpha1"},
        {"v": 1, "at": "T", "from": "a", "to": "b", "kind": "note", "slug": "s", "pri": "P3", "ptr": None}, sender="a")
    assert set(seen["doc"]) == {"data_type", "api_version", "note", "source", "recorded_at"}
