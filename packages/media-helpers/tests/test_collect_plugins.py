"""The Last.fm and file-based fulcra-collect plugins."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_media.collect_plugins import (
    LASTFM_PLUGIN,
    NETFLIX_PLUGIN,
    SPOTIFY_EXTENDED_PLUGIN,
    YOUTUBE_PLUGIN,
    SPOTIFY_IFTTT_PLUGIN,
    APPLE_TAKEOUT_PLUGIN,
    GENERIC_RSS_PLUGIN,
    LETTERBOXD_PLUGIN,
    GOODREADS_PLUGIN,
    DEEZER_PLUGIN,
    TRAKT_PLUGIN,
    APPLE_PODCASTS_PLUGIN,
    APPLE_PODCASTS_TIMEMACHINE_PLUGIN,
)


def test_lastfm_plugin_metadata_is_scheduled():
    assert LASTFM_PLUGIN.id == "lastfm"
    assert LASTFM_PLUGIN.kind == "scheduled"
    assert LASTFM_PLUGIN.default_interval is not None
    assert {c.key for c in LASTFM_PLUGIN.required_credentials} == {"api-key"}


def test_run_fetches_normalizes_imports_and_advances_watermark(monkeypatch):
    calls = {}

    monkeypatch.setattr("fulcra_media.collect_plugins.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.collect_plugins.normalize_history",
                        lambda raw: ["event-1"])

    class FakeResult:
        posted = 1
        skipped_existing = 0
        verified = 1

    class FakeClient:
        def ensure_tag(self, name, state):
            calls["ensure_tag"] = name
        def run_import(self, events, state, check_only=False):
            calls["imported"] = list(events)
            return FakeResult()

    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient",
                        lambda: FakeClient())
    monkeypatch.setattr("fulcra_media.collect_plugins.newest_event_iso",
                        lambda events: "2026-05-22T12:00:00Z")

    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm", config={}, credentials={"api-key": "K"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    assert calls["imported"] == ["event-1"]
    assert calls["ensure_tag"] == "lastfm"
    assert st.watermark == "2026-05-22T12:00:00Z"


def test_run_raises_a_clear_error_when_the_api_key_is_missing(monkeypatch):
    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm", config={}, credentials={},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    with pytest.raises(RuntimeError, match="api-key"):
        LASTFM_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Helpers shared by file-plugin tests
# ---------------------------------------------------------------------------

def _make_ctx(plugin_id: str, config: dict) -> tuple[RunContext, PluginState]:
    st = PluginState(plugin_id)
    ctx = RunContext(
        plugin_id=plugin_id,
        config=config,
        credentials={},
        state=st,
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    return ctx, st


class _FakeResult:
    posted = 2
    skipped_existing = 1
    verified = 2


class _FakeClient:
    def __init__(self):
        self.calls = {}

    def ensure_tag(self, name, state):
        self.calls["ensure_tag"] = name

    def run_import(self, events, state, check_only=False):
        self.calls["imported"] = list(events)
        return _FakeResult()


# ---------------------------------------------------------------------------
# Netflix plugin
# ---------------------------------------------------------------------------

def test_netflix_plugin_metadata():
    assert NETFLIX_PLUGIN.id == "netflix"
    assert NETFLIX_PLUGIN.kind == "manual"
    assert NETFLIX_PLUGIN.default_interval is None
    assert not NETFLIX_PLUGIN.required_credentials


def test_netflix_plugin_run(monkeypatch, tmp_path):
    fake_csv = tmp_path / "viewing.csv"
    fake_csv.write_text("Title,Date\n")

    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.collect_plugins.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.collect_plugins.netflix_importer.parse_auto",
                        lambda path: ["ev-netflix"])
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient",
                        lambda: fake_client)

    ctx, _ = _make_ctx("netflix", {"path": str(fake_csv)})
    NETFLIX_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-netflix"]
    assert fake_client.calls["ensure_tag"] == "netflix"


def test_netflix_plugin_raises_without_path():
    ctx, _ = _make_ctx("netflix", {})
    with pytest.raises(RuntimeError, match="path"):
        NETFLIX_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Spotify Extended plugin
# ---------------------------------------------------------------------------

def test_spotify_extended_plugin_metadata():
    assert SPOTIFY_EXTENDED_PLUGIN.id == "spotify-extended"
    assert SPOTIFY_EXTENDED_PLUGIN.kind == "manual"
    assert SPOTIFY_EXTENDED_PLUGIN.default_interval is None
    assert not SPOTIFY_EXTENDED_PLUGIN.required_credentials


def test_spotify_extended_plugin_run(monkeypatch, tmp_path):
    fake_zip = tmp_path / "spotify.zip"
    fake_zip.write_bytes(b"PK")

    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.collect_plugins.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.collect_plugins.spotify_importer.parse_extended_zip",
                        lambda path: ["ev-spotify"])
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient",
                        lambda: fake_client)

    ctx, _ = _make_ctx("spotify-extended", {"path": str(fake_zip)})
    SPOTIFY_EXTENDED_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-spotify"]
    assert fake_client.calls["ensure_tag"] == "spotify"


def test_spotify_extended_plugin_raises_without_path():
    ctx, _ = _make_ctx("spotify-extended", {})
    with pytest.raises(RuntimeError, match="path"):
        SPOTIFY_EXTENDED_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# YouTube plugin
# ---------------------------------------------------------------------------

def test_youtube_plugin_metadata():
    assert YOUTUBE_PLUGIN.id == "youtube"
    assert YOUTUBE_PLUGIN.kind == "manual"
    assert YOUTUBE_PLUGIN.default_interval is None
    assert not YOUTUBE_PLUGIN.required_credentials


def test_youtube_plugin_run(monkeypatch, tmp_path):
    fake_json = tmp_path / "watch-history.json"
    fake_json.write_text("[]")

    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.collect_plugins.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.collect_plugins.youtube_importer.parse_takeout_json",
                        lambda path: ["ev-youtube"])
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient",
                        lambda: fake_client)

    ctx, _ = _make_ctx("youtube", {"path": str(fake_json)})
    YOUTUBE_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-youtube"]
    assert fake_client.calls["ensure_tag"] == "youtube"


def test_youtube_plugin_raises_without_path():
    ctx, _ = _make_ctx("youtube", {})
    with pytest.raises(RuntimeError, match="path"):
        YOUTUBE_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Spotify IFTTT plugin
# ---------------------------------------------------------------------------

def test_spotify_ifttt_plugin_metadata():
    assert SPOTIFY_IFTTT_PLUGIN.id == "spotify-ifttt"
    assert SPOTIFY_IFTTT_PLUGIN.kind == "manual"
    assert SPOTIFY_IFTTT_PLUGIN.default_interval is None
    assert not SPOTIFY_IFTTT_PLUGIN.required_credentials


def test_spotify_ifttt_plugin_run(monkeypatch, tmp_path):
    fake_zip = tmp_path / "ifttt.zip"
    fake_zip.write_bytes(b"PK")

    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.collect_plugins.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.collect_plugins.spotify_ifttt_importer.parse_ifttt_zip",
                        lambda path, tz: ["ev-ifttt"])
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient",
                        lambda: fake_client)

    ctx, _ = _make_ctx("spotify-ifttt", {"path": str(fake_zip), "tz": "America/New_York"})
    SPOTIFY_IFTTT_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-ifttt"]
    assert fake_client.calls["ensure_tag"] == "spotify"


def test_spotify_ifttt_plugin_defaults_tz_to_utc(monkeypatch, tmp_path):
    """When no tz is configured, parse_ifttt_zip is called with ZoneInfo('UTC')."""
    from zoneinfo import ZoneInfo
    fake_zip = tmp_path / "ifttt.zip"
    fake_zip.write_bytes(b"PK")

    received_tz = {}
    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.collect_plugins.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.spotify_ifttt_importer.parse_ifttt_zip",
        lambda path, tz: (received_tz.update({"tz": tz}) or []),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient",
                        lambda: fake_client)

    ctx, _ = _make_ctx("spotify-ifttt", {"path": str(fake_zip)})
    SPOTIFY_IFTTT_PLUGIN.run(ctx)

    assert received_tz["tz"] == ZoneInfo("UTC")


def test_spotify_ifttt_plugin_raises_without_path():
    ctx, _ = _make_ctx("spotify-ifttt", {})
    with pytest.raises(RuntimeError, match="path"):
        SPOTIFY_IFTTT_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Apple Takeout plugin
# ---------------------------------------------------------------------------

def test_apple_takeout_plugin_metadata():
    assert APPLE_TAKEOUT_PLUGIN.id == "apple-takeout"
    assert APPLE_TAKEOUT_PLUGIN.kind == "manual"
    assert APPLE_TAKEOUT_PLUGIN.default_interval is None
    assert not APPLE_TAKEOUT_PLUGIN.required_credentials


def test_apple_takeout_plugin_run_with_csv_file(monkeypatch, tmp_path):
    fake_csv = tmp_path / "Playback Activity.csv"
    fake_csv.write_text("header\n")

    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.collect_plugins.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.collect_plugins.apple_takeout_importer.parse_playback_csv",
                        lambda path: ["ev-apple"])
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient",
                        lambda: fake_client)

    ctx, _ = _make_ctx("apple-takeout", {"path": str(fake_csv)})
    APPLE_TAKEOUT_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-apple"]
    assert fake_client.calls["ensure_tag"] == "apple-tv"


def test_apple_takeout_plugin_run_with_directory(monkeypatch, tmp_path):
    """When path points to a directory, the plugin searches for 'Playback Activity.csv'."""
    subdir = tmp_path / "Apple Media Services" / "Apple TV"
    subdir.mkdir(parents=True)
    csv_file = subdir / "Playback Activity.csv"
    csv_file.write_text("header\n")

    fake_client = _FakeClient()
    parsed_paths = []
    monkeypatch.setattr("fulcra_media.collect_plugins.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.apple_takeout_importer.parse_playback_csv",
        lambda path: (parsed_paths.append(path) or ["ev-apple-dir"]),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient",
                        lambda: fake_client)

    ctx, _ = _make_ctx("apple-takeout", {"path": str(tmp_path)})
    APPLE_TAKEOUT_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-apple-dir"]
    assert parsed_paths[0] == csv_file


def test_apple_takeout_plugin_raises_when_no_csv_in_dir(monkeypatch, tmp_path):
    """A directory without 'Playback Activity.csv' raises RuntimeError."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    monkeypatch.setattr("fulcra_media.collect_plugins.library.resolve",
                        lambda p: Path(p))

    ctx, _ = _make_ctx("apple-takeout", {"path": str(empty_dir)})
    with pytest.raises(RuntimeError, match="Playback Activity.csv"):
        APPLE_TAKEOUT_PLUGIN.run(ctx)


