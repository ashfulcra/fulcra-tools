"""Tests for the current-shape (2026) Apple takeout video importer.

The legacy 12-column Playback Activity.csv path is covered by
`test_apple_takeout_importer.py` (which uses the in-tree legacy fixture).
This file covers the new code paths added when Apple changed the
takeout's structure:

  - the 126-column ``Video Play Activity.csv``
  - the 6-column current ``Playback Activity.csv``
  - directory / zip / nested-zip dispatch
  - the strict ``Required column missing`` error
  - the shared ``since`` / ``until`` window filters

All fixtures are synthetic (tmp_path) — no real takeout files are
read or required for these tests to pass.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fulcra_media.importers import apple_takeout as at


# ---------------------------------------------------------------------------
# Fixture helpers — synthesise the two CSV shapes Apple currently exports
# ---------------------------------------------------------------------------

# The real Video Play Activity.csv has ~126 columns. We include the 5
# required ones plus a couple of optional ones we read (Hardware Model,
# Content Episode Name, etc.) and two completely-irrelevant extras to
# verify the importer ignores them rather than choking.
_VIDEO_HEADER = (
    "Event Type,Content Title,Content Episode Name,Content Season Name,"
    "Episode Number,UTC Start Time,UTC End Time,Play Duration,"
    "Hardware Model,Subscription Channel,Store Front Name,"
    "Some Future Apple Column,Another Random Field"
)


def _video_row(
    *,
    event_type: str = "playActivity",
    title: str = "Severance",
    ep_name: str = "The We We Are",
    season: str = "Season 2",
    episode: str = "1",
    start: str = "2025-06-15 21:00:00",
    end: str = "2025-06-15 21:58:00",
    duration: str = "3480000",
    hardware: str = "Apple TV 4K (3rd generation)",
    channel: str = "Apple TV+",
    store: str = "US",
    extra1: str = "future-value",
    extra2: str = "noise",
) -> str:
    return ",".join([
        event_type, title, ep_name, season, episode, start, end,
        duration, hardware, channel, store, extra1, extra2,
    ])


def _write_video_csv(path: Path, rows: list[str]) -> None:
    path.write_text(_VIDEO_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


_PLAYBACK_HEADER = (
    "Item Reference,Item Description,Last activity timestamp,"
    "Playback position,Play count,Has been played?"
)


def _playback_row(
    *,
    ref: str = "ref-1",
    desc: str = "Dune: Part Two",
    ts: str = "2026-01-15 22:00:00",
    position: str = "5400",
    count: str = "1",
    played: str = "Yes",
) -> str:
    return ",".join([ref, desc, ts, position, count, played])


def _write_playback_csv(path: Path, rows: list[str]) -> None:
    path.write_text(
        _PLAYBACK_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Video Play Activity — happy path
# ---------------------------------------------------------------------------

def test_video_csv_extracts_play_end_rows_and_ignores_extras(tmp_path):
    csv = tmp_path / "Video Play Activity.csv"
    _write_video_csv(csv, [
        _video_row(start="2025-06-15 21:00:00", end="2025-06-15 21:58:00",
                   duration="3480000"),
        # A second row with different timestamps so dedup IDs differ.
        _video_row(title="The Holdovers", ep_name="", season="", episode="",
                   start="2025-06-16 20:00:00", end="2025-06-16 22:13:00",
                   duration="7980000"),
    ])
    events = list(at.parse_video_play_activity_csv(csv))
    assert len(events) == 2
    e = events[0]
    assert e.importer == "apple-takeout"
    assert e.service == "apple-tv"
    assert e.category == "watched"
    assert e.title == "Severance"
    # The "Some Future Apple Column" extras must not leak into external_ids;
    # we only carry the fields the importer knows about.
    assert "Some Future Apple Column" not in e.external_ids


def test_video_csv_drops_short_plays(tmp_path):
    csv = tmp_path / "Video Play Activity.csv"
    _write_video_csv(csv, [
        # 10-second tap (10,000 ms) — should be dropped.
        _video_row(start="2025-06-15 21:00:00", end="2025-06-15 21:00:10",
                   duration="10000"),
        # 35-second play (35,000 ms) — should survive.
        _video_row(start="2025-06-16 21:00:00", end="2025-06-16 21:00:35",
                   duration="35000"),
    ])
    events = list(at.parse_video_play_activity_csv(csv))
    assert len(events) == 1
    assert events[0].start_time == datetime(
        2025, 6, 16, 21, 0, 0, tzinfo=timezone.utc,
    )


def test_video_csv_filters_non_play_event_types(tmp_path):
    csv = tmp_path / "Video Play Activity.csv"
    _write_video_csv(csv, [
        _video_row(event_type="pauseActivity", duration="60000"),
        _video_row(event_type="seekActivity", duration="60000"),
        _video_row(event_type="playActivity", duration="60000"),
    ])
    events = list(at.parse_video_play_activity_csv(csv))
    assert len(events) == 1


# ---------------------------------------------------------------------------
# Playback Activity (current sparse shape) — fallback path
# ---------------------------------------------------------------------------

def test_playback_csv_fallback_one_event_per_row(tmp_path):
    csv = tmp_path / "Playback Activity.csv"
    _write_playback_csv(csv, [
        _playback_row(desc="Dune: Part Two", ts="2026-01-15 22:00:00",
                      position="5400"),
        _playback_row(desc="Severance", ts="2026-02-01 20:00:00",
                      position="3500"),
    ])
    events = list(at.parse_playback_activity_csv(csv))
    assert len(events) == 2
    descs = {e.title for e in events}
    assert descs == {"Dune: Part Two", "Severance"}
    # Confidence downgraded — these are "last activity" timestamps, not
    # actual playback windows.
    assert all(e.timestamp_confidence == "low" for e in events)


# ---------------------------------------------------------------------------
# Missing required column
# ---------------------------------------------------------------------------

def test_video_csv_missing_required_column_raises(tmp_path):
    # Drop "Content Title" — required for the rich video parser.
    bad_header = _VIDEO_HEADER.replace("Content Title,", "")
    csv = tmp_path / "broken.csv"
    csv.write_text(
        bad_header + "\nPLAY_END,Ep,Season 1,1,2025-06-15 21:00:00,"
        "2025-06-15 21:30:00,1800,Apple TV,Apple TV+,US,x,y\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Content Title"):
        list(at.parse_video_play_activity_csv(csv))


def test_playback_csv_missing_required_column_raises(tmp_path):
    bad_header = _PLAYBACK_HEADER.replace("Item Description,", "")
    csv = tmp_path / "broken.csv"
    csv.write_text(
        bad_header + "\nref,2026-01-15 22:00:00,5400,1,Yes\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Item Description"):
        list(at.parse_playback_activity_csv(csv))


# ---------------------------------------------------------------------------
# Path-shape dispatch — CSV, directory, zip, nested zip
# ---------------------------------------------------------------------------

def test_parse_any_with_csv_path(tmp_path):
    csv = tmp_path / "Video Play Activity.csv"
    _write_video_csv(csv, [_video_row(duration="3480000")])
    events = list(at.parse_any(csv))
    assert len(events) == 1


def test_parse_any_with_directory_prefers_video(tmp_path):
    (tmp_path / "nested").mkdir()
    video = tmp_path / "nested" / "Video Play Activity.csv"
    playback = tmp_path / "nested" / "Playback Activity.csv"
    _write_video_csv(video, [_video_row(duration="3480000")])
    _write_playback_csv(playback, [_playback_row()])
    events = list(at.parse_any(tmp_path))
    # Video Play Activity is preferred over Playback Activity when both
    # exist; the rich source wins.
    assert len(events) == 1
    assert events[0].timestamp_confidence == "high"


def test_parse_any_with_directory_falls_back_to_playback(tmp_path):
    (tmp_path / "deep" / "nested").mkdir(parents=True)
    playback = tmp_path / "deep" / "nested" / "Playback Activity.csv"
    _write_playback_csv(playback, [_playback_row()])
    events = list(at.parse_any(tmp_path))
    assert len(events) == 1
    assert events[0].timestamp_confidence == "low"


def test_parse_any_with_zip(tmp_path):
    csv_bytes = (_VIDEO_HEADER + "\n" + _video_row(duration="3480000") + "\n").encode()
    zpath = tmp_path / "takeout.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr(
            "Apple_Media_Services/Stores Activity/Other Activity/"
            "Video Play Activity.csv",
            csv_bytes,
        )
    events = list(at.parse_any(zpath))
    assert len(events) == 1


def test_parse_any_with_nested_zip(tmp_path):
    # Build the structure Apple really ships: outer zip contains
    # Apple_Media_Services.zip, which itself contains the CSV.
    csv_bytes = (_VIDEO_HEADER + "\n" + _video_row(duration="3480000") + "\n").encode()
    inner_buf = io.BytesIO()
    with zipfile.ZipFile(inner_buf, "w") as inner_zf:
        inner_zf.writestr(
            "Stores Activity/Other Activity/Video Play Activity.csv",
            csv_bytes,
        )
    outer = tmp_path / "Apple Takeout.zip"
    with zipfile.ZipFile(outer, "w") as outer_zf:
        outer_zf.writestr(
            "Apple_Media_Services.zip", inner_buf.getvalue(),
        )
    events = list(at.parse_any(outer))
    assert len(events) == 1
    assert events[0].title == "Severance"


# ---------------------------------------------------------------------------
# since / until filters
# ---------------------------------------------------------------------------

def _three_year_video_csv(path: Path) -> None:
    """Three rows spanning 2023 / 2024 / 2025."""
    _write_video_csv(path, [
        _video_row(title="OldShow", start="2023-06-15 21:00:00",
                   end="2023-06-15 21:58:00", duration="3480000"),
        _video_row(title="MidShow", start="2024-06-15 21:00:00",
                   end="2024-06-15 21:58:00", duration="3480000"),
        _video_row(title="NewShow", start="2025-06-15 21:00:00",
                   end="2025-06-15 21:58:00", duration="3480000"),
    ])


def test_since_filter_keeps_only_recent(tmp_path):
    csv = tmp_path / "v.csv"
    _three_year_video_csv(csv)
    # since = 2025-01-01 → only NewShow (2025-06-15)
    since = datetime(2025, 1, 1, tzinfo=timezone.utc)
    events = list(at.parse_video_play_activity_csv(csv, since=since))
    assert [e.title for e in events] == ["NewShow"]


def test_until_filter_keeps_only_older(tmp_path):
    csv = tmp_path / "v.csv"
    _three_year_video_csv(csv)
    # until = 2025-01-01 → OldShow + MidShow, but not NewShow
    until = datetime(2025, 1, 1, tzinfo=timezone.utc)
    events = list(at.parse_video_play_activity_csv(csv, until=until))
    assert sorted(e.title for e in events) == ["MidShow", "OldShow"]


def test_since_and_until_window_together(tmp_path):
    csv = tmp_path / "v.csv"
    _three_year_video_csv(csv)
    # Window = 2024-01-01 ≤ start < 2025-01-01 → MidShow only.
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    until = datetime(2025, 1, 1, tzinfo=timezone.utc)
    events = list(at.parse_video_play_activity_csv(
        csv, since=since, until=until,
    ))
    assert [e.title for e in events] == ["MidShow"]


def test_until_is_half_open_exclusive_at_boundary(tmp_path):
    # If a row's start_time exactly equals `until`, it's EXCLUDED. That
    # mirrors a typical "everything strictly before date X" intent.
    csv = tmp_path / "v.csv"
    boundary = "2025-06-15 21:00:00"
    _write_video_csv(csv, [_video_row(start=boundary,
                                      end="2025-06-15 21:58:00", duration="3480000")])
    until = datetime(2025, 6, 15, 21, 0, 0, tzinfo=timezone.utc)
    events = list(at.parse_video_play_activity_csv(csv, until=until))
    assert events == []


def test_window_propagates_through_parse_any(tmp_path):
    # Cover the dispatch wrapper too — make sure filters aren't dropped
    # somewhere between the entry point and the CSV iterator.
    csv = tmp_path / "Video Play Activity.csv"
    _three_year_video_csv(csv)
    since = datetime(2024, 1, 1, tzinfo=timezone.utc)
    until = datetime(2025, 1, 1, tzinfo=timezone.utc)
    events = list(at.parse_any(tmp_path, since=since, until=until))
    assert [e.title for e in events] == ["MidShow"]
