"""Tests for watermark management."""
from datetime import datetime, timezone


from fulcra_media import watermarks
from fulcra_media.state import State


def test_get_returns_none_when_unset():
    s = State()
    assert watermarks.get(s, "lastfm") is None


def test_set_and_get_roundtrip():
    s = State()
    watermarks.set_(s, "lastfm", "2026-05-17T08:42:00+00:00")
    assert watermarks.get(s, "lastfm") == "2026-05-17T08:42:00+00:00"


def test_set_iso_and_get_iso_roundtrip():
    s = State()
    dt = datetime(2026, 5, 17, 8, 42, tzinfo=timezone.utc)
    watermarks.set_iso(s, "lastfm", dt)
    assert watermarks.get_iso(s, "lastfm") == dt


def test_get_iso_returns_none_when_unset():
    s = State()
    assert watermarks.get_iso(s, "lastfm") is None


def test_get_iso_returns_none_when_invalid_iso():
    s = State(watermarks={"x": "not a date"})
    assert watermarks.get_iso(s, "x") is None


def test_get_iso_parses_z_suffix():
    """Fulcra timestamps come back with Z; should parse cleanly."""
    s = State(watermarks={"lastfm": "2026-05-17T08:42:00Z"})
    dt = watermarks.get_iso(s, "lastfm")
    assert dt == datetime(2026, 5, 17, 8, 42, tzinfo=timezone.utc)


def test_get_iso_naive_treated_as_utc():
    """If a naive timestamp slipped in, treat it as UTC rather than crash."""
    s = State(watermarks={"x": "2026-05-17T08:42:00"})
    dt = watermarks.get_iso(s, "x")
    assert dt == datetime(2026, 5, 17, 8, 42, tzinfo=timezone.utc)


def test_set_snapshot_and_get_snapshot_roundtrip():
    """Snapshot importers store {sha256, path, mtime} as JSON-encoded string."""
    s = State()
    snap = {"sha256": "abc123", "path": "/tmp/x.sqlite", "mtime": 1700000000.0}
    watermarks.set_snapshot(s, "apple-podcasts", snap)
    assert watermarks.get_snapshot(s, "apple-podcasts") == snap


def test_get_snapshot_returns_none_for_invalid_json():
    s = State(watermarks={"x": "not json"})
    assert watermarks.get_snapshot(s, "x") is None


def test_get_snapshot_returns_none_when_unset():
    s = State()
    assert watermarks.get_snapshot(s, "x") is None
