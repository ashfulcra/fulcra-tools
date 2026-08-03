"""Round-trip delivery proof for the typed-record control plane."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from coord_engine import cli, records
from test_reconcile import FakeTransport


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def test_probe_payload_roundtrips_and_stamps():
    raw = records.roundtrip_probe_payload("coord-boss", nonce="abc123")
    parsed = records.parse_payload(raw)
    assert parsed is not None, "probe payload must be a parseable v1 event"
    assert parsed["to"] == "coord-boss-probe"
    assert parsed["slug"] == "delivery-probe-abc123"
    assert parsed["kind"] == "claim"
    stamp = parsed["writer"]
    from coord_engine import __version__
    assert stamp and stamp["engine_version"] == __version__


def test_prose_note_fails_the_parse_leg():
    legacy = "create: REVIEW REQUEST: pr-504-third · assignee: codex-coder"
    assert records.parse_payload(legacy) is None


class _DeliveryTransport(FakeTransport):
    """Write-through fake: records written land in the window immediately."""

    def __init__(self, *, drop_writes: bool = False, mangle: bool = False):
        super().__init__()
        self.window: list[dict] = []
        self.drop_writes = drop_writes
        self.mangle = mangle

    def record_write(self, data_type, api_version, note, source,
                     recorded_at=None):
        if self.drop_writes:
            return False
        raw = (f"create: legacy prose {json.loads(note)['slug']}"
               if self.mangle else note)
        self.window.append({
            "id": f"r{len(self.window)}",
            "recorded_at": "2026-08-01T12:00:00Z",
            "note": raw,
            "sources": [source],
        })
        return True

    def records(self, data_type, since, until):
        return list(self.window)


def _delivery_setup(monkeypatch, *, drop_writes=False, mangle=False):
    t = _DeliveryTransport(drop_writes=drop_writes, mangle=mangle)
    t.put(records.config_path("r"), json.dumps({
        "data_type": "MomentAnnotation/x",
        "api_version": "v1alpha1",
    }))
    monkeypatch.setattr(cli, "_now", lambda: NOW)
    return t


def test_delivery_probe_roundtrip_ok(capsys, monkeypatch):
    t = _delivery_setup(monkeypatch)
    rc = cli.main(["doctor", "r", "--delivery", "--agent", "coord-boss"],
                  transport=t)
    out = capsys.readouterr()
    assert rc == 0
    assert "delivery: PROVEN" in out.out


def test_delivery_probe_detects_legacy_writer(capsys, monkeypatch):
    t = _delivery_setup(monkeypatch, mangle=True)
    rc = cli.main(["doctor", "r", "--delivery", "--agent", "coord-boss"],
                  transport=t)
    out = capsys.readouterr()
    assert rc == 3
    assert "NOT readable" in out.out + out.err


def test_delivery_probe_write_refused(capsys, monkeypatch):
    t = _delivery_setup(monkeypatch, drop_writes=True)
    rc = cli.main(["doctor", "r", "--delivery", "--agent", "coord-boss"],
                  transport=t)
    out = capsys.readouterr()
    assert rc == 2
    assert "REFUSED" in out.err