def test_apple_takeout_plugin_raises_without_path():
    ctx, _ = _make_ctx("apple-takeout", {})
    with pytest.raises(RuntimeError, match="path"):
        APPLE_TAKEOUT_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Shared helpers for RSS scheduled plugin tests
# ---------------------------------------------------------------------------

def _make_event(start_iso: str):
    """Return a minimal fake NormalizedEvent-like object with start_time set."""
    from datetime import datetime, timezone

    class _FakeEvent:
        def __init__(self, iso: str):
            self.start_time = datetime.fromisoformat(iso.replace("Z", "+00:00"))

    return _FakeEvent(start_iso)


# ---------------------------------------------------------------------------
# Generic RSS plugin
# ---------------------------------------------------------------------------

def test_generic_rss_plugin_metadata():
    from datetime import timedelta
    assert GENERIC_RSS_PLUGIN.id == "generic-rss"
    assert GENERIC_RSS_PLUGIN.kind == "scheduled"
    assert GENERIC_RSS_PLUGIN.default_interval == timedelta(hours=6)
    assert not GENERIC_RSS_PLUGIN.required_credentials


def test_generic_rss_plugin_run_imports_and_advances_watermark(monkeypatch):
    ev = _make_event("2026-05-22T10:00:00+00:00")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter([ev]),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, st = _make_ctx(
        "generic-rss",
        {"feed_url": "https://example.com/feed.rss", "service": "mypodcast", "category": "listened"},
    )
    GENERIC_RSS_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [ev]
    assert fake_client.calls["ensure_tag"] == "mypodcast"
    assert st.watermark == "2026-05-22T10:00:00+00:00"


