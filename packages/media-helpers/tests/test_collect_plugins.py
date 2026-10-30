"""Media source normalization and settings tests.

Source-only cases call the unwrapped function with an injected client; actual
guarded entrypoints, strict auth and worker transport are tested in
``test_collect_v1_wiring.py``. Legacy annotation-resolver expectations have
been replaced by the v1 destination contract/readback tests.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_media.collect_plugins import (
    LASTFM_LISTENED_SPEC,
    LASTFM_PLUGIN,
    NETFLIX_PLUGIN,
    SPOTIFY_EXTENDED_LISTENED_SPEC,
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
from fulcra_media.state import State as MediaState


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_bootstrapped_media_state() -> MediaState:
    """A MediaState with all definition IDs pre-populated (simulates bootstrap
    having been run). Tests that exercise the normal import path use this to
    ensure the resolver is NOT invoked."""
    return MediaState(
        watched_definition_id="def-watched-123",
        listened_definition_id="def-listened-456",
        read_definition_id="def-read-789",
    )


def _make_empty_media_state() -> MediaState:
    """A MediaState with no definition IDs (simulates machine 2 that has never
    run bootstrap). Tests that exercise the R6 resolver path use this."""
    return MediaState()


# ---------------------------------------------------------------------------
# Metadata tests
# ---------------------------------------------------------------------------

def test_lastfm_plugin_metadata_is_scheduled():
    assert LASTFM_PLUGIN.id == "lastfm"
    assert LASTFM_PLUGIN.kind == "scheduled"
    assert LASTFM_PLUGIN.default_interval is not None
    assert {c.key for c in LASTFM_PLUGIN.required_credentials} == {"api-key"}


@pytest.mark.parametrize("plugin", [
    APPLE_PODCASTS_PLUGIN,
    APPLE_PODCASTS_TIMEMACHINE_PLUGIN,
])
def test_apple_plugin_full_disk_access_guidance_names_installed_app(plugin):
    body = "\n".join(step.body_md for step in plugin.setup_steps)
    assert "Fulcra Collect" in body
    assert "terminal running" not in body
    assert "once it exists" not in body


def test_lastfm_setup_steps_contain_a_test_connection_step():
    """The Last.fm wizard must verify entered creds before letting the
    user advance to definition_picker / done. Without this step, bad
    api-key/username combos silently persist and only surface as a run
    failure an hour later. The step lives between the paste-creds input
    and the definition_picker.
    """
    kinds = [s.kind for s in LASTFM_PLUGIN.setup_steps]
    assert "test_connection" in kinds, (
        f"Last.fm wizard is missing a test_connection step. Got kinds: {kinds}"
    )
    # And it must be reachable BEFORE definition_picker — otherwise the
    # user has already bound a Fulcra def before they discover their
    # Last.fm creds are wrong.
    assert kinds.index("test_connection") < kinds.index("definition_picker")
    # …and AFTER the input step (no point testing empty creds).
    assert kinds.index("input") < kinds.index("test_connection")


def test_lastfm_plugin_declares_a_health_check():
    """The wizard's test_connection step is wired by the daemon's
    /api/plugin/{id}/health_check route to plugin.health_check. Without
    one declared, the step renders empty and Next is unreachable."""
    assert LASTFM_PLUGIN.health_check is not None


def test_lastfm_plugin_declares_canonical_definition_name():
    """R6: the plugin opts into the shared resolver via canonical_definition_name."""
    assert LASTFM_PLUGIN.canonical_definition_name == "Listened"


def test_lastfm_listened_spec_shape():
    """LASTFM_LISTENED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    assert LASTFM_LISTENED_SPEC["annotation_type"] == "duration"
    ms = LASTFM_LISTENED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches


# ---------------------------------------------------------------------------
# Normal import path (media state already bootstrapped)
# ---------------------------------------------------------------------------



def test_run_raises_a_clear_error_when_the_api_key_is_missing(monkeypatch):
    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "test_user"},
                     credentials={},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    with pytest.raises(RuntimeError, match="api-key"):
        LASTFM_PLUGIN.run.__wrapped__(ctx)


