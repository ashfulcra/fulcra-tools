"""Tests for the Apple Music Activity (takeout) importer.

Covers the parsing, the four input shapes (CSV / dir / zip / nested zip),
the strict required-column error, and the shared since / until filters.
All fixtures are synthetic — no real takeout files are read.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fulcra_common.cross_source_fingerprint import listened_fingerprint
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


# --- Sibling-file fixtures for artist enrichment --------------------------

_DAILY_HEADER = "Date Played,Track Description,Play Duration Milliseconds"


def _daily_tracks_csv(descriptions: list[str]) -> bytes:
    rows = [f"20250615,{d},449000" for d in descriptions]
    return (_DAILY_HEADER + "\n" + "\n".join(rows) + "\n").encode()


def _library_tracks_zip(entries: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Apple Music Library Tracks.json", json.dumps(entries))
    return buf.getvalue()


def _takeout_zip(
    tmp_path: Path,
    *,
    activity_rows: list[str],
    daily_descriptions: list[str] | None = None,
    library_entries: list[dict] | None = None,
) -> Path:
    """Zip in Apple's real shape, optionally with the enrichment siblings."""
    prefix = "Apple_Media_Services/Apple Music Activity/"
    zpath = tmp_path / "takeout.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(
            prefix + "Apple Music Play Activity.csv",
            (_HEADER + "\n" + "\n".join(activity_rows) + "\n").encode(),
        )
        if daily_descriptions is not None:
            zf.writestr(
                prefix + "Apple Music - Play History Daily Tracks.csv",
                _daily_tracks_csv(daily_descriptions),
            )
        if library_entries is not None:
            zf.writestr(
                prefix + "Apple Music Library Tracks.json.zip",
                _library_tracks_zip(library_entries),
            )
    return zpath


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


# ---------------------------------------------------------------------------
# Blank-artist enrichment from sibling takeout files
# ---------------------------------------------------------------------------

def test_zip_enriches_blank_artist_from_daily_tracks(tmp_path):
    # Daily Tracks "Track Description" is "Artist - Title" (split on the
    # FIRST " - "); matching is lowercase + whitespace-collapsed, so the
    # casing/spacing noise here must still match "Take On Me".
    zpath = _takeout_zip(
        tmp_path,
        activity_rows=[
            _row(song="Take On Me", artist=""),
            # Title containing " - " itself: only the FIRST separator splits.
            _row(song="Take On Me - Live", artist="",
                 start="2025-06-15 22:00:00", end="2025-06-15 22:03:46",
                 duration_ms="226000"),
        ],
        daily_descriptions=["A-ha - take  on ME", "A-ha - Take On Me - Live"],
    )
    events = list(amt.parse_any(zpath))
    assert len(events) == 2
    by_title = {e.title: e for e in events}

    e = by_title["Take On Me"]
    assert e.external_ids["artist"] == "A-ha"
    assert e.note == "A-ha – Take On Me"
    assert e.external_ids["content_fingerprint"] == "music:a-ha:take-on-me"
    expected_cross = listened_fingerprint(
        timestamp=datetime(2025, 6, 15, 21, 0, tzinfo=timezone.utc),
        artist="A-ha", track="Take On Me",
    )
    assert e.extra_source_ids == (expected_cross,)

    live = by_title["Take On Me - Live"]
    assert live.external_ids["artist"] == "A-ha"


def test_daily_tracks_split_handles_artist_containing_separator(tmp_path):
    # A naive split on the first " - " would treat the artist as just
    # "A-ha" and the title as "Archive - Take On Me", then fail to enrich
    # the real "Take On Me" play. Use the Play Activity title set to choose
    # the only split whose title is actually present.
    zpath = _takeout_zip(
        tmp_path,
        activity_rows=[_row(song="Take On Me", artist="")],
        daily_descriptions=["A-ha - Archive - Take On Me"],
    )
    events = list(amt.parse_any(zpath))
    assert len(events) == 1
    assert events[0].external_ids["artist"] == "A-ha - Archive"
    assert events[0].note == "A-ha - Archive – Take On Me"


def test_zip_enrichment_uses_sibling_next_to_selected_activity_csv(tmp_path):
    # If a zip contains more than one Apple Music Activity folder, the lookup
    # must not borrow a same-basename sibling from a different folder.
    zpath = tmp_path / "multi.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(
            "A/Apple Music Play Activity.csv",
            (_HEADER + "\n" + _row(song="Take On Me", artist="") + "\n").encode(),
        )
        zf.writestr(
            "B/Apple Music - Play History Daily Tracks.csv",
            _daily_tracks_csv(["A-ha - Take On Me"]),
        )

    events = list(amt.parse_any(zpath))
    assert len(events) == 1
    assert events[0].external_ids["artist"] == ""