def test_generic_rss_plugin_filters_by_watermark(monkeypatch):
    """Events before the watermark must be excluded."""
    old_ev = _make_event("2026-05-20T00:00:00+00:00")
    new_ev = _make_event("2026-05-22T10:00:00+00:00")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter([old_ev, new_ev]),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, st = _make_ctx(
        "generic-rss",
        {"feed_url": "https://example.com/feed.rss", "service": "s", "category": "watched"},
    )
    st.watermark = "2026-05-21T00:00:00+00:00"
    GENERIC_RSS_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [new_ev]


def test_generic_rss_plugin_max_entries(monkeypatch):
    """max_entries slices the filtered list."""
    events = [_make_event("2026-05-22T10:00:00+00:00"),
              _make_event("2026-05-22T11:00:00+00:00"),
              _make_event("2026-05-22T12:00:00+00:00")]

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter(events),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda evs: "2026-05-22T10:00:00+00:00",
    )

    ctx, _ = _make_ctx(
        "generic-rss",
        {"feed_url": "https://example.com/f.rss", "service": "s", "category": "watched",
         "max_entries": 2},
    )
    GENERIC_RSS_PLUGIN.run(ctx)

    assert len(fake_client.calls["imported"]) == 2


def test_generic_rss_plugin_raises_without_feed_url():
    ctx, _ = _make_ctx("generic-rss", {"service": "s", "category": "watched"})
    with pytest.raises(RuntimeError, match="feed_url"):
        GENERIC_RSS_PLUGIN.run(ctx)