def test_run_raises_a_clear_error_when_username_setting_is_missing(monkeypatch):
    """Regression for the 2026-05-25 Last.fm KeyError bug. Username lives in
    settings (configured via the wizard or `set-setting`), api-key in
    credentials. Before the fix, _run_lastfm forwarded only api_key, so the
    importer crashed with KeyError: 'username' the moment a real sync ran.
    Now we fail fast with a clear, actionable RuntimeError instead.
    """
    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={},
                     credentials={"api-key": "K"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    with pytest.raises(RuntimeError, match="username"):
        LASTFM_PLUGIN.run.__wrapped__(ctx)








# ---------------------------------------------------------------------------
# R6 resolver path (no pre-existing listened_definition_id)
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Helpers shared by file-plugin tests
# ---------------------------------------------------------------------------

def _bind_trakt_policy_context(ctx):
    """Strict memory source for policy-only tests; worker acceptance is separate."""
    from fulcra_collect.trakt_source import Owner
    class Store:
        key = 'synthetic-policy-trakt'
        file = False
        def snapshot(self):
            return {'access_token': 'synthetic-access', 'client_id': 'synthetic-client',
                    'refresh_token': 'synthetic-refresh', 'client_secret': None,
                    'redirect_uri': None, 'created_at': None, 'expires_in': None, **ctx.credentials}
    ctx._trakt_source_owner = Owner(Store())
    ctx._register_current_work_guard = lambda callback: None
    ctx._trakt_authority = lambda: None
    ctx._trakt_local = lambda: None


@pytest.fixture(autouse=True)
def _trakt_policy_source_only(request, monkeypatch):
    if request.node.name.startswith(('test_trakt', 'test_run_trakt')):
        from fulcra_media.importers import trakt

        from fulcra_collect import trakt_identity
        monkeypatch.setattr(trakt_identity, 'probe_identity', lambda subject, bounds, **kw:
            trakt_identity.Observation(subject, 'Synthetic opaque identity', trakt_identity.ENDPOINT, 1.0))
        monkeypatch.setattr(trakt, 'fetch_history_with_headers',
                            lambda headers, **kw: trakt.fetch_history(strict_scan=True))


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
    if plugin_id == "trakt":
        _bind_trakt_policy_context(ctx)
    return ctx, st


class _FakeResult:
    posted = 2
    skipped_existing = 1
    verified = 2


class _FakeClient:
    def prepare_media(self, categories):
        pass
    def __init__(self, **_kwargs):
        self.calls = {}

    def ensure_tag(self, name, state):
        self.calls["ensure_tag"] = name

    def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
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
    monkeypatch.setattr("fulcra_media.plugins.netflix.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.netflix.netflix_importer.parse_auto",
                        lambda path: ["ev-netflix"])
    monkeypatch.setattr("fulcra_media.plugins.netflix.FulcraClient",
                        lambda **_kwargs: fake_client)
    # R8: _run_netflix now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.netflix._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("netflix", {"path": str(fake_csv)})
    NETFLIX_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == ["ev-netflix"]


def test_netflix_plugin_raises_without_path(monkeypatch):
    # Make hermetic: stub _state_load so the resolver guard exits
    # without needing a fulcra client (which this ctx doesn't have).
    # Previously this test relied on a leftover ~/.config/fulcra-media/
    # state.json containing a cached watched_definition_id. After the
    # 2026-05-26 state-clear that file is empty, exposing the env
    # dependency.
    monkeypatch.setattr("fulcra_media.plugins.netflix._state_load",
                        lambda path: _make_bootstrapped_media_state())
    ctx, _ = _make_ctx("netflix", {})
    with pytest.raises(RuntimeError, match="path"):
        NETFLIX_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# R8 regression-guard tests for netflix resolver
# ---------------------------------------------------------------------------

def test_netflix_plugin_declares_canonical_definition_name():
    """R8: the plugin opts into the shared resolver via canonical_definition_name."""
    assert NETFLIX_PLUGIN.canonical_definition_name == "Watched"


def test_netflix_watched_spec_shape():
    """NETFLIX_WATCHED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import NETFLIX_WATCHED_SPEC
    assert NETFLIX_WATCHED_SPEC["annotation_type"] == "duration"
    ms = NETFLIX_WATCHED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






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
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended.spotify_importer.parse_extended_zip",
                        lambda path: ["ev-spotify"])
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended.FulcraClient",
                        lambda **_kwargs: fake_client)
    # R7: _run_spotify_extended now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("spotify-extended", {"path": str(fake_zip)})
    SPOTIFY_EXTENDED_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == ["ev-spotify"]


def test_spotify_extended_plugin_raises_without_path(monkeypatch):
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended._state_load",
                        lambda path: _make_bootstrapped_media_state())
    ctx, _ = _make_ctx("spotify-extended", {})
    with pytest.raises(RuntimeError, match="path"):
        SPOTIFY_EXTENDED_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# R7 regression-guard tests for spotify-extended resolver
# ---------------------------------------------------------------------------

def test_spotify_extended_plugin_declares_canonical_definition_name():
    """R7: the plugin opts into the shared resolver via canonical_definition_name."""
    assert SPOTIFY_EXTENDED_PLUGIN.canonical_definition_name == "Listened"


def test_spotify_extended_listened_spec_shape():
    """SPOTIFY_EXTENDED_LISTENED_SPEC must declare a duration annotation with a
    full measurement_spec so the resolver can match existing definitions."""
    assert SPOTIFY_EXTENDED_LISTENED_SPEC["annotation_type"] == "duration"
    ms = SPOTIFY_EXTENDED_LISTENED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






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
    monkeypatch.setattr("fulcra_media.plugins.youtube.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.youtube.youtube_importer.parse_takeout_json",
                        lambda path: ["ev-youtube"])
    monkeypatch.setattr("fulcra_media.plugins.youtube.FulcraClient",
                        lambda **_kwargs: fake_client)
    # R8: _run_youtube now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.youtube._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("youtube", {"path": str(fake_json)})
    YOUTUBE_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == ["ev-youtube"]


def test_youtube_plugin_raises_without_path(monkeypatch):
    monkeypatch.setattr("fulcra_media.plugins.youtube._state_load",
                        lambda path: _make_bootstrapped_media_state())
    ctx, _ = _make_ctx("youtube", {})
    with pytest.raises(RuntimeError, match="path"):
        YOUTUBE_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# R8 regression-guard tests for youtube resolver
# ---------------------------------------------------------------------------

def test_youtube_plugin_declares_canonical_definition_name():
    """R8: the plugin opts into the shared resolver via canonical_definition_name."""
    assert YOUTUBE_PLUGIN.canonical_definition_name == "Watched"


def test_youtube_watched_spec_shape():
    """YOUTUBE_WATCHED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import YOUTUBE_WATCHED_SPEC
    assert YOUTUBE_WATCHED_SPEC["annotation_type"] == "duration"
    ms = YOUTUBE_WATCHED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






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
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.spotify_ifttt_importer.parse_ifttt_zip",
                        lambda path, tz: ["ev-ifttt"])
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.FulcraClient",
                        lambda **_kwargs: fake_client)
    # BR7: _run_spotify_ifttt now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("spotify-ifttt", {"path": str(fake_zip), "tz": "America/New_York"})
    SPOTIFY_IFTTT_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == ["ev-ifttt"]


def test_spotify_ifttt_plugin_defaults_tz_to_utc(monkeypatch, tmp_path):
    """When no tz is configured, parse_ifttt_zip is called with ZoneInfo('UTC')."""
    from zoneinfo import ZoneInfo
    fake_zip = tmp_path / "ifttt.zip"
    fake_zip.write_bytes(b"PK")

    received_tz = {}
    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.spotify_ifttt.spotify_ifttt_importer.parse_ifttt_zip",
        lambda path, tz: (received_tz.update({"tz": tz}) or []),
    )
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.FulcraClient",
                        lambda **_kwargs: fake_client)
    # BR7: _run_spotify_ifttt now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("spotify-ifttt", {"path": str(fake_zip)})
    SPOTIFY_IFTTT_PLUGIN.run.__wrapped__(ctx)

    assert received_tz["tz"] == ZoneInfo("UTC")


def test_spotify_ifttt_plugin_raises_without_path(monkeypatch):
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt._state_load",
                        lambda path: _make_bootstrapped_media_state())
    ctx, _ = _make_ctx("spotify-ifttt", {})
    with pytest.raises(RuntimeError, match="path"):
        SPOTIFY_IFTTT_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# BR7 regression-guard tests for spotify-ifttt resolver
# ---------------------------------------------------------------------------

def test_spotify_ifttt_plugin_declares_canonical_definition_name():
    """BR7: the plugin opts into the shared resolver via canonical_definition_name."""
    assert SPOTIFY_IFTTT_PLUGIN.canonical_definition_name == "Listened"


