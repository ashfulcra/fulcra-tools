"""A hand-sent directive tracks no obligation - say so at send time."""
from __future__ import annotations
import json
from coord_engine import cli
from coord_engine_test_helpers import FakeTransport

TEAM = "r"

class _RecordingTransport(FakeTransport):
    def record_write(self, data_type, api_version, note, source,
                     recorded_at=None, tags=None):
        return True

def _send(monkeypatch, capsys, kind, *, records_cfg=True):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "alice")
    t = _RecordingTransport()
    if records_cfg:
        t.put(f"team/{TEAM}/_coord/bus-v3/records.json",
              json.dumps({"data_type": "X/1", "api_version": "v1alpha1"}))
    rc = cli.main(["bus-v3", "send", TEAM, "--to", "bob", "--kind", kind,
                   "--ptr", "p.md", "--slug", "s"], transport=t)
    return rc, capsys.readouterr()

def test_a_hand_sent_DIRECTIVE_says_it_tracks_no_obligation(monkeypatch, capsys):
    _rc, cap = _send(monkeypatch, capsys, "directive")
    assert "NO task row" in cap.err
    assert "needs-me" in cap.err and "tell" in cap.err

def test_the_note_does_not_REFUSE_the_send(monkeypatch, capsys):
    rc, _ = _send(monkeypatch, capsys, "directive")
    assert rc == 0, f"the warning turned into a refusal: rc={rc}"

def test_OTHER_kinds_are_not_warned_about(monkeypatch, capsys):
    for kind in ("response", "verdict", "claim"):
        _rc, cap = _send(monkeypatch, capsys, kind)
        assert "NO task row" not in cap.err

def test_the_note_precedes_the_config_failure_path(monkeypatch, capsys):
    rc, cap = _send(monkeypatch, capsys, "directive", records_cfg=False)
    assert rc == 2
    assert "NO task row" in cap.err, (
        "the verb note was swallowed by the config failure")