def test_generic_rss_plugin_raises_without_service():
    ctx, _ = _make_ctx("generic-rss", {"feed_url": "https://x.com/f", "category": "watched"})
    with pytest.raises(RuntimeError, match="service"):
        GENERIC_RSS_PLUGIN.run(ctx)


def test_generic_rss_plugin_raises_without_category():
    ctx, _ = _make_ctx("generic-rss", {"feed_url": "https://x.com/f", "service": "s"})
    with pytest.raises(RuntimeError, match="category"):
        GENERIC_RSS_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Letterboxd plugin
# ---------------------------------------------------------------------------

def test_letterboxd_plugin_metadata():
    from datetime import timedelta
    assert LETTERBOXD_PLUGIN.id == "letterboxd"
    assert LETTERBOXD_PLUGIN.kind == "scheduled"
    assert LETTERBOXD_PLUGIN.default_interval == timedelta(hours=12)
    assert not LETTERBOXD_PLUGIN.required_credentials


def test_letterboxd_plugin_run_imports_and_advances_watermark(monkeypatch):
    ev = _make_event("2026-05-22T10:00:00+00:00")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.lb_importer.fetch_diary",
        lambda username: iter([ev]),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, st = _make_ctx("letterboxd", {"username": "johndoe"})
    LETTERBOXD_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [ev]
    assert fake_client.calls["ensure_tag"] == "letterboxd"
    assert st.watermark == "2026-05-22T10:00:00+00:00"