def test_spotify_ifttt_listened_spec_shape():
    """SPOTIFY_IFTTT_LISTENED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import SPOTIFY_IFTTT_LISTENED_SPEC
    assert SPOTIFY_IFTTT_LISTENED_SPEC["annotation_type"] == "duration"
    ms = SPOTIFY_IFTTT_LISTENED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






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
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.library.resolve",
                        lambda p: Path(p))
    # _run_apple_takeout now delegates to the importer's parse_any (which
    # handles file/dir/zip/nested-zip itself); patch that entry point.
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.apple_takeout_importer.parse_any",
                        lambda path, *, since=None, until=None: ["ev-apple"])
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.FulcraClient",
                        lambda **_kwargs: fake_client)
    # R8: _run_apple_takeout now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("apple-takeout", {"path": str(fake_csv)})
    APPLE_TAKEOUT_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == ["ev-apple"]


def test_apple_takeout_plugin_run_with_directory(monkeypatch, tmp_path):
    """When path points to a directory, the importer's parse_any handles the search."""
    subdir = tmp_path / "Apple Media Services" / "Apple TV"
    subdir.mkdir(parents=True)
    csv_file = subdir / "Playback Activity.csv"
    csv_file.write_text("header\n")

    fake_client = _FakeClient()
    parsed_paths = []
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_takeout.apple_takeout_importer.parse_any",
        lambda path, *, since=None, until=None: (
            parsed_paths.append(path) or ["ev-apple-dir"]
        ),
    )
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.FulcraClient",
                        lambda **_kwargs: fake_client)
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("apple-takeout", {"path": str(tmp_path)})
    APPLE_TAKEOUT_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == ["ev-apple-dir"]
    # The plugin passes the directory through verbatim; parse_any does the search.
    assert parsed_paths[0] == tmp_path


def test_apple_takeout_plugin_raises_when_no_csv_in_dir(monkeypatch, tmp_path):
    """A directory with no takeout CSV raises RuntimeError."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("apple-takeout", {"path": str(empty_dir)})
    # parse_any mentions both Video Play Activity and Playback Activity now.
    with pytest.raises(RuntimeError, match="Video Play Activity.csv|Playback Activity.csv"):
        APPLE_TAKEOUT_PLUGIN.run.__wrapped__(ctx)


def test_apple_takeout_plugin_raises_without_path(monkeypatch):
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda path: _make_bootstrapped_media_state())
    ctx, _ = _make_ctx("apple-takeout", {})
    with pytest.raises(RuntimeError, match="path"):
        APPLE_TAKEOUT_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# R8 regression-guard tests for apple-takeout resolver
# ---------------------------------------------------------------------------

def test_apple_takeout_plugin_declares_canonical_definition_name():
    """R8: the plugin opts into the shared resolver via canonical_definition_name."""
    assert APPLE_TAKEOUT_PLUGIN.canonical_definition_name == "Watched"


def test_apple_takeout_watched_spec_shape():
    """APPLE_TAKEOUT_WATCHED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import APPLE_TAKEOUT_WATCHED_SPEC
    assert APPLE_TAKEOUT_WATCHED_SPEC["annotation_type"] == "duration"
    ms = APPLE_TAKEOUT_WATCHED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






# ---------------------------------------------------------------------------
# Shared helpers for RSS scheduled plugin tests
# ---------------------------------------------------------------------------

def _make_event(start_iso: str):
    """Return a minimal fake NormalizedEvent-like object with start_time set."""
    from datetime import datetime

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










def test_generic_rss_plugin_raises_without_feed_url():
    ctx, _ = _make_ctx("generic-rss", {"service": "s", "category": "watched"})
    with pytest.raises(RuntimeError, match="feed_url"):
        GENERIC_RSS_PLUGIN.run.__wrapped__(ctx)


def test_generic_rss_plugin_raises_without_service():
    ctx, _ = _make_ctx("generic-rss", {"feed_url": "https://x.com/f", "category": "watched"})
    with pytest.raises(RuntimeError, match="service"):
        GENERIC_RSS_PLUGIN.run.__wrapped__(ctx)


def test_generic_rss_plugin_raises_without_category():
    ctx, _ = _make_ctx("generic-rss", {"feed_url": "https://x.com/f", "service": "s"})
    with pytest.raises(RuntimeError, match="category"):
        GENERIC_RSS_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# BR11 regression-guard tests for generic-rss resolver
# ---------------------------------------------------------------------------

def test_generic_rss_plugin_canonical_definition_name_is_none():
    """BR11: generic-rss has no static canonical name — it depends on runtime
    config.  The Plugin object must NOT declare canonical_definition_name."""
    assert GENERIC_RSS_PLUGIN.canonical_definition_name is None








# ---------------------------------------------------------------------------
# Letterboxd plugin
# ---------------------------------------------------------------------------

def test_letterboxd_plugin_metadata():
    from datetime import timedelta
    assert LETTERBOXD_PLUGIN.id == "letterboxd"
    assert LETTERBOXD_PLUGIN.kind == "scheduled"
    assert LETTERBOXD_PLUGIN.default_interval == timedelta(hours=12)
    assert not LETTERBOXD_PLUGIN.required_credentials






def test_letterboxd_plugin_raises_without_username():
    ctx, _ = _make_ctx("letterboxd", {})
    with pytest.raises(RuntimeError, match="username"):
        LETTERBOXD_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# R8 regression-guard tests for letterboxd resolver
# ---------------------------------------------------------------------------

def test_letterboxd_plugin_declares_canonical_definition_name():
    """R8: the plugin opts into the shared resolver via canonical_definition_name."""
    assert LETTERBOXD_PLUGIN.canonical_definition_name == "Watched"


def test_letterboxd_watched_spec_shape():
    """LETTERBOXD_WATCHED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import LETTERBOXD_WATCHED_SPEC
    assert LETTERBOXD_WATCHED_SPEC["annotation_type"] == "duration"
    ms = LETTERBOXD_WATCHED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






# ---------------------------------------------------------------------------
# Goodreads plugin
# ---------------------------------------------------------------------------

def test_goodreads_plugin_metadata():
    from datetime import timedelta
    assert GOODREADS_PLUGIN.id == "goodreads"
    assert GOODREADS_PLUGIN.kind == "scheduled"
    assert GOODREADS_PLUGIN.default_interval == timedelta(hours=12)
    assert not GOODREADS_PLUGIN.required_credentials






def test_goodreads_plugin_raises_without_user_id():
    ctx, _ = _make_ctx("goodreads", {})
    with pytest.raises(RuntimeError, match="user_id"):
        GOODREADS_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# BR10 regression-guard tests for goodreads resolver
# ---------------------------------------------------------------------------

def test_goodreads_plugin_declares_canonical_definition_name():
    """BR10: the plugin opts into the shared resolver via canonical_definition_name."""
    assert GOODREADS_PLUGIN.canonical_definition_name == "Read"


def test_goodreads_read_spec_shape():
    """GOODREADS_READ_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import GOODREADS_READ_SPEC
    assert GOODREADS_READ_SPEC["annotation_type"] == "duration"
    ms = GOODREADS_READ_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






