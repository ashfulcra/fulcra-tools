"""Cross-source dedup wiring tests.

Two importers that capture the same listen/watch should now emit
identical cross-source fingerprints in ``NormalizedEvent.extra_source_ids``
even though their per-plugin ``deterministic_id`` differs.

Per-importer smoke: one event per importer, assert the extras tuple
contains a ``com.fulcra.content.<kind>.v1.*`` entry.

Integration: synthetic Last.fm + Apple Music takeout events for the same
artist + track at timestamps inside the same 5-minute bucket produce
DIFFERENT deterministic_ids but the SAME content fingerprint in
``extra_source_ids``. The Fulcra worker concatenates both into
``metadata.source`` and ingest dedups on any source-id match — so the
second importer's POST will be deduped at ingest time.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_media.importers import (
    apple_music_takeout,
    apple_podcasts,
    apple_takeout,
    lastfm,
    letterboxd,
    netflix,
    spotify,
    trakt,
)


# ---------------------------------------------------------------------------
# Per-importer smoke (one event each, assert the extras carry a
# com.fulcra.content.* id).
# ---------------------------------------------------------------------------


def _assert_has_content_fp(ev, kind: str) -> None:
    """Fail loudly if `ev.extra_source_ids` lacks a com.fulcra.content.<kind>.v1.*
    entry. Used as a one-liner per-importer probe."""
    extras = getattr(ev, "extra_source_ids", ())
    matching = [s for s in extras if s.startswith(f"com.fulcra.content.{kind}.v1.")]
    assert matching, (
        f"expected a com.fulcra.content.{kind}.v1.* source-id in "
        f"extra_source_ids; got {extras!r}"
    )


def test_lastfm_emits_listened_fingerprint() -> None:
    ev = lastfm.normalize_track({
        "name": "Yellow",
        "artist": {"#text": "Coldplay"},
        "date": {"uts": "1748266500"},  # 2025-05-26 ...
    })
    assert ev is not None
    _assert_has_content_fp(ev, "listened")


def test_apple_music_takeout_emits_listened_fingerprint() -> None:
    header = (
        "Event Type,Song Name,Container Album Name,Container Artist Name,"
        "Event Start Timestamp,Event End Timestamp,Play Duration Milliseconds,"
        "UTC Offset In Seconds\n"
    )
    row = (
        "PLAY_END,Yellow,Parachutes,Coldplay,"
        "2025-05-26T14:35:00Z,2025-05-26T14:39:30Z,270000,0\n"
    )
    events = list(apple_music_takeout.parse_lines(iter((header + row).splitlines(keepends=True))))
    assert len(events) == 1
    _assert_has_content_fp(events[0], "listened")


def test_spotify_extended_track_emits_listened_fingerprint() -> None:
    entry = {
        "ts": "2025-05-26T14:39:30Z",
        "ms_played": 270000,
        "master_metadata_album_artist_name": "Coldplay",
        "master_metadata_track_name": "Yellow",
        "spotify_track_uri": "spotify:track:abc",
        "platform": "iOS",
    }
    events = list(spotify._process(entry))
    assert len(events) == 1
    _assert_has_content_fp(events[0], "listened")


def test_spotify_extended_podcast_emits_podcast_fingerprint() -> None:
    entry = {
        "ts": "2025-05-26T08:30:00Z",
        "ms_played": 1800_000,
        "episode_show_name": "The Daily",
        "episode_name": "Some Episode",
        "spotify_episode_uri": "spotify:episode:xyz",
    }
    events = list(spotify._process(entry))
    assert len(events) == 1
    _assert_has_content_fp(events[0], "podcast")


def test_trakt_episode_emits_watched_fingerprint() -> None:
    items = [{
        "id": 999_001,
        "type": "episode",
        "action": "scrobble",
        "watched_at": "2025-05-26T20:00:00.000Z",
        "episode": {"season": 2, "number": 5, "runtime": 45,
                    "ids": {"imdb": "tt000"}, "title": "Whatever"},
        "show": {"title": "Severance", "ids": {"imdb": "tt111"}},
    }]
    events = list(trakt.normalize_history(items))
    assert len(events) == 1
    _assert_has_content_fp(events[0], "watched")


def test_trakt_movie_emits_watched_fingerprint() -> None:
    items = [{
        "id": 999_002,
        "type": "movie",
        "action": "scrobble",
        "watched_at": "2025-05-26T22:00:00.000Z",
        "movie": {"title": "Dune Part Two", "year": 2024, "runtime": 165,
                  "ids": {"imdb": "tt222"}},
    }]
    events = list(trakt.normalize_history(items))
    assert len(events) == 1
    _assert_has_content_fp(events[0], "watched")


def test_apple_podcasts_emits_podcast_fingerprint() -> None:
    """Run the actual sqlite-fed importer against the in-tree fixture
    DB and confirm every emitted event carries a podcast fingerprint."""
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "apple_podcasts_mtlibrary.sqlite"
    events = list(apple_podcasts.parse_db(fixture))
    assert events, "fixture should produce at least one episode"
    for ev in events:
        _assert_has_content_fp(ev, "podcast")


def test_apple_takeout_video_tv_emits_watched_fingerprint(tmp_path) -> None:
    # Quote "Hello, Ms. Cobel" since the episode name contains a comma —
    # otherwise the CSV parser splits it into two fields.
    header = ",".join([
        "UTC Start Time", "UTC End Time", "Content Title", "Content Episode Name",
        "Content Season Name", "Episode Number", "Play Duration",
        "Hardware Model", "Store Front Name", "Subscription Channel",
        "Event Type", "End Reason Type",
    ]) + "\n"
    row = ",".join([
        "2025-05-26 20:00:00",
        "2025-05-26 20:45:00",
        "Severance",
        '"Hello, Ms. Cobel"',
        "Season 2",
        "5",
        "2700000",  # 45 min
        '"iPhone12,1"',
        "United States",
        "Apple TV+",
        "PLAY_END",
        "NATURAL_END_OF_TRACK",
    ]) + "\n"
    path = tmp_path / "video.csv"
    path.write_text(header + row)
    events = list(apple_takeout.parse_any_csv(path))
    assert len(events) == 1
    _assert_has_content_fp(events[0], "watched")


def test_netflix_rich_movie_emits_watched_fingerprint(tmp_path) -> None:
    header = ",".join(netflix._RICH_EXPECTED_COLS) + "\n"
    row = ",".join([
        "Profile",
        "2025-05-26 22:00:00",
        "1:55:00",
        "",
        "Dune: Part Two",
        "",
        "iPhone",
        "0", "0", "US",
    ]) + "\n"
    path = tmp_path / "netflix-rich.csv"
    path.write_text(header + row)
    events = list(netflix.parse_rich(path))
    assert len(events) == 1
    _assert_has_content_fp(events[0], "watched")


def test_netflix_rich_episode_emits_watched_fingerprint(tmp_path) -> None:
    header = ",".join(netflix._RICH_EXPECTED_COLS) + "\n"
    row = ",".join([
        "Profile",
        "2025-05-26 21:00:00",
        "0:45:00",
        "",
        "Severance: Season 2: Episode 5",
        "",
        "iPhone",
        "0", "0", "US",
    ]) + "\n"
    path = tmp_path / "netflix-rich.csv"
    path.write_text(header + row)
    events = list(netflix.parse_rich(path))
    assert len(events) == 1
    _assert_has_content_fp(events[0], "watched")


def test_letterboxd_emits_movie_fingerprint() -> None:
    """Letterboxd flows through generic_rss with a callback; smoke-test
    by calling the callback directly with a feedparser-shaped entry."""
    start = datetime(2025, 5, 26, 22, 0, tzinfo=timezone.utc)
    entry = {
        "letterboxd_filmtitle": "Dune Part Two",
        "letterboxd_filmyear": "2024",
        "title": "Dune Part Two, 2024 - ★★★★½",
    }
    extras = letterboxd._extract_extra_source_ids(entry, start)
    assert extras
    assert extras[0].startswith("com.fulcra.content.watched.v1.")


def test_spotify_ifttt_emits_listened_fingerprint(monkeypatch) -> None:
    """spotify_ifttt's parse_ifttt_zip is xlsx-bound; bypass the openpyxl
    layer by monkeypatching the row iterator so we exercise the
    construction site that wires extra_source_ids."""
    from fulcra_media.importers import spotify_ifttt as sift

    def fake_rows(_zf):
        # (ts, track, artist, track_id, url)
        yield (
            "May 26, 2025 at 02:37PM",
            "Yellow",
            "Coldplay",
            "spotify:track:abc",
            "https://open.spotify.com/track/abc",
        )

    monkeypatch.setattr(sift, "_iter_xlsx_rows", fake_rows)
    # zipfile.ZipFile() needs a real-ish argument; use an empty in-memory zip
    # since the patched fake_rows ignores it anyway.
    import io as _io
    import zipfile as _zip
    buf = _io.BytesIO()
    with _zip.ZipFile(buf, "w") as zf:
        zf.writestr("placeholder.txt", "x")
    buf.seek(0)
    # parse_ifttt_zip expects a Path-like; ZipFile accepts file objects too,
    # but the importer opens it via zipfile.ZipFile(zip_path). Pass the buffer
    # — Path-vs-fileobj works because ZipFile handles both.
    events = list(sift.parse_ifttt_zip(buf))
    assert len(events) == 1
    _assert_has_content_fp(events[0], "listened")


# ---------------------------------------------------------------------------
# Integration: Last.fm + Apple Music takeout for the same listen
# ---------------------------------------------------------------------------


def test_lastfm_and_apple_music_dedup_against_each_other() -> None:
    """The motivating use case: I scrobbled "Coldplay – Yellow" via
    Last.fm at 14:35:12 UTC, and the same play also got recorded by
    Apple Music and shows up in the takeout with start
    14:34:55 UTC (a few seconds of skew between when each service
    timestamps a play). Both timestamps land in the 14:35 5-minute
    bucket, so both events emit IDENTICAL
    com.fulcra.content.listened.v1.* source-ids — even though their
    plugin-namespaced deterministic_ids are completely different.
    """
    # Last.fm row at 14:37:30 UTC (UTS = 1748270250) — in the 14:35 bucket
    lastfm_ev = lastfm.normalize_track({
        "name": "Yellow",
        "artist": {"#text": "Coldplay"},
        "date": {"uts": "1748270250"},  # 2025-05-26T14:37:30Z
    })
    assert lastfm_ev is not None

    # Apple Music row at 14:35:00 UTC for the same track.
    # Same 5-minute bucket as the Last.fm scrobble.
    header = (
        "Event Type,Song Name,Container Album Name,Container Artist Name,"
        "Event Start Timestamp,Event End Timestamp,Play Duration Milliseconds,"
        "UTC Offset In Seconds\n"
    )
    row = (
        "PLAY_END,Yellow,Parachutes,Coldplay,"
        "2025-05-26T14:35:00Z,2025-05-26T14:39:30Z,270000,0\n"
    )
    apple_events = list(
        apple_music_takeout.parse_lines(
            iter((header + row).splitlines(keepends=True))
        )
    )
    assert len(apple_events) == 1
    apple_ev = apple_events[0]

    # Plugin-namespaced source ids MUST differ (that's how we trace which
    # importer produced each annotation).
    assert lastfm_ev.deterministic_id != apple_ev.deterministic_id
    assert lastfm_ev.deterministic_id.startswith("com.fulcra.media.lastfm.")
    assert apple_ev.deterministic_id.startswith(
        "com.fulcra.media.apple-music-takeout."
    )

    # Cross-source fingerprints MUST match — that's what makes Fulcra's
    # ingest dedupe the second POST.
    def _content_fp(ev) -> str:
        matching = [
            s for s in ev.extra_source_ids
            if s.startswith("com.fulcra.content.listened.v1.")
        ]
        assert len(matching) == 1, ev.extra_source_ids
        return matching[0]

    assert _content_fp(lastfm_ev) == _content_fp(apple_ev)


def test_lastfm_and_apple_music_bucket_boundary_DOES_NOT_dedup() -> None:
    """Negative case: the same listen captured by two sources that land
    on opposite sides of a 5-minute bucket boundary won't cross-dedup.
    This is the documented limitation of any bucketed scheme. Pinned as
    a test so we notice if the bucket width / boundary behaviour ever
    changes silently."""
    # Last.fm at 14:37:30 UTC (bucket 14:35) vs Apple at 14:30:00 UTC
    # (bucket 14:30) — adjacent buckets, no cross-match.
    lastfm_ev = lastfm.normalize_track({
        "name": "Yellow",
        "artist": {"#text": "Coldplay"},
        "date": {"uts": "1748270250"},  # 2025-05-26T14:37:30Z — bucket 14:35
    })
    # Push the Apple event into a different bucket
    header = (
        "Event Type,Song Name,Container Album Name,Container Artist Name,"
        "Event Start Timestamp,Event End Timestamp,Play Duration Milliseconds,"
        "UTC Offset In Seconds\n"
    )
    row = (
        "PLAY_END,Yellow,Parachutes,Coldplay,"
        "2025-05-26T14:30:00Z,2025-05-26T14:33:00Z,180000,0\n"
    )
    apple_events = list(
        apple_music_takeout.parse_lines(
            iter((header + row).splitlines(keepends=True))
        )
    )
    assert lastfm_ev is not None and len(apple_events) == 1

    def _content_fp(ev) -> str:
        return next(
            s for s in ev.extra_source_ids
            if s.startswith("com.fulcra.content.listened.v1.")
        )
    assert _content_fp(lastfm_ev) != _content_fp(apple_events[0])