def test_letterboxd_plugin_filters_by_watermark(monkeypatch):
    old_ev = _make_event("2026-05-20T00:00:00+00:00")
    new_ev = _make_event("2026-05-22T10:00:00+00:00")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.lb_importer.fetch_diary",
        lambda username: iter([old_ev, new_ev]),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, st = _make_ctx("letterboxd", {"username": "johndoe"})
    st.watermark = "2026-05-21T00:00:00+00:00"
    LETTERBOXD_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [new_ev]


def test_letterboxd_plugin_raises_without_username():
    ctx, _ = _make_ctx("letterboxd", {})
    with pytest.raises(RuntimeError, match="username"):
        LETTERBOXD_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Goodreads plugin
# ---------------------------------------------------------------------------

def test_goodreads_plugin_metadata():
    from datetime import timedelta
    assert GOODREADS_PLUGIN.id == "goodreads"
    assert GOODREADS_PLUGIN.kind == "scheduled"
    assert GOODREADS_PLUGIN.default_interval == timedelta(hours=12)
    assert not GOODREADS_PLUGIN.required_credentials


def test_goodreads_plugin_run_imports_and_advances_watermark(monkeypatch):
    ev = _make_event("2026-05-22T10:00:00+00:00")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.gr_importer.fetch_diary",
        lambda user_id: iter([ev]),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, st = _make_ctx("goodreads", {"user_id": "12345"})
    GOODREADS_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [ev]
    assert fake_client.calls["ensure_tag"] == "goodreads"
    assert st.watermark == "2026-05-22T10:00:00+00:00"


def test_goodreads_plugin_filters_by_watermark(monkeypatch):
    old_ev = _make_event("2026-05-20T00:00:00+00:00")
    new_ev = _make_event("2026-05-22T10:00:00+00:00")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.gr_importer.fetch_diary",
        lambda user_id: iter([old_ev, new_ev]),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, st = _make_ctx("goodreads", {"user_id": "12345"})
    st.watermark = "2026-05-21T00:00:00+00:00"
    GOODREADS_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [new_ev]


def test_goodreads_plugin_raises_without_user_id():
    ctx, _ = _make_ctx("goodreads", {})
    with pytest.raises(RuntimeError, match="user_id"):
        GOODREADS_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Deezer plugin
# ---------------------------------------------------------------------------

def test_deezer_plugin_metadata():
    from datetime import timedelta
    assert DEEZER_PLUGIN.id == "deezer"
    assert DEEZER_PLUGIN.kind == "scheduled"
    assert DEEZER_PLUGIN.default_interval == timedelta(hours=2)
    assert {c.key for c in DEEZER_PLUGIN.required_credentials} == {"access-token"}


def test_deezer_plugin_run_imports_and_advances_watermark(monkeypatch):
    """fetch_history + normalize_history are called; watermark advances on posted > 0."""
    fetch_calls = {}

    def fake_fetch(creds, since, max_pages):
        fetch_calls["creds"] = creds
        fetch_calls["since"] = since
        return [{"raw": 1}]

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.deezer_importer.fetch_history",
        fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.deezer_importer.normalize_history",
        lambda raw: ["ev-deezer"],
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, st = _make_ctx("deezer", {})
    ctx = RunContext(
        plugin_id="deezer", config={},
        credentials={"access-token": "mytoken"},
        state=st, log=logging.getLogger("t"), _emit=lambda e: None,
    )
    DEEZER_PLUGIN.run(ctx)

    assert fetch_calls["creds"] == {"access_token": "mytoken"}
    assert fetch_calls["since"] is None  # no watermark → full backfill
    assert fake_client.calls["imported"] == ["ev-deezer"]
    assert fake_client.calls["ensure_tag"] == "deezer"
    assert st.watermark == "2026-05-22T10:00:00+00:00"