# ---------------------------------------------------------------------------
# Deezer plugin
# ---------------------------------------------------------------------------

def test_deezer_plugin_metadata():
    from datetime import timedelta
    assert DEEZER_PLUGIN.id == "deezer"
    assert DEEZER_PLUGIN.kind == "scheduled"
    assert DEEZER_PLUGIN.default_interval == timedelta(hours=2)
    assert {c.key for c in DEEZER_PLUGIN.required_credentials} == {"access-token"}






def test_deezer_plugin_raises_when_credential_missing():
    ctx, st = _make_ctx("deezer", {})
    ctx = RunContext(
        plugin_id="deezer", config={}, credentials={},
        state=st, log=logging.getLogger("t"), _emit=lambda e: None,
    )
    with pytest.raises(RuntimeError, match="access-token"):
        DEEZER_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# BR6 regression-guard tests for deezer resolver
# ---------------------------------------------------------------------------

def test_deezer_plugin_declares_canonical_definition_name():
    """BR6: the plugin opts into the shared resolver via canonical_definition_name."""
    assert DEEZER_PLUGIN.canonical_definition_name == "Listened"


def test_deezer_listened_spec_shape():
    """DEEZER_LISTENED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import DEEZER_LISTENED_SPEC
    assert DEEZER_LISTENED_SPEC["annotation_type"] == "duration"
    ms = DEEZER_LISTENED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






# ---------------------------------------------------------------------------
# Trakt plugin
# ---------------------------------------------------------------------------

def test_trakt_plugin_metadata():
    from datetime import timedelta
    assert TRAKT_PLUGIN.id == "trakt"
    assert TRAKT_PLUGIN.kind == "scheduled"
    assert TRAKT_PLUGIN.default_interval == timedelta(hours=6)
    # Credentials are now managed via the web-UI OAuth wizard and stored in
    # the OS keychain. The legacy secret and saved refresh callback are optional.
    cred_keys = {c.key for c in TRAKT_PLUGIN.required_credentials}
    assert cred_keys == {"client_id", "client_secret", "access_token", "refresh_token", "redirect_uri"}
    assert TRAKT_PLUGIN.oauth_handler is not None
    assert TRAKT_PLUGIN.health_check is not None
    assert len(TRAKT_PLUGIN.setup_steps) == 7


def test_trakt_plugin_run_imports_and_advances_watermark(monkeypatch):
    """fetch_history + normalize_history run; cluster/twin helpers are called;
    watermark advances when posted > 0."""
    fake_client = _FakeClient()

    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda **_kwargs: [{"id": 1, "type": "movie", "watched_at": "2026-05-22T10:00:00.000Z"}],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: ["ev-trakt"],
    )
    # Stub out cluster and twin helpers — no clusters, no twins.
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.apply_cluster_policy",
        lambda events, policy: events,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins._common.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient", lambda **_kwargs: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # R8: _run_trakt now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, st = _make_ctx("trakt", {})
    TRAKT_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == ["ev-trakt"]
    assert st.watermark == "2026-05-22T10:00:00+00:00"


def test_trakt_plugin_raises_when_clusters_is_ask(monkeypatch):
    """clusters='ask' is interactive and must raise RuntimeError in headless mode."""
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [],
    )

    ctx, _ = _make_ctx("trakt", {"clusters": "ask"})
    with pytest.raises(RuntimeError, match="ask"):
        TRAKT_PLUGIN.run.__wrapped__(ctx)


def test_trakt_plugin_raises_when_twin_policy_is_ask(monkeypatch):
    """twin_policy='ask' is interactive and must raise RuntimeError in headless mode."""
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [],
    )

    ctx, _ = _make_ctx("trakt", {"twin_policy": "ask"})
    with pytest.raises(RuntimeError, match="ask"):
        TRAKT_PLUGIN.run.__wrapped__(ctx)


def test_trakt_plugin_drops_clusters_when_policy_is_drop(monkeypatch):
    """clusters='drop' should call apply_cluster_policy with action='drop'."""

    applied_policies = []

    def fake_apply(events, policy):
        applied_policies.append(policy)
        return events

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: ["ev"],
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.apply_cluster_policy", fake_apply)
    monkeypatch.setattr(
        "fulcra_media.plugins._common.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient", lambda **_kwargs: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("trakt", {"clusters": "drop"})
    TRAKT_PLUGIN.run.__wrapped__(ctx)

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

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [low],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.apply_cluster_policy",
        lambda events, policy: events,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins._common.find_low_conf_twins",
        lambda events, extra_pool: [(low, high)],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda **_kwargs: [high],
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient", lambda **_kwargs: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso",
        lambda events: None,
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("trakt", {"twin_policy": "auto-discard"})
    TRAKT_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == []


def test_trakt_auto_discard_does_not_crash_on_normalized_event(monkeypatch):
    """Regression: trakt's auto-discard path filtered with fulcra_csv's
    apply_twin_decisions, which reads ``e.source_id`` — but NormalizedEvent
    exposes its dedup id as ``deterministic_id``, so the real filter raised
    AttributeError. Drive the path with REAL NormalizedEvents (no patch over
    the filter) and assert the matched low-conf twin is dropped WITHOUT raising.
    """
    from datetime import datetime, timezone

    from fulcra_media.importers.base import NormalizedEvent

    def _ne(*, sid, conf, fp, ts):
        return NormalizedEvent(
            importer="trakt", service="trakt", category="watched",
            note="x", title="x", start_time=ts, end_time=ts,
            deterministic_id=sid, timestamp_confidence=conf,
            external_ids={"content_fingerprint": fp},
        )

    low = _ne(sid="trakt-low", conf="low", fp="tv:dune:s01e01",
              ts=datetime(2026, 5, 16, tzinfo=timezone.utc))
    keep = _ne(sid="trakt-keep", conf="low", fp="tv:other:s01e01",
               ts=datetime(2026, 5, 17, tzinfo=timezone.utc))
    high = _ne(sid="netflix-high", conf="high", fp="tv:dune:s01e01",
               ts=datetime(2026, 4, 1, tzinfo=timezone.utc))

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history", lambda **_kwargs: [])
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [low, keep])
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.apply_cluster_policy",
        lambda events, policy: events)
    # Real find_low_conf_twins + real filter path — only the cache load is faked.
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda **_kwargs: [high])
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.FulcraClient", lambda **_kwargs: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso", lambda events: None)
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("trakt", {"twin_policy": "auto-discard"})
    TRAKT_PLUGIN.run.__wrapped__(ctx)  # must not raise AttributeError

    # The matched low-conf twin was dropped; the unmatched low-conf event stays.
    assert fake_client.calls["imported"] == [keep]


def test_trakt_default_keep_policy_imports_everything(monkeypatch):
    """Default twin_policy ('keep') is a no-op: nothing is discarded even when a
    matching high-conf twin exists in the cache."""
    from datetime import datetime, timezone

    from fulcra_media.importers.base import NormalizedEvent

    low = NormalizedEvent(
        importer="trakt", service="trakt", category="watched", note="x",
        title="x", start_time=datetime(2026, 5, 16, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 16, tzinfo=timezone.utc),
        deterministic_id="trakt-low", timestamp_confidence="low",
        external_ids={"content_fingerprint": "tv:dune:s01e01"},
    )

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history", lambda **_kwargs: [])
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [low])
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.apply_cluster_policy",
        lambda events, policy: events)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.FulcraClient", lambda **_kwargs: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso", lambda events: None)
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("trakt", {})  # default twin_policy = keep
    TRAKT_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == [low]


# ---------------------------------------------------------------------------
# R8 regression-guard tests for trakt resolver
# ---------------------------------------------------------------------------

def test_trakt_plugin_declares_canonical_definition_name():
    """R8: the plugin opts into the shared resolver via canonical_definition_name."""
    assert TRAKT_PLUGIN.canonical_definition_name == "Watched"


def test_trakt_watched_spec_shape():
    """TRAKT_WATCHED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import TRAKT_WATCHED_SPEC
    assert TRAKT_WATCHED_SPEC["annotation_type"] == "duration"
    ms = TRAKT_WATCHED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






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
        "fulcra_media.plugins.apple_podcasts.ap.parse_db",
        lambda db_path: iter([ev]),
    )
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.FulcraClient", lambda **_kwargs: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # BR8: _run_apple_podcasts now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, st = _make_ctx("apple-podcasts", {})
    APPLE_PODCASTS_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == [ev]
    assert st.watermark == "2026-05-22T10:00:00+00:00"


