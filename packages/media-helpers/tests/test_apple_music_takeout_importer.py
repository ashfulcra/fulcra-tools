"""Tests for the Apple Music Activity (takeout) importer.

Covers the parsing, the four input shapes (CSV / dir / zip / nested zip),
the strict required-column error, and the shared since / until filters.
All fixtures are synthetic — no real takeout files are read.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fulcra_media.importers import apple_music_takeout as amt


# The live Apple Music Play Activity.csv has ~144 columns. We carry the 5
# required ones plus several optional/filtered fields and two completely
# irrelevant extras to confirm they're ignored.
_HEADER = (
    "Song Name,Container Album Name,Container Artist Name,"
    "Event Start Timestamp,Event End Timestamp,"
    "Play Duration Milliseconds,UTC Offset In Seconds,"
    "Event Type,End Reason Type,"
    "Some Future Apple Column,Another Random Field"
)


def _row(
    *,
    song: str = "Blue Monday",
    album: str = "Substance",  # avoid commas in the album name — naive
                               # comma-split would break the CSV row
    artist: str = "New Order",
    start: str = "2025-06-15 21:00:00",
    end: str = "2025-06-15 21:07:29",
    duration_ms: str = "449000",  # 7m29s
    offset: str = "-25200",
    event_type: str = "PLAY_END",
    end_reason: str = "NATURAL_END_OF_TRACK",
    extra1: str = "future-value",
    extra2: str = "noise",
) -> str:
    return ",".join([
        song, album, artist, start, end, duration_ms, offset,
        event_type, end_reason, extra1, extra2,
    ])


def _write_csv(path: Path, rows: list[str]) -> None:
    path.write_text(_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Happy-path parsing
# ---------------------------------------------------------------------------

def test_csv_extracts_natural_end_rows_and_ignores_extras(tmp_path):
    csv = tmp_path / "Apple Music Play Activity.csv"
    _write_csv(csv, [
        _row(song="Blue Monday", artist="New Order"),
        _row(song="Bizarre Love Triangle", artist="New Order",
             start="2025-06-15 21:08:00", end="2025-06-15 21:12:23"),
    ])
    events = list(amt.parse_csv(csv))
    assert len(events) == 2
    e = events[0]
    assert e.importer == "apple-music-takeout"
    assert e.service == "apple-music"
    assert e.category == "listened"
    assert e.title == "Blue Monday"
    assert e.note == "New Order – Blue Monday"
    assert e.external_ids["artist"] == "New Order"
    assert e.external_ids["album"] == "Substance"
    assert e.external_ids["content_fingerprint"] == "music:new-order:blue-monday"
    assert "Some Future Apple Column" not in e.external_ids


def test_csv_drops_skipped_tracks(tmp_path):
    csv = tmp_path / "Apple Music Play Activity.csv"
    _write_csv(csv, [
        # Short skip (5 seconds / 5000 ms) — dropped by duration check.
        _row(song="Skipped", duration_ms="5000", end_reason="TRACK_SKIPPED_FORWARDS"),
        # Long skip (60 seconds / 60000 ms) — passes duration check and survives.
        _row(song="Mostly Listened", duration_ms="60000", end_reason="TRACK_SKIPPED_FORWARDS"),
        # Natural end of track — survives.
        _row(song="Complete", duration_ms="300000", end_reason="NATURAL_END_OF_TRACK"),
    ])
    events = list(amt.parse_csv(csv))
    titles = sorted(e.title for e in events)
    assert titles == ["Complete", "Mostly Listened"]


def test_csv_drops_short_plays(tmp_path):
    csv = tmp_path / "Apple Music Play Activity.csv"
    _write_csv(csv, [
        # 5-second tap-and-skip — under 30s threshold.
        _row(song="Skipped", duration_ms="5000"),
        _row(song="Played", duration_ms="180000"),
    ])
    events = list(amt.parse_csv(csv))
    assert [e.title for e in events] == ["Played"]


def test_csv_filters_non_play_event_types(tmp_path):
    csv = tmp_path / "Apple Music Play Activity.csv"
    _write_csv(csv, [
        _row(song="BadType", event_type="LYRIC_DISPLAY"),
        _row(song="Good", event_type="PLAY_END"),
    ])
    events = list(amt.parse_csv(csv))
    assert [e.title for e in events] == ["Good"]


def test_csv_requires_song_only_artist_optional(tmp_path):
    csv = tmp_path / "Apple Music Play Activity.csv"
    _write_csv(csv, [
        # No song — dropped.
        _row(song="", artist="Some Artist"),
        # Song but no artist — now passes (artist is optional).
        _row(song="Song Without Artist", artist=""),
        # Both song and artist — passes.
        _row(song="Complete Track", artist="Known Artist"),
    ])
    events = list(amt.parse_csv(csv))
    titles = sorted(e.title for e in events)
    assert titles == ["Complete Track", "Song Without Artist"]


# ---------------------------------------------------------------------------
# Missing required column
# ---------------------------------------------------------------------------

def test_csv_missing_required_column_raises(tmp_path):
    bad_header = _HEADER.replace("Song Name,", "")
    csv = tmp_path / "broken.csv"
    csv.write_text(
        bad_header + "\nAlbum,Artist,2025-06-15 21:00:00,"
        "2025-06-15 21:05:00,300000,-25200,PLAY_END,"
        "NATURAL_END_OF_TRACK,x,y\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Song Name"):
        list(amt.parse_csv(csv))


# ---------------------------------------------------------------------------
# Path-shape dispatch
# ---------------------------------------------------------------------------

def test_parse_any_with_csv_path(tmp_path):
    csv = tmp_path / "Apple Music Play Activity.csv"
    _write_csv(csv, [_row()])
    events = list(amt.parse_any(csv))
    assert len(events) == 1


def test_parse_any_with_directory(tmp_path):
    (tmp_path / "Apple_Media_Services" / "Apple Music Activity").mkdir(parents=True)
    csv = tmp_path / "Apple_Media_Services" / "Apple Music Activity" / \
        "Apple Music Play Activity.csv"
    _write_csv(csv, [_row()])
    events = list(amt.parse_any(tmp_path))
    assert len(events) == 1


def test_parse_any_with_zip(tmp_path):
    csv_bytes = (_HEADER + "\n" + _row() + "\n").encode()
    zpath = tmp_path / "takeout.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(
            "Apple_Media_Services/Apple Music Activity/"
            "Apple Music Play Activity.csv",
            csv_bytes,
        )
    events = list(amt.parse_any(zpath))
    assert len(events) == 1


def test_parse_any_with_nested_zip(tmp_path):
    # Apple's real shape: outer zip → Apple_Media_Services.zip → CSV
    csv_bytes = (_HEADER + "\n" + _row() + "\n").encode()
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w") as inner_zf:
        inner_zf.writestr(
            "Apple Music Activity/Apple Music Play Activity.csv",
            csv_bytes,
        )
    outer = tmp_path / "Apple Takeout.zip"
    with zipfile.ZipFile(outer, "w") as outer_zf:
        outer_zf.writestr(
            "Apple_Media_Services.zip", inner_buf.getvalue(),
        )
    events = list(amt.parse_any(outer))
    assert len(events) == 1


# ---------------------------------------------------------------------------
# since / until filters
# ---------------------------------------------------------------------------

def _three_year_csv(path: Path) -> None:
    _write_csv(path, [
        _row(song="Old", start="2023-06-15 21:00:00",
             end="2023-06-15 21:04:00"),
        _row(song="Mid", start="2024-06-15 21:00:00",
             end="2024-06-15 21:04:00"),
        _row(song="New", start="2025-06-15 21:00:00",
             end="2025-06-15 21:04:00"),
    ])


def test_since_filter_keeps_only_recent(tmp_path):
    csv = tmp_path / "v.csv"
    _three_year_csv(csv)
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    events = list(amt.parse_csv(csv, since=since))
    assert [e.title for e in events] == ["New"]


def test_until_filter_keeps_only_older(tmp_path):
    csv = tmp_path / "v.csv"
    _three_year_csv(csv)
    until = datetime(2025, 1, 1, tzinfo=timezone.utc)
    events = list(amt.parse_csv(csv, until=until))
    assert sorted(e.title for e in events) == ["Mid", "Old"]


def test_since_and_until_window(tmp_path):
    csv = tmp_path / "v.csv"
    _three_year_csv(csv)
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    until = datetime(2025, 1, 1, tzinfo=timezone.utc)
    events = list(amt.parse_csv(csv, since=since, until=until))
    assert [e.title for e in events] == ["Mid"]


def test_window_propagates_through_parse_any(tmp_path):
    csv = tmp_path / "Apple Music Play Activity.csv"
    _three_year_csv(csv)
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    until = datetime(2025, 1, 1, tzinfo=timezone.utc)
    events = list(amt.parse_any(tmp_path, since=since, until=until))
    assert [e.title for e in events] == ["Mid"]