def test_deezer_plugin_rewinds_watermark_by_one_hour(monkeypatch):
    """When a watermark is set, since = watermark - 1h."""
    from datetime import datetime, timezone, timedelta

    received_since = {}

    def fake_fetch(creds, since, max_pages):
        received_since["since"] = since
        return []

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.deezer_importer.fetch_history",
        fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.deezer_importer.normalize_history",
        lambda raw: [],
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)

    ctx, st = _make_ctx("deezer", {})
    st.watermark = "2026-05-22T12:00:00+00:00"
    ctx = RunContext(
        plugin_id="deezer", config={},
        credentials={"access-token": "tok"},
        state=st, log=logging.getLogger("t"), _emit=lambda e: None,
    )
    DEEZER_PLUGIN.run(ctx)

    expected = datetime(2026, 5, 22, 11, 0, 0, tzinfo=timezone.utc)
    assert received_since["since"] == expected


def test_deezer_plugin_raises_when_credential_missing():
    ctx, st = _make_ctx("deezer", {})
    ctx = RunContext(
        plugin_id="deezer", config={}, credentials={},
        state=st, log=logging.getLogger("t"), _emit=lambda e: None,
    )
    with pytest.raises(RuntimeError, match="access-token"):
        DEEZER_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Trakt plugin
# ---------------------------------------------------------------------------

def test_trakt_plugin_metadata():
    from datetime import timedelta
    assert TRAKT_PLUGIN.id == "trakt"
    assert TRAKT_PLUGIN.kind == "scheduled"
    assert TRAKT_PLUGIN.default_interval == timedelta(hours=6)
    assert not TRAKT_PLUGIN.required_credentials  # creds come from the file wizard


def test_trakt_plugin_run_imports_and_advances_watermark(monkeypatch):
    """fetch_history + normalize_history run; cluster/twin helpers are called;
    watermark advances when posted > 0."""
    fake_client = _FakeClient()

    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.fetch_history",
        lambda: [{"id": 1, "type": "movie", "watched_at": "2026-05-22T10:00:00.000Z"}],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.normalize_history",
        lambda items, cluster_threshold: ["ev-trakt"],
    )
    # Stub out cluster and twin helpers — no clusters, no twins.
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.apply_cluster_policy",
        lambda events, policy: events,
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.twin_cache.load_for_twin_lookup",
        lambda: [],
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, st = _make_ctx("trakt", {})
    TRAKT_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-trakt"]
    assert fake_client.calls["ensure_tag"] == "trakt"
    assert st.watermark == "2026-05-22T10:00:00+00:00"


def test_trakt_plugin_raises_when_clusters_is_ask(monkeypatch):
    """clusters='ask' is interactive and must raise RuntimeError in headless mode."""
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.fetch_history",
        lambda: [],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [],
    )

    ctx, _ = _make_ctx("trakt", {"clusters": "ask"})
    with pytest.raises(RuntimeError, match="ask"):
        TRAKT_PLUGIN.run(ctx)


def test_trakt_plugin_raises_when_twin_policy_is_ask(monkeypatch):
    """twin_policy='ask' is interactive and must raise RuntimeError in headless mode."""
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.fetch_history",
        lambda: [],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [],
    )

    ctx, _ = _make_ctx("trakt", {"twin_policy": "ask"})
    with pytest.raises(RuntimeError, match="ask"):
        TRAKT_PLUGIN.run(ctx)


def test_trakt_plugin_drops_clusters_when_policy_is_drop(monkeypatch):
    """clusters='drop' should call apply_cluster_policy with action='drop'."""
    from fulcra_csv import ClusterPolicy

    applied_policies = []

    def fake_apply(events, policy):
        applied_policies.append(policy)
        return events

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.fetch_history",
        lambda: [],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.normalize_history",
        lambda items, cluster_threshold: ["ev"],
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.apply_cluster_policy", fake_apply)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.twin_cache.load_for_twin_lookup",
        lambda: [],
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, _ = _make_ctx("trakt", {"clusters": "drop"})
    TRAKT_PLUGIN.run(ctx)

    assert len(applied_policies) == 1
    assert applied_policies[0].action == "drop"