def test_apple_podcasts_plugin_uses_config_db_path(monkeypatch, tmp_path):
    """When db_path is set in config, that path is passed to parse_db."""
    custom_db = tmp_path / "custom.sqlite"
    custom_db.touch()

    received_paths = []
    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts.ap.parse_db",
        lambda db_path: (received_paths.append(db_path) or iter([])),
    )
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.FulcraClient", lambda **_kwargs: fake_client)
    # BR8: _run_apple_podcasts now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("apple-podcasts", {"db_path": str(custom_db)})
    APPLE_PODCASTS_PLUGIN.run.__wrapped__(ctx)

    assert received_paths[0] == custom_db


def test_apple_podcasts_plugin_snapshot_error_becomes_runtime_error(monkeypatch):
    """A SnapshotError from parse_db must be re-raised as RuntimeError."""
    from fulcra_media.importers.apple_podcasts import SnapshotError

    def _raise_snapshot_error(db_path):
        raise SnapshotError("stalled")

    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts.ap.parse_db",
        _raise_snapshot_error,
    )

    ctx, _ = _make_ctx("apple-podcasts", {})
    with pytest.raises(RuntimeError, match="stalled"):
        APPLE_PODCASTS_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# BR8 regression-guard tests for apple-podcasts resolver
# ---------------------------------------------------------------------------

def test_apple_podcasts_plugin_declares_canonical_definition_name():
    """BR8: the plugin opts into the shared resolver via canonical_definition_name."""
    assert APPLE_PODCASTS_PLUGIN.canonical_definition_name == "Listened"