def test_zip_enriches_from_library_tracks_json_zip_only(tmp_path):
    zpath = _takeout_zip(
        tmp_path,
        activity_rows=[_row(song="Take On Me", artist="")],
        library_entries=[{"Title": "Take On Me", "Artist": "A-ha"}],
    )
    events = list(amt.parse_any(zpath))
    assert len(events) == 1
    assert events[0].external_ids["artist"] == "A-ha"
    assert events[0].note == "A-ha – Take On Me"


def test_ambiguous_title_across_sources_stays_blank(tmp_path):
    # Same normalized title maps to two distinct artists across the union
    # of both sources — must NOT guess.
    zpath = _takeout_zip(
        tmp_path,
        activity_rows=[_row(song="Same Title", artist="")],
        daily_descriptions=["Artist One - Same Title"],
        library_entries=[{"Title": "Same Title", "Artist": "Artist Two"}],
    )
    events = list(amt.parse_any(zpath))
    assert len(events) == 1
    assert events[0].external_ids["artist"] == ""
    assert events[0].note == " – Same Title"


def test_nonempty_artist_is_never_overwritten(tmp_path):
    zpath = _takeout_zip(
        tmp_path,
        activity_rows=[_row(song="Take On Me", artist="Original Artist")],
        daily_descriptions=["Other Artist - Take On Me"],
    )
    events = list(amt.parse_any(zpath))
    assert len(events) == 1
    assert events[0].external_ids["artist"] == "Original Artist"
    assert events[0].note == "Original Artist – Take On Me"


def test_enrichment_keeps_deterministic_id_stable(tmp_path):
    # REGRESSION: det_id must hash the RAW (pre-enrichment) artist. If
    # enrichment leaked into the hash, every previously-imported play
    # would change det_id and re-import as a duplicate on the next run.
    row = _row(song="Take On Me", artist="")

    bare_dir = tmp_path / "bare"
    bare_dir.mkdir()
    bare_csv = bare_dir / "Apple Music Play Activity.csv"
    _write_csv(bare_csv, [row])
    unenriched = list(amt.parse_any(bare_csv))

    zpath = _takeout_zip(
        tmp_path, activity_rows=[row],
        daily_descriptions=["A-ha - Take On Me"],
    )
    enriched = list(amt.parse_any(zpath))

    assert len(unenriched) == len(enriched) == 1
    # Enrichment actually happened on the zip side...
    assert unenriched[0].external_ids["artist"] == ""
    assert enriched[0].external_ids["artist"] == "A-ha"
    # ...but the deterministic_id is identical.
    assert enriched[0].deterministic_id == unenriched[0].deterministic_id


def test_bare_csv_without_siblings_skips_enrichment_gracefully(tmp_path):
    csv = tmp_path / "Apple Music Play Activity.csv"
    _write_csv(csv, [_row(song="Take On Me", artist="")])
    events = list(amt.parse_any(csv))
    assert len(events) == 1
    assert events[0].external_ids["artist"] == ""
    assert events[0].note == " – Take On Me"


def test_bare_csv_with_siblings_next_to_it_enriches(tmp_path):
    csv = tmp_path / "Apple Music Play Activity.csv"
    _write_csv(csv, [_row(song="Take On Me", artist="")])
    (tmp_path / "Apple Music - Play History Daily Tracks.csv").write_bytes(
        _daily_tracks_csv(["A-ha - Take On Me"])
    )
    (tmp_path / "Apple Music Library Tracks.json.zip").write_bytes(
        _library_tracks_zip([{"Title": "Take On Me", "Artist": "A-ha"}])
    )
    events = list(amt.parse_any(csv))
    assert len(events) == 1
    assert events[0].external_ids["artist"] == "A-ha"


def test_nested_zip_enriches_from_siblings(tmp_path):
    # Outer zip → Apple_Media_Services.zip → CSV + siblings.
    prefix = "Apple Music Activity/"
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w") as inner_zf:
        inner_zf.writestr(
            prefix + "Apple Music Play Activity.csv",
            (_HEADER + "\n" + _row(song="Take On Me", artist="") + "\n").encode(),
        )
        inner_zf.writestr(
            prefix + "Apple Music - Play History Daily Tracks.csv",
            _daily_tracks_csv(["A-ha - Take On Me"]),
        )
    outer = tmp_path / "Apple Takeout.zip"
    with zipfile.ZipFile(outer, "w") as outer_zf:
        outer_zf.writestr("Apple_Media_Services.zip", inner_buf.getvalue())
    events = list(amt.parse_any(outer))
    assert len(events) == 1
    assert events[0].external_ids["artist"] == "A-ha"
