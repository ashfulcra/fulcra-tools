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