def test_trakt_plugin_auto_discards_twins_when_policy_is_auto_discard(monkeypatch):
    """twin_policy='auto-discard' discards low-conf twins from the twin cache."""
    from datetime import datetime, timezone

    class _FakeLowConf:
        deterministic_id = "low-id"
        external_ids = {"content_fingerprint": "fp:music:artist:title"}
        timestamp_confidence = "low"
        start_time = datetime(2026, 5, 22, 10, tzinfo=timezone.utc)

    class _FakeHighConf:
        source_id = "high-id"
        external_ids = {"content_fingerprint": "fp:music:artist:title",
                        "importer": "lastfm"}
        timestamp_confidence = "high"
        start_time = datetime(2026, 5, 22, 10, tzinfo=timezone.utc)

    low = _FakeLowConf()
    high = _FakeHighConf()

    discard_calls = []

    def fake_apply_twin(events, discard_ids):
        discard_calls.append(discard_ids)
        return [e for e in events if getattr(e, "deterministic_id", None) not in discard_ids]

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.fetch_history",
        lambda: [],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [low],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.apply_cluster_policy",
        lambda events, policy: events,
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.find_low_conf_twins",
        lambda events, extra_pool: [(low, high)],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.twin_cache.load_for_twin_lookup",
        lambda: [high],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.apply_twin_decisions",
        fake_apply_twin,
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: None,
    )

    ctx, _ = _make_ctx("trakt", {"twin_policy": "auto-discard"})
    TRAKT_PLUGIN.run(ctx)

    assert len(discard_calls) == 1
    assert "low-id" in discard_calls[0]


# ---------------------------------------------------------------------------
# Apple Podcasts (on-device) plugin
# ---------------------------------------------------------------------------

def test_apple_podcasts_plugin_metadata():
    from datetime import timedelta
    assert APPLE_PODCASTS_PLUGIN.id == "apple-podcasts"
    assert APPLE_PODCASTS_PLUGIN.name == "Apple Podcasts (on-device)"
    assert APPLE_PODCASTS_PLUGIN.kind == "scheduled"
    assert APPLE_PODCASTS_PLUGIN.default_interval == timedelta(hours=6)
    assert APPLE_PODCASTS_PLUGIN.requires_network is False
    perm_ids = {p.id for p in APPLE_PODCASTS_PLUGIN.required_permissions}
    assert "full-disk-access" in perm_ids


def test_apple_podcasts_plugin_run_imports_and_advances_watermark(monkeypatch):
    """parse_db is called; events are imported; watermark advances when posted > 0."""
    ev = _make_event("2026-05-22T10:00:00+00:00")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.ap.parse_db",
        lambda db_path: iter([ev]),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )

    ctx, st = _make_ctx("apple-podcasts", {})
    APPLE_PODCASTS_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [ev]
    assert fake_client.calls["ensure_tag"] == "apple-podcasts"
    assert st.watermark == "2026-05-22T10:00:00+00:00"


def test_apple_podcasts_plugin_uses_config_db_path(monkeypatch, tmp_path):
    """When db_path is set in config, that path is passed to parse_db."""
    custom_db = tmp_path / "custom.sqlite"
    custom_db.touch()

    received_paths = []
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.ap.parse_db",
        lambda db_path: (received_paths.append(db_path) or iter([])),
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)

    ctx, _ = _make_ctx("apple-podcasts", {"db_path": str(custom_db)})
    APPLE_PODCASTS_PLUGIN.run(ctx)

    assert received_paths[0] == custom_db


def test_apple_podcasts_plugin_snapshot_error_becomes_runtime_error(monkeypatch):
    """A SnapshotError from parse_db must be re-raised as RuntimeError."""
    from fulcra_media.importers.apple_podcasts import SnapshotError

    def _raise_snapshot_error(db_path):
        raise SnapshotError("stalled")

    monkeypatch.setattr(
        "fulcra_media.collect_plugins.ap.parse_db",
        _raise_snapshot_error,
    )

    ctx, _ = _make_ctx("apple-podcasts", {})
    with pytest.raises(RuntimeError, match="stalled"):
        APPLE_PODCASTS_PLUGIN.run(ctx)