def test_apple_podcasts_listened_spec_shape():
    """APPLE_PODCASTS_LISTENED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import APPLE_PODCASTS_LISTENED_SPEC
    assert APPLE_PODCASTS_LISTENED_SPEC["annotation_type"] == "duration"
    ms = APPLE_PODCASTS_LISTENED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






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
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.find_timemachine_snapshots",
        lambda **_kwargs: [snap1, snap2],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.parse_db",
        fake_parse_db,
    )
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine.FulcraClient", lambda **_kwargs: fake_client)
    # BR9: _run_apple_podcasts_timemachine now reads media state; pre-populate
    # so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, st = _make_ctx("apple-podcasts-timemachine", {})
    APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run.__wrapped__(ctx)

    assert set(fake_client.calls["imported"]) == {ev1, ev2}
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
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.find_timemachine_snapshots",
        lambda **_kwargs: [snap1, snap2],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.parse_db",
        fake_parse_db,
    )
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine.FulcraClient", lambda **_kwargs: fake_client)
    # BR9: _run_apple_podcasts_timemachine now reads media state; pre-populate
    # so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine._state_load",
                        lambda path: _make_bootstrapped_media_state())

    st = PluginState("apple-podcasts-timemachine")
    ctx = RunContext(
        plugin_id="apple-podcasts-timemachine",
        config={},
        credentials={},
        state=st,
        log=_FakeLog(),
        _emit=lambda e: None,
    )
    APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == [ev1]
    assert len(log_warnings) == 1


def test_apple_podcasts_timemachine_plugin_raises_when_no_snapshots(monkeypatch):
    """When find_timemachine_snapshots returns empty, raise RuntimeError."""
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine._state_load",
                        lambda path: _make_bootstrapped_media_state())
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.find_timemachine_snapshots",
        lambda **_kwargs: [],
    )

    ctx, _ = _make_ctx("apple-podcasts-timemachine", {})
    with pytest.raises(RuntimeError, match="Time Machine"):
        APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run.__wrapped__(ctx)


# ---------------------------------------------------------------------------
# BR9 regression-guard tests for apple-podcasts-timemachine resolver
# ---------------------------------------------------------------------------

def test_apple_podcasts_timemachine_plugin_declares_canonical_definition_name():
    """BR9: the plugin opts into the shared resolver via canonical_definition_name."""
    assert APPLE_PODCASTS_TIMEMACHINE_PLUGIN.canonical_definition_name == "Listened"


def test_apple_podcasts_timemachine_listened_spec_shape():
    """APPLE_PODCASTS_TIMEMACHINE_LISTENED_SPEC must declare a duration annotation
    with a full measurement_spec so the resolver can match existing definitions."""
    from fulcra_media.collect_plugins import APPLE_PODCASTS_TIMEMACHINE_LISTENED_SPEC
    assert APPLE_PODCASTS_TIMEMACHINE_LISTENED_SPEC["annotation_type"] == "duration"
    ms = APPLE_PODCASTS_TIMEMACHINE_LISTENED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






# ---------------------------------------------------------------------------
# Generic CSV plugin
# ---------------------------------------------------------------------------

from fulcra_media.collect_plugins import GENERIC_CSV_PLUGIN  # noqa: E402
from fulcra_media.importers.generic_csv import _FP_AUTO  # noqa: E402


# ---------------------------------------------------------------------------
# media-webhook service plugin
# ---------------------------------------------------------------------------

from fulcra_media.collect_plugins import MEDIA_WEBHOOK_PLUGIN, MEDIA_WEBHOOK_WATCHED_SPEC  # noqa: E402


def test_media_webhook_plugin_metadata():
    assert MEDIA_WEBHOOK_PLUGIN.id == "media-webhook"
    assert MEDIA_WEBHOOK_PLUGIN.name == "Plex/Jellyfin webhook receiver"
    assert MEDIA_WEBHOOK_PLUGIN.kind == "service"
    perm_ids = {p.id for p in MEDIA_WEBHOOK_PLUGIN.required_permissions}
    assert "network-loopback-server" in perm_ids
    cred_keys = {c.key for c in MEDIA_WEBHOOK_PLUGIN.required_credentials}
    assert "bearer-token" in cred_keys


def test_media_webhook_plugin_run_starts_and_serves(monkeypatch):
    """run() builds the server via make_server and calls serve_forever."""
    served = []

    class _FakeServer:
        def server_close(self):
            pass
        def serve_forever(self):
            served.append(True)

    make_server_calls = {}

    class _FakeState:
        watched_definition_id = "def-uuid-123"

    def fake_make_server(*, host, port, state, client, bearer_token, log_stream):
        make_server_calls["host"] = host
        make_server_calls["port"] = port
        make_server_calls["bearer_token"] = bearer_token
        make_server_calls["log_stream"] = log_stream
        return _FakeServer()

    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook._state_load",
        lambda path: _FakeState(),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook.webhook_receiver.make_server",
        fake_make_server,
    )
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda **_kwargs: _FakeClient())

    ctx, _ = _make_ctx("media-webhook", {"host": "127.0.0.1", "port": "8765"})
    MEDIA_WEBHOOK_PLUGIN.run.__wrapped__(ctx)

    assert served == [True]
    assert make_server_calls["host"] == "127.0.0.1"
    assert make_server_calls["port"] == 8765
    assert make_server_calls["bearer_token"] is None


def test_media_webhook_plugin_run_uses_defaults(monkeypatch):
    """When host/port are absent from config, defaults 127.0.0.1:8765 are used."""
    make_server_calls = {}

    class _FakeState:
        watched_definition_id = "def-uuid-123"

    def fake_make_server(*, host, port, state, client, bearer_token, log_stream):
        make_server_calls["host"] = host
        make_server_calls["port"] = port
        return type("S", (), {"serve_forever": lambda self: None, "server_close": lambda self: None})()

    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook._state_load",
        lambda path: _FakeState(),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook.webhook_receiver.make_server",
        fake_make_server,
    )
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda **_kwargs: _FakeClient())

    ctx, _ = _make_ctx("media-webhook", {})
    MEDIA_WEBHOOK_PLUGIN.run.__wrapped__(ctx)

    assert make_server_calls["host"] == "127.0.0.1"
    assert make_server_calls["port"] == 8765


def test_media_webhook_plugin_non_loopback_without_token_raises(monkeypatch):
    """A non-loopback host with no bearer token must raise RuntimeError."""
    class _FakeState:
        watched_definition_id = "def-uuid-123"

    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook._state_load",
        lambda path: _FakeState(),
    )
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda **_kwargs: _FakeClient())

    ctx, _ = _make_ctx("media-webhook", {"host": "0.0.0.0"})
    with pytest.raises(RuntimeError, match="non-loopback"):
        MEDIA_WEBHOOK_PLUGIN.run.__wrapped__(ctx)


def test_media_webhook_plugin_declares_canonical_definition_name():
    """The plugin opts into the shared resolver via canonical_definition_name."""
    assert MEDIA_WEBHOOK_PLUGIN.canonical_definition_name == "Watched"


def test_media_webhook_watched_spec_shape():
    """MEDIA_WEBHOOK_WATCHED_SPEC must declare a duration annotation with a full
    measurement_spec so the resolver can match existing definitions."""
    assert MEDIA_WEBHOOK_WATCHED_SPEC["annotation_type"] == "duration"
    ms = MEDIA_WEBHOOK_WATCHED_SPEC["measurement_spec"]
    assert ms["measurement_type"] == "duration"
    assert ms["value_type"] == "duration"
    assert "unit" in ms  # unit may be None — presence matters for _spec_matches






def test_media_webhook_plugin_declares_host_and_topology_settings():
    """Wizard topology fix (user feedback 2026-05-26): users on a different
    machine from the daemon had no way to discover they needed host=0.0.0.0
    plus a bearer token. The plugin now declares both `host` (so the wizard
    can collect it) and a `setup_topology` navigation hint (so the setup
    flow can branch between same-machine and cross-machine setups)."""
    keys = {s.key: s for s in MEDIA_WEBHOOK_PLUGIN.required_settings}
    assert "host" in keys, "host must be declared so the wizard can collect it"
    assert keys["host"].default == "127.0.0.1"
    assert "setup_topology" in keys, (
        "setup_topology navigation hint must be declared so conditional "
        "setup_steps can branch on the user's topology choice"
    )
    topo = keys["setup_topology"]
    assert topo.kind == "enum"
    assert topo.enum_values == ("same", "lan")
    assert topo.default == "same"
    # Daemon-side: setup_topology is wizard-only; the plugin's run function
    # ignores it. Marking required=False keeps post-setup re-validation
    # quiet if the user clears it.
    assert topo.required is False


def test_media_webhook_setup_steps_branch_on_topology():
    """Verify the wizard's conditional flow: the LAN-only input step and
    each of the two external_action steps gate on setup_topology."""
    steps = MEDIA_WEBHOOK_PLUGIN.setup_steps

    # Find the LAN input step (collects host + bearer-token)
    lan_input = [
        s for s in steps
        if s.kind == "input"
        and "host" in s.settings_keys
        and "bearer-token" in s.settings_keys
    ]
    assert len(lan_input) == 1, "expected one LAN-only input step"
    assert lan_input[0].condition == {"setup_topology": ("lan",)}

    # Both external_action steps must be gated — one for same, one for lan
    ext_actions = [s for s in steps if s.kind == "external_action"]
    assert len(ext_actions) == 2, (
        "expected two external_action steps (one per topology); "
        "the legacy single hardcoded-URL step must be gone"
    )
    conditions = sorted(
        (tuple(sorted(s.condition.items())) if s.condition else ())
        for s in ext_actions
    )
    assert conditions == sorted([
        (("setup_topology", ("same",)),),
        (("setup_topology", ("lan",)),),
    ])

    # The same-machine external_action still shows the loopback URL verbatim.
    same_step = next(
        s for s in ext_actions
        if s.condition == {"setup_topology": ("same",)}
    )
    assert "127.0.0.1:8765/webhook" in same_step.body_md

    # The LAN external_action explains the ?token= query string (which is
    # how Plex's fixed webhook URL carries the bearer — see
    # webhook_receiver._authorize).
    lan_step = next(
        s for s in ext_actions
        if s.condition == {"setup_topology": ("lan",)}
    )
    assert "?token=" in lan_step.body_md
    assert "0.0.0.0" not in lan_step.body_md or "LAN IP" in lan_step.body_md, (
        "LAN step should instruct the user to substitute their Mac's LAN "
        "IP rather than hand-rolling 0.0.0.0 into the webhook URL"
    )


def test_media_webhook_plugin_run_accepts_non_loopback_with_token(monkeypatch):
    """Regression: declaring `host` in required_settings must not change the
    runtime behaviour. With host=0.0.0.0 AND bearer-token set, the server
    must start (no RuntimeError) and pass both through to make_server."""
    make_server_calls = {}

    class _FakeServer:
        def server_close(self):
            pass
        def serve_forever(self):
            pass

    class _FakeState:
        watched_definition_id = "def-uuid-123"

    def fake_make_server(*, host, port, state, client, bearer_token, log_stream):
        make_server_calls["host"] = host
        make_server_calls["port"] = port
        make_server_calls["bearer_token"] = bearer_token
        return _FakeServer()

    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook._state_load",
        lambda path: _FakeState(),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook.webhook_receiver.make_server",
        fake_make_server,
    )
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda **_kwargs: _FakeClient())

    ctx, _ = _make_ctx("media-webhook", {"host": "0.0.0.0", "port": "8765"})
    ctx.credentials["bearer-token"] = "s" * 32
    MEDIA_WEBHOOK_PLUGIN.run.__wrapped__(ctx)

    assert make_server_calls["host"] == "0.0.0.0"
    assert make_server_calls["bearer_token"] == "s" * 32


def test_generic_csv_plugin_metadata():
    assert GENERIC_CSV_PLUGIN.id == "generic-csv"
    assert GENERIC_CSV_PLUGIN.name == "Generic media CSV"
    assert GENERIC_CSV_PLUGIN.kind == "manual"
    assert GENERIC_CSV_PLUGIN.default_interval is None
    assert not GENERIC_CSV_PLUGIN.required_credentials


def test_generic_csv_plugin_run_imports_with_column_map_and_service_category(
    monkeypatch, tmp_path
):
    """run() parses the CSV, passes a ColumnMap + service/category, and runs the import."""
    from fulcra_csv import ColumnMap

    fake_csv = tmp_path / "data.csv"
    fake_csv.write_text("timestamp,title,artist,id\n")

    received = {}

    def fake_parse(path, *, service, category, column_map, tz, confidence, fingerprint_kind):
        received["service"] = service
        received["category"] = category
        received["column_map"] = column_map
        received["tz"] = tz
        received["confidence"] = confidence
        received["fingerprint_kind"] = fingerprint_kind
        return iter(["ev-csv"])

    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.library.resolve", lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_csv.parse_media_csv",
        fake_parse,
    )
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient", lambda **_kwargs: fake_client)
    # BR12: _run_generic_csv now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx(
        "generic-csv",
        {
            "path": str(fake_csv),
            "service": "myservice",
            "category": "listened",
        },
    )
    GENERIC_CSV_PLUGIN.run.__wrapped__(ctx)

    assert fake_client.calls["imported"] == ["ev-csv"]
    assert received["service"] == "myservice"
    assert received["category"] == "listened"
    assert isinstance(received["column_map"], ColumnMap)
    # Defaults: ts_col=timestamp, title_col=title, subtitle_col=artist, id_col=id
    assert received["column_map"].timestamp == "timestamp"
    assert received["column_map"].title == "title"
    assert received["column_map"].subtitle == "artist"
    assert received["column_map"].source_id == "id"
    # tz default is UTC (timezone.utc)
    from datetime import timezone
    assert received["tz"] is timezone.utc
    assert received["confidence"] == "medium"
    # fingerprint default is "auto" → _FP_AUTO sentinel
    assert received["fingerprint_kind"] is _FP_AUTO


def test_generic_csv_plugin_raises_when_path_missing():
    ctx, _ = _make_ctx("generic-csv", {"service": "svc", "category": "watched"})
    with pytest.raises(RuntimeError, match="path"):
        GENERIC_CSV_PLUGIN.run.__wrapped__(ctx)


def test_generic_csv_plugin_raises_when_service_missing():
    ctx, _ = _make_ctx("generic-csv", {"path": "/tmp/x.csv", "category": "watched"})
    with pytest.raises(RuntimeError, match="service"):
        GENERIC_CSV_PLUGIN.run.__wrapped__(ctx)


def test_generic_csv_plugin_raises_when_category_missing():
    ctx, _ = _make_ctx("generic-csv", {"path": "/tmp/x.csv", "service": "svc"})
    with pytest.raises(RuntimeError, match="category"):
        GENERIC_CSV_PLUGIN.run.__wrapped__(ctx)


def test_generic_csv_plugin_fingerprint_none_maps_to_none(monkeypatch, tmp_path):
    """fingerprint='none' must pass None as fingerprint_kind to parse_media_csv."""
    fake_csv = tmp_path / "data.csv"
    fake_csv.write_text("timestamp,title,artist,id\n")

    received = {}
    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.library.resolve", lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_csv.parse_media_csv",
        lambda path, *, service, category, column_map, tz, confidence, fingerprint_kind: (
            received.update({"fingerprint_kind": fingerprint_kind}) or iter([])
        ),
    )
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient", lambda **_kwargs: fake_client)
    # BR12: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx(
        "generic-csv",
        {"path": str(fake_csv), "service": "svc", "category": "watched", "fingerprint": "none"},
    )
    GENERIC_CSV_PLUGIN.run.__wrapped__(ctx)

    assert received["fingerprint_kind"] is None


def test_generic_csv_plugin_fingerprint_auto_maps_to_fp_auto(monkeypatch, tmp_path):
    """fingerprint='auto' (the default) must pass _FP_AUTO as fingerprint_kind."""
    fake_csv = tmp_path / "data.csv"
    fake_csv.write_text("timestamp,title,artist,id\n")

    received = {}
    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.library.resolve", lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_csv.parse_media_csv",
        lambda path, *, service, category, column_map, tz, confidence, fingerprint_kind: (
            received.update({"fingerprint_kind": fingerprint_kind}) or iter([])
        ),
    )
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient", lambda **_kwargs: fake_client)
    # BR12: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx(
        "generic-csv",
        {"path": str(fake_csv), "service": "svc", "category": "watched", "fingerprint": "auto"},
    )
    GENERIC_CSV_PLUGIN.run.__wrapped__(ctx)

    assert received["fingerprint_kind"] is _FP_AUTO


def test_generic_csv_plugin_fingerprint_explicit_passes_through(monkeypatch, tmp_path):
    """fingerprint='music' (or any explicit kind) passes the string through unchanged."""
    fake_csv = tmp_path / "data.csv"
    fake_csv.write_text("timestamp,title,artist,id\n")

    received = {}
    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.library.resolve", lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_csv.parse_media_csv",
        lambda path, *, service, category, column_map, tz, confidence, fingerprint_kind: (
            received.update({"fingerprint_kind": fingerprint_kind}) or iter([])
        ),
    )
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient", lambda **_kwargs: fake_client)
    # BR12: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx(
        "generic-csv",
        {"path": str(fake_csv), "service": "svc", "category": "watched", "fingerprint": "music"},
    )
    GENERIC_CSV_PLUGIN.run.__wrapped__(ctx)

    assert received["fingerprint_kind"] == "music"


# ---------------------------------------------------------------------------
# BR12 regression-guard tests for generic-csv resolver
# ---------------------------------------------------------------------------

def test_generic_csv_plugin_canonical_definition_name_is_none():
    """BR12: generic-csv has no static canonical name — it depends on runtime
    config.  The Plugin object must NOT declare canonical_definition_name."""
    assert GENERIC_CSV_PLUGIN.canonical_definition_name is None








# ---------------------------------------------------------------------------
# GAP 12 — _run_trakt OAuth credentials path
# ---------------------------------------------------------------------------

def test_run_trakt_uses_keychain_oauth_credentials_when_available(monkeypatch):
    """When ctx.credentials has access_token + client_id from the OAuth wizard
    path, _run_trakt calls fetch_history_with_headers with those values instead
    of falling back to the legacy file-based device-flow auth.

    This is the primary production path for new users who onboard via the web
    UI OAuth wizard; was previously untested."""
    captured_headers: list[dict] = []

    def fake_fetch_with_headers(api_headers, *, strict_scan, _bounds, _before_request):
        assert strict_scan is True
        assert _bounds is not None
        captured_headers.append(dict(api_headers))
        return iter([])

    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history_with_headers",
        fake_fetch_with_headers,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.apply_cluster_policy",
        lambda events, policy: events,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins._common.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda **_kwargs: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso",
        lambda events: None,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt._state_load",
        lambda path: _make_bootstrapped_media_state(),
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def prepare_media(self, categories):
            pass
        def __init__(self, **_kwargs):
            pass
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None): return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient", lambda **_kwargs: FakeClient())

    st = PluginState("trakt")
    ctx = RunContext(
        plugin_id="trakt",
        config={},
        credentials={"access_token": "my-access-tok", "client_id": "my-client-id"},
        state=st,
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    _bind_trakt_policy_context(ctx)
    TRAKT_PLUGIN.run.__wrapped__(ctx)

    # fetch_history_with_headers must have been called (not the legacy fetch_history)
    assert len(captured_headers) == 1, "fetch_history_with_headers should be called once"
    headers = captured_headers[0]
    assert headers["Authorization"] == "Bearer my-access-tok"
    assert headers["trakt-api-key"] == "my-client-id"
    assert headers["trakt-api-version"] == "2"


# ---------------------------------------------------------------------------
# GAP 14 — annotation events emit verification
# ---------------------------------------------------------------------------



def test_since_from_watermark_rewinds_24h():
    """Late-arriving scrobbles: a phone listening offline uploads its plays to
    Last.fm when the app next foregrounds — hours, not minutes, late. A 1-hour
    rewind permanently missed those (confirmed live: a 2026-06-07 15:38
    scrobble absent from Fulcra while its 14:34 sibling landed). The rewind is
    24h; det-id readback makes the wider refetch idempotent and it costs at
    most ~one extra API page per run.
    """
    from fulcra_media.plugins._common import since_from_watermark

    class St:
        watermark = "2026-06-07T18:00:00Z"

    class Ctx:
        state = St()

    out = since_from_watermark(Ctx())
    assert out is not None
    assert out.isoformat() == "2026-06-06T18:00:00+00:00"


def test_media_webhook_setup_exposes_custom_receiver_port_for_both_topologies():
    settings = {setting.key: setting for setting in MEDIA_WEBHOOK_PLUGIN.required_settings}
    assert settings["port"].kind == "port"
    assert settings["port"].default == 8765
    port_steps = [step for step in MEDIA_WEBHOOK_PLUGIN.setup_steps if "port" in step.settings_keys]
    assert len(port_steps) == 1
    assert port_steps[0].condition is None
    for step in MEDIA_WEBHOOK_PLUGIN.setup_steps:
        if step.kind == "external_action":
            assert "Replace `8765`" in step.body_md