# ---------------------------------------------------------------------------
# Apple Podcasts (Time Machine recovery) plugin
# ---------------------------------------------------------------------------

def test_apple_podcasts_timemachine_plugin_metadata():
    assert APPLE_PODCASTS_TIMEMACHINE_PLUGIN.id == "apple-podcasts-timemachine"
    assert APPLE_PODCASTS_TIMEMACHINE_PLUGIN.name == "Apple Podcasts (Time Machine recovery)"
    assert APPLE_PODCASTS_TIMEMACHINE_PLUGIN.kind == "manual"
    assert APPLE_PODCASTS_TIMEMACHINE_PLUGIN.requires_network is False
    perm_ids = {p.id for p in APPLE_PODCASTS_TIMEMACHINE_PLUGIN.required_permissions}
    assert "full-disk-access" in perm_ids


def test_apple_podcasts_timemachine_plugin_run_imports_all_snapshots(monkeypatch, tmp_path):
    """All events from all snapshots are imported; no watermark is set."""
    snap1 = tmp_path / "snap1.sqlite"
    snap2 = tmp_path / "snap2.sqlite"
    ev1 = _make_event("2026-05-20T10:00:00+00:00")
    ev2 = _make_event("2026-05-21T10:00:00+00:00")

    fake_client = _FakeClient()

    def fake_parse_db(path):
        return iter([ev1] if path == snap1 else [ev2])

    monkeypatch.setattr(
        "fulcra_media.collect_plugins.ap.find_timemachine_snapshots",
        lambda: [snap1, snap2],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.ap.parse_db",
        fake_parse_db,
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)

    ctx, st = _make_ctx("apple-podcasts-timemachine", {})
    APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run(ctx)

    assert set(fake_client.calls["imported"]) == {ev1, ev2}
    assert fake_client.calls["ensure_tag"] == "apple-podcasts"
    # Manual plugin: watermark must NOT be set
    assert st.watermark is None


def test_apple_podcasts_timemachine_plugin_skips_erroring_snapshots(monkeypatch, tmp_path):
    """A SnapshotError on one snapshot is logged and skipped; others still import."""
    from fulcra_media.importers.apple_podcasts import SnapshotError

    snap1 = tmp_path / "good.sqlite"
    snap2 = tmp_path / "bad.sqlite"
    ev1 = _make_event("2026-05-20T10:00:00+00:00")

    log_warnings = []
    fake_client = _FakeClient()

    class _FakeLog:
        def warning(self, *args, **kwargs):
            log_warnings.append(args)

    def fake_parse_db(path):
        if path == snap2:
            raise SnapshotError("bad snapshot")
        return iter([ev1])

    monkeypatch.setattr(
        "fulcra_media.collect_plugins.ap.find_timemachine_snapshots",
        lambda: [snap1, snap2],
    )
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.ap.parse_db",
        fake_parse_db,
    )
    monkeypatch.setattr("fulcra_media.collect_plugins.FulcraClient", lambda: fake_client)

    st = PluginState("apple-podcasts-timemachine")
    ctx = RunContext(
        plugin_id="apple-podcasts-timemachine",
        config={},
        credentials={},
        state=st,
        log=_FakeLog(),
        _emit=lambda e: None,
    )
    APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [ev1]
    assert len(log_warnings) == 1


def test_apple_podcasts_timemachine_plugin_raises_when_no_snapshots(monkeypatch):
    """When find_timemachine_snapshots returns empty, raise RuntimeError."""
    monkeypatch.setattr(
        "fulcra_media.collect_plugins.ap.find_timemachine_snapshots",
        lambda: [],
    )

    ctx, _ = _make_ctx("apple-podcasts-timemachine", {})
    with pytest.raises(RuntimeError, match="Time Machine"):
        APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run(ctx)
