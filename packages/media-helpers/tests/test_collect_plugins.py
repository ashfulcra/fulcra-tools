"""The Last.fm and file-based fulcra-collect plugins."""
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

def test_run_fetches_normalizes_imports_and_advances_watermark(monkeypatch):
    calls = {}

    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: ["event-1"])

    class FakeResult:
        posted = 1
        skipped_existing = 0
        verified = 1

    class FakeClient:
        def ensure_tag(self, name, state):
            calls["ensure_tag"] = name
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            calls["imported"] = list(events)
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        lambda: FakeClient())
    monkeypatch.setattr("fulcra_media.plugins.lastfm.newest_event_iso",
                        lambda events: "2026-05-22T12:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _make_bootstrapped_media_state())

    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "test_user"},
                     credentials={"api-key": "K"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    assert calls["imported"] == ["event-1"]
    assert calls["ensure_tag"] == "lastfm"
    assert st.watermark == "2026-05-22T12:00:00Z"


def test_run_raises_a_clear_error_when_the_api_key_is_missing(monkeypatch):
    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "test_user"},
                     credentials={},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    with pytest.raises(RuntimeError, match="api-key"):
        LASTFM_PLUGIN.run(ctx)


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
        LASTFM_PLUGIN.run(ctx)


def test_run_forwards_username_and_api_key_to_the_importer(monkeypatch):
    """Companion to the regression above: when both credential + setting are
    present, the importer receives both — keyed exactly as it expects
    (`api_key` and `username`).
    """
    captured = {}

    def fake_fetch(creds, since, max_pages):
        captured["creds"] = creds
        return []  # no scrobbles, nothing else to do

    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        fake_fetch)
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        lambda: FakeClient())
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _make_bootstrapped_media_state())

    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "alice"},
                     credentials={"api-key": "secret"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    assert captured["creds"] == {"api_key": "secret", "username": "alice"}


def test_run_advances_watermark_on_all_duplicate_window(monkeypatch):
    """Finding 10: the watermark must advance even when every fetched event
    was already in Fulcra (posted == 0, skipped_existing > 0).

    The 1-hour rewind in `_since_from_watermark` re-fetches an overlapping
    window every run, so the steady state is posted == 0. Freezing the
    watermark there means the re-fetched window grows without bound any
    time the user goes quiet — this asserts the freeze is gone.
    """
    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: ["event-1"])

    class AllDupResult:
        posted = 0          # nothing new
        skipped_existing = 1  # already in Fulcra
        verified = 1

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return AllDupResult()

    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        lambda: FakeClient())
    monkeypatch.setattr("fulcra_media.plugins.lastfm.newest_event_iso",
                        lambda events: "2026-05-22T12:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _make_bootstrapped_media_state())

    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "test_user"},
                     credentials={"api-key": "K"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    # Every event was successfully processed (skipped_existing means already
    # ingested into Fulcra), so the watermark must reflect that progress.
    assert st.watermark == "2026-05-22T12:00:00Z"


def test_run_threads_ctx_claim_dedup_keys_into_run_import(monkeypatch):
    """Component 3 seam: the scheduled media plugin must pass the daemon's
    per-event write-dedup claim (ctx.claim_dedup_keys) down into run_import,
    so concurrent runs / same-run cross-source twins can't double-write.
    Regression guard against the claim quietly dropping out of the call
    chain (plugin → _common.run_scheduled_import → run_import)."""
    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: ["event-1"])

    seen = {}

    class FakeResult:
        posted = 1
        skipped_existing = 0
        verified = 1

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            seen["claim"] = claim
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        lambda: FakeClient())
    monkeypatch.setattr("fulcra_media.plugins.lastfm.newest_event_iso",
                        lambda events: "2026-05-22T12:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _make_bootstrapped_media_state())

    claim_calls = []

    def fake_claim(keys: set[str]) -> bool:
        claim_calls.append(keys)
        return True

    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "test_user"},
                     credentials={"api-key": "K"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None,
                     _claim_dedup_keys=fake_claim)
    LASTFM_PLUGIN.run(ctx)

    # A claim WAS handed to run_import (not None), and invoking it routes
    # through to our injected backend — proving ctx.claim_dedup_keys was
    # threaded down rather than dropped. (Bound methods aren't identity-equal
    # across accesses, so we assert behaviour, not object identity.)
    assert seen["claim"] is not None
    assert seen["claim"]({"k"}) is True
    assert claim_calls == [{"k"}]


# ---------------------------------------------------------------------------
# R6 resolver path (no pre-existing listened_definition_id)
# ---------------------------------------------------------------------------

def test_run_uses_resolver_when_listened_definition_not_bootstrapped(monkeypatch):
    """R6 regression: when listened_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), run() must call
    ctx.resolved_definition_id rather than raise RuntimeError.

    The resolver is mocked at the RunContext level: we supply a
    _fulcra_client_factory whose client returns a known id. After run()
    completes, the media state must be persisted with that id so subsequent
    runs (and other importers that share the "Listened" definition) find it."""
    # Media state starts empty (no bootstrap)
    empty_media_state = _make_empty_media_state()
    saved_states: list[MediaState] = []

    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_save",
                        lambda state, path=None: saved_states.append(state))

    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        lambda: FakeClient())

    # Resolver fake: list_definitions returns nothing → create_definition called
    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-resolver-new-listened"}

    fake_def_client = _FakeDefinitionClient()

    # Give ctx.state a PluginState-like object so resolved_definition_id can
    # cache the id there (it writes ctx.state.definition_id). It also needs a
    # watermark attribute because _since_from_watermark reads ctx.state.watermark.
    class _FakePluginState:
        definition_id: str | None = None
        watermark: str | None = None

    ctx = RunContext(
        plugin_id="lastfm",
        config={"username": "test_user"},
        credentials={"api-key": "K"},
        state=_FakePluginState(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    LASTFM_PLUGIN.run(ctx)

    # Resolver was used with the canonical name "Listened"
    assert fake_def_client.list_calls == ["Listened"]
    assert fake_def_client.create_calls[0]["name"] == "Listened"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"

    # The resolved id was written into the media state
    assert empty_media_state.listened_definition_id == "def-resolver-new-listened"

    # The media state was persisted to disk
    assert len(saved_states) == 1
    assert saved_states[0].listened_definition_id == "def-resolver-new-listened"


def test_run_does_not_call_resolver_when_definition_already_bootstrapped(monkeypatch):
    """When listened_definition_id is already in the media state (bootstrap
    has been run), the resolver must NOT be called — no network trip."""
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _make_bootstrapped_media_state())
    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    st = PluginState("lastfm")
    ctx = RunContext(
        plugin_id="lastfm",
        config={"username": "test_user"},
        credentials={"api-key": "K"},
        state=st,
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    # Intercept resolved_definition_id at the RunContext level
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    LASTFM_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
                        lambda: fake_client)
    # R8: _run_netflix now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.netflix._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("netflix", {"path": str(fake_csv)})
    NETFLIX_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-netflix"]
    assert fake_client.calls["ensure_tag"] == "netflix"


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
        NETFLIX_PLUGIN.run(ctx)


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


def test_netflix_uses_resolver_when_watched_definition_not_bootstrapped(
    monkeypatch, tmp_path
):
    """R8 regression: when watched_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_netflix must call
    ctx.resolved_definition_id rather than proceed without a definition ID.

    After run() completes, the media state must be persisted with the resolved
    id so subsequent runs (and other Watched plugins) find it."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.netflix._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.netflix._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_csv = tmp_path / "viewing.csv"
    fake_csv.write_text("Title,Date\n")
    monkeypatch.setattr("fulcra_media.plugins.netflix.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.netflix.netflix_importer.parse_auto",
                        lambda path: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.netflix.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-netflix-watched"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="netflix",
        config={"path": str(fake_csv)},
        credentials={},
        state=PluginState("netflix"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    NETFLIX_PLUGIN.run(ctx)

    # Resolver was invoked with the canonical name "Watched"
    assert fake_def_client.list_calls == ["Watched"]
    assert fake_def_client.create_calls[0]["name"] == "Watched"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"

    # The resolved id was written into the shared media state
    assert empty_media_state.watched_definition_id == "def-netflix-watched"

    # The media state was persisted to disk
    assert len(saved_states) == 1
    assert saved_states[0].watched_definition_id == "def-netflix-watched"


def test_netflix_does_not_call_resolver_when_definition_already_bootstrapped(
    monkeypatch, tmp_path
):
    """When watched_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.netflix._state_load",
                        lambda path: _make_bootstrapped_media_state())

    fake_csv = tmp_path / "viewing.csv"
    fake_csv.write_text("Title,Date\n")
    monkeypatch.setattr("fulcra_media.plugins.netflix.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.netflix.netflix_importer.parse_auto",
                        lambda path: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.netflix.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="netflix",
        config={"path": str(fake_csv)},
        credentials={},
        state=PluginState("netflix"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    NETFLIX_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
                        lambda: fake_client)
    # R7: _run_spotify_extended now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("spotify-extended", {"path": str(fake_zip)})
    SPOTIFY_EXTENDED_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-spotify"]
    assert fake_client.calls["ensure_tag"] == "spotify"


def test_spotify_extended_plugin_raises_without_path(monkeypatch):
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended._state_load",
                        lambda path: _make_bootstrapped_media_state())
    ctx, _ = _make_ctx("spotify-extended", {})
    with pytest.raises(RuntimeError, match="path"):
        SPOTIFY_EXTENDED_PLUGIN.run(ctx)


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


def test_run_uses_resolver_when_listened_definition_not_bootstrapped_spotify_extended(
    monkeypatch, tmp_path
):
    """R7 regression: when listened_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_spotify_extended must call
    ctx.resolved_definition_id rather than proceed without a definition ID.

    After run() completes, the media state must be persisted with the resolved
    id so subsequent runs (and lastfm, which shares the same field) find it."""
    empty_media_state = _make_empty_media_state()
    saved_states: list[MediaState] = []

    monkeypatch.setattr("fulcra_media.plugins.spotify_extended._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_zip = tmp_path / "spotify.zip"
    fake_zip.write_bytes(b"PK")
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended.spotify_importer.parse_extended_zip",
                        lambda path: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.spotify_extended.FulcraClient",
                        lambda: FakeClient())

    # Resolver fake: list_definitions returns nothing → create_definition called
    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-resolver-new-listened-sp"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="spotify-extended",
        config={"path": str(fake_zip)},
        credentials={},
        state=PluginState("spotify-extended"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    SPOTIFY_EXTENDED_PLUGIN.run(ctx)

    # Resolver was invoked with the canonical name "Listened"
    assert fake_def_client.list_calls == ["Listened"]
    assert fake_def_client.create_calls[0]["name"] == "Listened"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"

    # The resolved id was written into the shared media state
    assert empty_media_state.listened_definition_id == "def-resolver-new-listened-sp"

    # The media state was persisted to disk
    assert len(saved_states) == 1
    assert saved_states[0].listened_definition_id == "def-resolver-new-listened-sp"


def test_run_does_not_call_resolver_when_definition_already_bootstrapped_spotify_extended(
    monkeypatch, tmp_path
):
    """When listened_definition_id is already in the media state (bootstrap or
    a prior resolver run has populated it), the resolver must NOT be called —
    no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended._state_load",
                        lambda path: _make_bootstrapped_media_state())

    fake_zip = tmp_path / "spotify.zip"
    fake_zip.write_bytes(b"PK")
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.spotify_extended.spotify_importer.parse_extended_zip",
                        lambda path: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.spotify_extended.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="spotify-extended",
        config={"path": str(fake_zip)},
        credentials={},
        state=PluginState("spotify-extended"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    SPOTIFY_EXTENDED_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
                        lambda: fake_client)
    # R8: _run_youtube now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.youtube._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("youtube", {"path": str(fake_json)})
    YOUTUBE_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-youtube"]
    assert fake_client.calls["ensure_tag"] == "youtube"


def test_youtube_plugin_raises_without_path(monkeypatch):
    monkeypatch.setattr("fulcra_media.plugins.youtube._state_load",
                        lambda path: _make_bootstrapped_media_state())
    ctx, _ = _make_ctx("youtube", {})
    with pytest.raises(RuntimeError, match="path"):
        YOUTUBE_PLUGIN.run(ctx)


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


def test_youtube_uses_resolver_when_watched_definition_not_bootstrapped(
    monkeypatch, tmp_path
):
    """R8 regression: when watched_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_youtube must call
    ctx.resolved_definition_id rather than proceed without a definition ID."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.youtube._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.youtube._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_json = tmp_path / "watch-history.json"
    fake_json.write_text("[]")
    monkeypatch.setattr("fulcra_media.plugins.youtube.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.youtube.youtube_importer.parse_takeout_json",
                        lambda path: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.youtube.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-youtube-watched"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="youtube",
        config={"path": str(fake_json)},
        credentials={},
        state=PluginState("youtube"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    YOUTUBE_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Watched"]
    assert fake_def_client.create_calls[0]["name"] == "Watched"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"
    assert empty_media_state.watched_definition_id == "def-youtube-watched"
    assert len(saved_states) == 1
    assert saved_states[0].watched_definition_id == "def-youtube-watched"


def test_youtube_does_not_call_resolver_when_definition_already_bootstrapped(
    monkeypatch, tmp_path
):
    """When watched_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.youtube._state_load",
                        lambda path: _make_bootstrapped_media_state())

    fake_json = tmp_path / "watch-history.json"
    fake_json.write_text("[]")
    monkeypatch.setattr("fulcra_media.plugins.youtube.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.youtube.youtube_importer.parse_takeout_json",
                        lambda path: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.youtube.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="youtube",
        config={"path": str(fake_json)},
        credentials={},
        state=PluginState("youtube"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    YOUTUBE_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
                        lambda: fake_client)
    # BR7: _run_spotify_ifttt now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt._state_load",
                        lambda path: _make_bootstrapped_media_state())

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
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.spotify_ifttt.spotify_ifttt_importer.parse_ifttt_zip",
        lambda path, tz: (received_tz.update({"tz": tz}) or []),
    )
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.FulcraClient",
                        lambda: fake_client)
    # BR7: _run_spotify_ifttt now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("spotify-ifttt", {"path": str(fake_zip)})
    SPOTIFY_IFTTT_PLUGIN.run(ctx)

    assert received_tz["tz"] == ZoneInfo("UTC")


def test_spotify_ifttt_plugin_raises_without_path(monkeypatch):
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt._state_load",
                        lambda path: _make_bootstrapped_media_state())
    ctx, _ = _make_ctx("spotify-ifttt", {})
    with pytest.raises(RuntimeError, match="path"):
        SPOTIFY_IFTTT_PLUGIN.run(ctx)


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


def test_spotify_ifttt_uses_resolver_when_listened_definition_not_bootstrapped(
    monkeypatch, tmp_path
):
    """BR7 regression: when listened_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_spotify_ifttt must call
    ctx.resolved_definition_id rather than proceed without a definition ID.

    Uses the adoption path: fake client returns an existing def so the
    resolver adopts it without creating a duplicate."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_zip = tmp_path / "ifttt.zip"
    fake_zip.write_bytes(b"PK")
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.spotify_ifttt_importer.parse_ifttt_zip",
                        lambda path, tz: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-spotify-ifttt-listened"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="spotify-ifttt",
        config={"path": str(fake_zip)},
        credentials={},
        state=PluginState("spotify-ifttt"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    SPOTIFY_IFTTT_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Listened"]
    assert fake_def_client.create_calls[0]["name"] == "Listened"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"
    assert empty_media_state.listened_definition_id == "def-spotify-ifttt-listened"
    assert len(saved_states) == 1
    assert saved_states[0].listened_definition_id == "def-spotify-ifttt-listened"


def test_spotify_ifttt_does_not_call_resolver_when_definition_already_bootstrapped(
    monkeypatch, tmp_path
):
    """When listened_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    fake_zip = tmp_path / "ifttt.zip"
    fake_zip.write_bytes(b"PK")
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.spotify_ifttt_importer.parse_ifttt_zip",
                        lambda path, tz: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.spotify_ifttt.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="spotify-ifttt",
        config={"path": str(fake_zip)},
        credentials={},
        state=PluginState("spotify-ifttt"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    SPOTIFY_IFTTT_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
                        lambda: fake_client)
    # R8: _run_apple_takeout now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("apple-takeout", {"path": str(fake_csv)})
    APPLE_TAKEOUT_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-apple"]
    assert fake_client.calls["ensure_tag"] == "apple-tv"


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
                        lambda: fake_client)
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("apple-takeout", {"path": str(tmp_path)})
    APPLE_TAKEOUT_PLUGIN.run(ctx)

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
        APPLE_TAKEOUT_PLUGIN.run(ctx)


def test_apple_takeout_plugin_raises_without_path(monkeypatch):
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda path: _make_bootstrapped_media_state())
    ctx, _ = _make_ctx("apple-takeout", {})
    with pytest.raises(RuntimeError, match="path"):
        APPLE_TAKEOUT_PLUGIN.run(ctx)


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


def test_apple_takeout_uses_resolver_when_watched_definition_not_bootstrapped(
    monkeypatch, tmp_path
):
    """R8 regression: when watched_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_apple_takeout must call
    ctx.resolved_definition_id rather than proceed without a definition ID."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_save",
                        lambda state, path=None: saved_states.append(state))

    fake_csv = tmp_path / "Playback Activity.csv"
    fake_csv.write_text("header\n")
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.apple_takeout_importer.parse_any",
                        lambda path, *, since=None, until=None: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-apple-takeout-watched"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="apple-takeout",
        config={"path": str(fake_csv)},
        credentials={},
        state=PluginState("apple-takeout"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    APPLE_TAKEOUT_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Watched"]
    assert fake_def_client.create_calls[0]["name"] == "Watched"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"
    assert empty_media_state.watched_definition_id == "def-apple-takeout-watched"
    assert len(saved_states) == 1
    assert saved_states[0].watched_definition_id == "def-apple-takeout-watched"


def test_apple_takeout_does_not_call_resolver_when_definition_already_bootstrapped(
    monkeypatch, tmp_path
):
    """When watched_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda path: _make_bootstrapped_media_state())

    fake_csv = tmp_path / "Playback Activity.csv"
    fake_csv.write_text("header\n")
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.library.resolve",
                        lambda p: Path(p))
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.apple_takeout_importer.parse_any",
                        lambda path, *, since=None, until=None: [])

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="apple-takeout",
        config={"path": str(fake_csv)},
        credentials={},
        state=PluginState("apple-takeout"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    APPLE_TAKEOUT_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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


def test_generic_rss_plugin_run_imports_and_advances_watermark(monkeypatch):
    ev = _make_event("2026-05-22T10:00:00+00:00")

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter([ev]),
    )
    monkeypatch.setattr("fulcra_media.plugins.generic_rss.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # BR11: _run_generic_rss now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_rss._state_load",
                        lambda path: _make_bootstrapped_media_state())

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
        "fulcra_media.plugins.generic_rss.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter([old_ev, new_ev]),
    )
    monkeypatch.setattr("fulcra_media.plugins.generic_rss.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # BR11: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_rss._state_load",
                        lambda path: _make_bootstrapped_media_state())

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
        "fulcra_media.plugins.generic_rss.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter(events),
    )
    monkeypatch.setattr("fulcra_media.plugins.generic_rss.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.newest_event_iso",
        lambda evs: "2026-05-22T10:00:00+00:00",
    )
    # BR11: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_rss._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx(
        "generic-rss",
        {"feed_url": "https://example.com/f.rss", "service": "s", "category": "watched",
         "max_entries": 2},
    )
    GENERIC_RSS_PLUGIN.run(ctx)

    assert len(fake_client.calls["imported"]) == 2


def test_generic_rss_plugin_newest_first_feed_keeps_oldest_block(monkeypatch):
    """Finding 10b: when the underlying feed is newest-first and `max_entries`
    is set, the cap must keep the *oldest* events — not the newest. Otherwise
    older history is permanently un-imported because the watermark advances
    past it on the very next run.
    """
    newest = _make_event("2026-05-22T12:00:00+00:00")
    middle = _make_event("2026-05-22T11:00:00+00:00")
    oldest = _make_event("2026-05-22T10:00:00+00:00")

    fake_client = _FakeClient()
    # Newest-first ordering, as many real RSS feeds emit.
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter([newest, middle, oldest]),
    )
    monkeypatch.setattr("fulcra_media.plugins.generic_rss.FulcraClient", lambda: fake_client)
    # Don't stub newest_event_iso — let the real one run on what we actually imported,
    # so the watermark assertion below proves we advanced to a *safe* timestamp.
    # BR11: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_rss._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, st = _make_ctx(
        "generic-rss",
        {"feed_url": "https://example.com/f.rss", "service": "s", "category": "watched",
         "max_entries": 2},
    )
    GENERIC_RSS_PLUGIN.run(ctx)

    # The two oldest events were imported (a contiguous oldest-block); newest is deferred.
    assert fake_client.calls["imported"] == [oldest, middle]
    # The watermark advances to the newest of what we imported — middle, not newest.
    # Next run will see `newest` (which is now older than nothing imported above) again.
    assert st.watermark == "2026-05-22T11:00:00+00:00"


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
# BR11 regression-guard tests for generic-rss resolver
# ---------------------------------------------------------------------------

def test_generic_rss_plugin_canonical_definition_name_is_none():
    """BR11: generic-rss has no static canonical name — it depends on runtime
    config.  The Plugin object must NOT declare canonical_definition_name."""
    assert GENERIC_RSS_PLUGIN.canonical_definition_name is None


def test_generic_rss_uses_resolver_for_watched_category(monkeypatch):
    """BR11 regression: when category='watched' and watched_definition_id is
    absent, the resolver is called with canonical_name='Watched'."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.generic_rss._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.generic_rss._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter([]),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.newest_event_iso",
        lambda events: None,
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.generic_rss.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-generic-rss-watched"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="generic-rss",
        config={"feed_url": "https://x.com/f.rss", "service": "s", "category": "watched"},
        credentials={},
        state=PluginState("generic-rss"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    GENERIC_RSS_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Watched"]
    assert fake_def_client.create_calls[0]["name"] == "Watched"
    assert empty_media_state.watched_definition_id == "def-generic-rss-watched"
    assert len(saved_states) == 1
    assert saved_states[0].watched_definition_id == "def-generic-rss-watched"


def test_generic_rss_uses_resolver_for_listened_category(monkeypatch):
    """BR11 regression: when category='listened' and listened_definition_id is
    absent, the resolver is called with canonical_name='Listened'."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.generic_rss._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.generic_rss._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter([]),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.newest_event_iso",
        lambda events: None,
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.generic_rss.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-generic-rss-listened"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="generic-rss",
        config={"feed_url": "https://x.com/f.rss", "service": "s", "category": "listened"},
        credentials={},
        state=PluginState("generic-rss"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    GENERIC_RSS_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Listened"]
    assert fake_def_client.create_calls[0]["name"] == "Listened"
    assert empty_media_state.listened_definition_id == "def-generic-rss-listened"
    assert len(saved_states) == 1
    assert saved_states[0].listened_definition_id == "def-generic-rss-listened"


def test_generic_rss_does_not_call_resolver_when_definition_already_bootstrapped(monkeypatch):
    """When the target definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.generic_rss._state_load",
                        lambda path: _make_bootstrapped_media_state())
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.rss_importer.normalize_feed",
        lambda feed_url, service, category: iter([]),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_rss.newest_event_iso",
        lambda events: None,
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.generic_rss.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="generic-rss",
        config={"feed_url": "https://x.com/f.rss", "service": "s", "category": "watched"},
        credentials={},
        state=PluginState("generic-rss"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    GENERIC_RSS_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
        "fulcra_media.plugins.letterboxd.lb_importer.fetch_diary",
        lambda username: iter([ev]),
    )
    monkeypatch.setattr("fulcra_media.plugins.letterboxd.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.letterboxd.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # R8: _run_letterboxd now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.letterboxd._state_load",
                        lambda path: _make_bootstrapped_media_state())

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
        "fulcra_media.plugins.letterboxd.lb_importer.fetch_diary",
        lambda username: iter([old_ev, new_ev]),
    )
    monkeypatch.setattr("fulcra_media.plugins.letterboxd.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.letterboxd.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    monkeypatch.setattr("fulcra_media.plugins.letterboxd._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, st = _make_ctx("letterboxd", {"username": "johndoe"})
    st.watermark = "2026-05-21T00:00:00+00:00"
    LETTERBOXD_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [new_ev]


def test_letterboxd_plugin_raises_without_username():
    ctx, _ = _make_ctx("letterboxd", {})
    with pytest.raises(RuntimeError, match="username"):
        LETTERBOXD_PLUGIN.run(ctx)


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


def test_letterboxd_uses_resolver_when_watched_definition_not_bootstrapped(monkeypatch):
    """R8 regression: when watched_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_letterboxd must call
    ctx.resolved_definition_id rather than proceed without a definition ID."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.letterboxd._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.letterboxd._state_save",
                        lambda state, path=None: saved_states.append(state))

    monkeypatch.setattr(
        "fulcra_media.plugins.letterboxd.lb_importer.fetch_diary",
        lambda username: iter([]),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.letterboxd.newest_event_iso",
        lambda events: None,
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.letterboxd.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-letterboxd-watched"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="letterboxd",
        config={"username": "johndoe"},
        credentials={},
        state=PluginState("letterboxd"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    LETTERBOXD_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Watched"]
    assert fake_def_client.create_calls[0]["name"] == "Watched"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"
    assert empty_media_state.watched_definition_id == "def-letterboxd-watched"
    assert len(saved_states) == 1
    assert saved_states[0].watched_definition_id == "def-letterboxd-watched"


def test_letterboxd_does_not_call_resolver_when_definition_already_bootstrapped(monkeypatch):
    """When watched_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.letterboxd._state_load",
                        lambda path: _make_bootstrapped_media_state())
    monkeypatch.setattr(
        "fulcra_media.plugins.letterboxd.lb_importer.fetch_diary",
        lambda username: iter([]),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.letterboxd.newest_event_iso",
        lambda events: None,
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.letterboxd.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="letterboxd",
        config={"username": "johndoe"},
        credentials={},
        state=PluginState("letterboxd"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    LETTERBOXD_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
        "fulcra_media.plugins.goodreads.gr_importer.fetch_diary",
        lambda user_id: iter([ev]),
    )
    monkeypatch.setattr("fulcra_media.plugins.goodreads.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.goodreads.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # BR10: _run_goodreads now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.goodreads._state_load",
                        lambda path: _make_bootstrapped_media_state())

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
        "fulcra_media.plugins.goodreads.gr_importer.fetch_diary",
        lambda user_id: iter([old_ev, new_ev]),
    )
    monkeypatch.setattr("fulcra_media.plugins.goodreads.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.goodreads.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # BR10: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.goodreads._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, st = _make_ctx("goodreads", {"user_id": "12345"})
    st.watermark = "2026-05-21T00:00:00+00:00"
    GOODREADS_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [new_ev]


def test_goodreads_plugin_raises_without_user_id():
    ctx, _ = _make_ctx("goodreads", {})
    with pytest.raises(RuntimeError, match="user_id"):
        GOODREADS_PLUGIN.run(ctx)


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


def test_goodreads_uses_resolver_when_read_definition_not_bootstrapped(monkeypatch):
    """BR10 regression: when read_definition_id is absent from the media state
    file (machine 2 never ran bootstrap), _run_goodreads must call
    ctx.resolved_definition_id rather than proceed without a definition ID."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.goodreads._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.goodreads._state_save",
                        lambda state, path=None: saved_states.append(state))

    monkeypatch.setattr(
        "fulcra_media.plugins.goodreads.gr_importer.fetch_diary",
        lambda user_id: iter([]),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.goodreads.newest_event_iso",
        lambda events: None,
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.goodreads.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-goodreads-read"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="goodreads",
        config={"user_id": "12345"},
        credentials={},
        state=PluginState("goodreads"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    GOODREADS_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Read"]
    assert fake_def_client.create_calls[0]["name"] == "Read"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"
    assert empty_media_state.read_definition_id == "def-goodreads-read"
    assert len(saved_states) == 1
    assert saved_states[0].read_definition_id == "def-goodreads-read"


def test_goodreads_does_not_call_resolver_when_definition_already_bootstrapped(monkeypatch):
    """When read_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.goodreads._state_load",
                        lambda path: _make_bootstrapped_media_state())
    monkeypatch.setattr(
        "fulcra_media.plugins.goodreads.gr_importer.fetch_diary",
        lambda user_id: iter([]),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.goodreads.newest_event_iso",
        lambda events: None,
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.goodreads.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="goodreads",
        config={"user_id": "12345"},
        credentials={},
        state=PluginState("goodreads"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    GOODREADS_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
        "fulcra_media.plugins.deezer.deezer_importer.fetch_history",
        fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.deezer.deezer_importer.normalize_history",
        lambda raw: ["ev-deezer"],
    )
    monkeypatch.setattr("fulcra_media.plugins.deezer.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.deezer.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # BR6: _run_deezer now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.deezer._state_load",
                        lambda path: _make_bootstrapped_media_state())

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
    from datetime import datetime, timezone

    received_since = {}

    def fake_fetch(creds, since, max_pages):
        received_since["since"] = since
        return []

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.plugins.deezer.deezer_importer.fetch_history",
        fake_fetch,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.deezer.deezer_importer.normalize_history",
        lambda raw: [],
    )
    monkeypatch.setattr("fulcra_media.plugins.deezer.FulcraClient", lambda: fake_client)
    # BR6: _run_deezer now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.deezer._state_load",
                        lambda path: _make_bootstrapped_media_state())

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


def test_deezer_uses_resolver_when_listened_definition_not_bootstrapped(monkeypatch):
    """BR6 regression: when listened_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_deezer must call
    ctx.resolved_definition_id rather than proceed without a definition ID.

    Uses the creation path: fake client returns no existing def so the
    resolver creates a new one."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.deezer._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.deezer._state_save",
                        lambda state, path=None: saved_states.append(state))

    monkeypatch.setattr(
        "fulcra_media.plugins.deezer.deezer_importer.fetch_history",
        lambda creds, since, max_pages: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.deezer.deezer_importer.normalize_history",
        lambda raw: [],
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.deezer.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-deezer-listened"}

    fake_def_client = _FakeDefinitionClient()

    class _FakePluginState:
        definition_id: str | None = None
        watermark: str | None = None

    ctx = RunContext(
        plugin_id="deezer",
        config={},
        credentials={"access-token": "tok"},
        state=_FakePluginState(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    DEEZER_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Listened"]
    assert fake_def_client.create_calls[0]["name"] == "Listened"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"
    assert empty_media_state.listened_definition_id == "def-deezer-listened"
    assert len(saved_states) == 1
    assert saved_states[0].listened_definition_id == "def-deezer-listened"


def test_deezer_does_not_call_resolver_when_definition_already_bootstrapped(monkeypatch):
    """When listened_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.deezer._state_load",
                        lambda path: _make_bootstrapped_media_state())

    monkeypatch.setattr(
        "fulcra_media.plugins.deezer.deezer_importer.fetch_history",
        lambda creds, since, max_pages: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.deezer.deezer_importer.normalize_history",
        lambda raw: [],
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.deezer.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    class _FakePluginState:
        definition_id: str | None = None
        watermark: str | None = None

    ctx = RunContext(
        plugin_id="deezer",
        config={},
        credentials={"access-token": "tok"},
        state=_FakePluginState(),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    DEEZER_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


# ---------------------------------------------------------------------------
# Trakt plugin
# ---------------------------------------------------------------------------

def test_trakt_plugin_metadata():
    from datetime import timedelta
    assert TRAKT_PLUGIN.id == "trakt"
    assert TRAKT_PLUGIN.kind == "scheduled"
    assert TRAKT_PLUGIN.default_interval == timedelta(hours=6)
    # Credentials are now managed via the web-UI OAuth wizard and stored in
    # the OS keychain. The plugin declares all four so the credential-status
    # UI knows what to show (set / missing).
    cred_keys = {c.key for c in TRAKT_PLUGIN.required_credentials}
    assert cred_keys == {"client_id", "client_secret", "access_token", "refresh_token"}
    assert TRAKT_PLUGIN.oauth_handler is not None
    assert TRAKT_PLUGIN.health_check is not None
    assert len(TRAKT_PLUGIN.setup_steps) == 7


def test_trakt_plugin_run_imports_and_advances_watermark(monkeypatch):
    """fetch_history + normalize_history run; cluster/twin helpers are called;
    watermark advances when posted > 0."""
    fake_client = _FakeClient()

    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda: [{"id": 1, "type": "movie", "watched_at": "2026-05-22T10:00:00.000Z"}],
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
        "fulcra_media.plugins.trakt.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda: [],
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # R8: _run_trakt now reads media state; pre-populate so the resolver
    # guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, st = _make_ctx("trakt", {})
    TRAKT_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-trakt"]
    assert fake_client.calls["ensure_tag"] == "trakt"
    assert st.watermark == "2026-05-22T10:00:00+00:00"


def test_trakt_plugin_raises_when_clusters_is_ask(monkeypatch):
    """clusters='ask' is interactive and must raise RuntimeError in headless mode."""
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [],
    )

    ctx, _ = _make_ctx("trakt", {"clusters": "ask"})
    with pytest.raises(RuntimeError, match="ask"):
        TRAKT_PLUGIN.run(ctx)


def test_trakt_plugin_raises_when_twin_policy_is_ask(monkeypatch):
    """twin_policy='ask' is interactive and must raise RuntimeError in headless mode."""
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: [],
    )

    ctx, _ = _make_ctx("trakt", {"twin_policy": "ask"})
    with pytest.raises(RuntimeError, match="ask"):
        TRAKT_PLUGIN.run(ctx)


def test_trakt_plugin_drops_clusters_when_policy_is_drop(monkeypatch):
    """clusters='drop' should call apply_cluster_policy with action='drop'."""

    applied_policies = []

    def fake_apply(events, policy):
        applied_policies.append(policy)
        return events

    fake_client = _FakeClient()
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.normalize_history",
        lambda items, cluster_threshold: ["ev"],
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.apply_cluster_policy", fake_apply)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda: [],
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: _make_bootstrapped_media_state())

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
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda: [],
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
        "fulcra_media.plugins.trakt.find_low_conf_twins",
        lambda events, extra_pool: [(low, high)],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda: [high],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.apply_twin_decisions",
        fake_apply_twin,
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso",
        lambda events: None,
    )
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("trakt", {"twin_policy": "auto-discard"})
    TRAKT_PLUGIN.run(ctx)

    assert len(discard_calls) == 1
    assert "low-id" in discard_calls[0]


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


def test_trakt_uses_resolver_when_watched_definition_not_bootstrapped(monkeypatch):
    """R8 regression: when watched_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_trakt must call
    ctx.resolved_definition_id rather than proceed without a definition ID."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_save",
                        lambda state, path=None: saved_states.append(state))

    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda: [],
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
        "fulcra_media.plugins.trakt.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso",
        lambda events: None,
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-trakt-watched"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="trakt",
        config={},
        credentials={},
        state=PluginState("trakt"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    TRAKT_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Watched"]
    assert fake_def_client.create_calls[0]["name"] == "Watched"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"
    assert empty_media_state.watched_definition_id == "def-trakt-watched"
    assert len(saved_states) == 1
    assert saved_states[0].watched_definition_id == "def-trakt-watched"


def test_trakt_does_not_call_resolver_when_definition_already_bootstrapped(monkeypatch):
    """When watched_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.trakt._state_load",
                        lambda path: _make_bootstrapped_media_state())
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.trakt_importer.fetch_history",
        lambda: [],
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
        "fulcra_media.plugins.trakt.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.newest_event_iso",
        lambda events: None,
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="trakt",
        config={},
        credentials={},
        state=PluginState("trakt"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    TRAKT_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.FulcraClient", lambda: fake_client)
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts.newest_event_iso",
        lambda events: "2026-05-22T10:00:00+00:00",
    )
    # BR8: _run_apple_podcasts now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_load",
                        lambda path: _make_bootstrapped_media_state())

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
        "fulcra_media.plugins.apple_podcasts.ap.parse_db",
        lambda db_path: (received_paths.append(db_path) or iter([])),
    )
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.FulcraClient", lambda: fake_client)
    # BR8: _run_apple_podcasts now reads media state; pre-populate so the
    # resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx("apple-podcasts", {"db_path": str(custom_db)})
    APPLE_PODCASTS_PLUGIN.run(ctx)

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
        APPLE_PODCASTS_PLUGIN.run(ctx)


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


def test_apple_podcasts_uses_resolver_when_listened_definition_not_bootstrapped(
    monkeypatch,
):
    """BR8 regression: when listened_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_apple_podcasts must call
    ctx.resolved_definition_id rather than proceed without a definition ID.

    Uses the creation path: fake client returns no existing def so the
    resolver creates a new one."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_save",
                        lambda state, path=None: saved_states.append(state))

    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts.ap.parse_db",
        lambda db_path: iter([]),
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-apple-podcasts-listened"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="apple-podcasts",
        config={},
        credentials={},
        state=PluginState("apple-podcasts"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    APPLE_PODCASTS_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Listened"]
    assert fake_def_client.create_calls[0]["name"] == "Listened"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"
    assert empty_media_state.listened_definition_id == "def-apple-podcasts-listened"
    assert len(saved_states) == 1
    assert saved_states[0].listened_definition_id == "def-apple-podcasts-listened"


def test_apple_podcasts_does_not_call_resolver_when_definition_already_bootstrapped(
    monkeypatch,
):
    """When listened_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_load",
                        lambda path: _make_bootstrapped_media_state())

    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts.ap.parse_db",
        lambda db_path: iter([]),
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="apple-podcasts",
        config={},
        credentials={},
        state=PluginState("apple-podcasts"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    APPLE_PODCASTS_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
        lambda: [snap1, snap2],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.parse_db",
        fake_parse_db,
    )
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine.FulcraClient", lambda: fake_client)
    # BR9: _run_apple_podcasts_timemachine now reads media state; pre-populate
    # so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine._state_load",
                        lambda path: _make_bootstrapped_media_state())

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
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.find_timemachine_snapshots",
        lambda: [snap1, snap2],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.parse_db",
        fake_parse_db,
    )
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine.FulcraClient", lambda: fake_client)
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
    APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == [ev1]
    assert len(log_warnings) == 1


def test_apple_podcasts_timemachine_plugin_raises_when_no_snapshots(monkeypatch):
    """When find_timemachine_snapshots returns empty, raise RuntimeError."""
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine._state_load",
                        lambda path: _make_bootstrapped_media_state())
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.find_timemachine_snapshots",
        lambda: [],
    )

    ctx, _ = _make_ctx("apple-podcasts-timemachine", {})
    with pytest.raises(RuntimeError, match="Time Machine"):
        APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run(ctx)


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


def test_apple_podcasts_timemachine_uses_resolver_when_listened_definition_not_bootstrapped(
    monkeypatch, tmp_path
):
    """BR9 regression: when listened_definition_id is absent from the media
    state file (machine 2 never ran bootstrap), _run_apple_podcasts_timemachine
    must call ctx.resolved_definition_id rather than proceed without a definition ID.

    Uses the adoption path: fake client returns an existing def so the
    resolver adopts it without creating a duplicate."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine._state_save",
                        lambda state, path=None: saved_states.append(state))

    snap = tmp_path / "snap.sqlite"
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.find_timemachine_snapshots",
        lambda: [snap],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.parse_db",
        lambda db_path: iter([]),
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-apm-timemachine-listened"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="apple-podcasts-timemachine",
        config={},
        credentials={},
        state=PluginState("apple-podcasts-timemachine"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Listened"]
    assert fake_def_client.create_calls[0]["name"] == "Listened"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"
    assert empty_media_state.listened_definition_id == "def-apm-timemachine-listened"
    assert len(saved_states) == 1
    assert saved_states[0].listened_definition_id == "def-apm-timemachine-listened"


def test_apple_podcasts_timemachine_does_not_call_resolver_when_definition_already_bootstrapped(
    monkeypatch, tmp_path
):
    """When listened_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine._state_load",
                        lambda path: _make_bootstrapped_media_state())

    snap = tmp_path / "snap.sqlite"
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.find_timemachine_snapshots",
        lambda: [snap],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts_timemachine.ap.parse_db",
        lambda db_path: iter([]),
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts_timemachine.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="apple-podcasts-timemachine",
        config={},
        credentials={},
        state=PluginState("apple-podcasts-timemachine"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    APPLE_PODCASTS_TIMEMACHINE_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda: object())

    ctx, _ = _make_ctx("media-webhook", {"host": "127.0.0.1", "port": "8765"})
    MEDIA_WEBHOOK_PLUGIN.run(ctx)

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
        return type("S", (), {"serve_forever": lambda self: None})()

    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook._state_load",
        lambda path: _FakeState(),
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook.webhook_receiver.make_server",
        fake_make_server,
    )
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda: object())

    ctx, _ = _make_ctx("media-webhook", {})
    MEDIA_WEBHOOK_PLUGIN.run(ctx)

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
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda: object())

    ctx, _ = _make_ctx("media-webhook", {"host": "0.0.0.0"})
    with pytest.raises(RuntimeError, match="non-loopback"):
        MEDIA_WEBHOOK_PLUGIN.run(ctx)


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


def test_run_uses_resolver_when_watched_definition_not_bootstrapped(monkeypatch):
    """Resolver retrofit: when watched_definition_id is absent from the media
    state file (fresh machine, no bootstrap, no other Watched-producing plugin),
    _run_media_webhook must call ctx.resolved_definition_id at startup rather
    than raise RuntimeError — allowing the service to start standalone.

    After the resolver call the media state must be persisted with the resolved
    id so subsequent restarts find it already set (fast cached path)."""
    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook._state_load",
        lambda path: empty_media_state,
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook._state_save",
        lambda state, path=None: saved_states.append(state),
    )

    class _FakeServer:
        def serve_forever(self):
            pass  # don't actually run the loop

    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook.webhook_receiver.make_server",
        lambda *, host, port, state, client, bearer_token, log_stream: _FakeServer(),
    )
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda: object())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-mw"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="media-webhook",
        config={"host": "127.0.0.1", "port": "8765"},
        credentials={},
        state=PluginState("media-webhook"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    MEDIA_WEBHOOK_PLUGIN.run(ctx)

    # Resolver was invoked with canonical name "Watched"
    assert fake_def_client.list_calls == ["Watched"]
    assert fake_def_client.create_calls[0]["name"] == "Watched"
    assert fake_def_client.create_calls[0]["annotation_type"] == "duration"

    # The resolved id was written into the shared media state
    assert empty_media_state.watched_definition_id == "def-mw"

    # The media state was persisted to disk
    assert len(saved_states) == 1
    assert saved_states[0].watched_definition_id == "def-mw"


def test_run_skips_resolver_when_watched_definition_already_bootstrapped(monkeypatch):
    """When watched_definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip on supervisor restart."""
    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook._state_load",
        lambda path: _make_bootstrapped_media_state(),
    )

    class _FakeServer:
        def serve_forever(self):
            pass

    monkeypatch.setattr(
        "fulcra_media.plugins.media_webhook.webhook_receiver.make_server",
        lambda *, host, port, state, client, bearer_token, log_stream: _FakeServer(),
    )
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda: object())

    resolver_calls: list = []

    def _fake_resolver(self, spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    ctx, _ = _make_ctx("media-webhook", {"host": "127.0.0.1", "port": "8765"})
    MEDIA_WEBHOOK_PLUGIN.run(ctx)

    assert resolver_calls == [], (
        "resolved_definition_id should not be called when watched_definition_id "
        "is already in the media state"
    )


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
    monkeypatch.setattr("fulcra_media.plugins.media_webhook.FulcraClient", lambda: object())

    ctx, _ = _make_ctx("media-webhook", {"host": "0.0.0.0", "port": "8765"})
    ctx.credentials["bearer-token"] = "s" * 32
    MEDIA_WEBHOOK_PLUGIN.run(ctx)

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
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient", lambda: fake_client)
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
    GENERIC_CSV_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-csv"]
    assert fake_client.calls["ensure_tag"] == "myservice"
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
        GENERIC_CSV_PLUGIN.run(ctx)


def test_generic_csv_plugin_raises_when_service_missing():
    ctx, _ = _make_ctx("generic-csv", {"path": "/tmp/x.csv", "category": "watched"})
    with pytest.raises(RuntimeError, match="service"):
        GENERIC_CSV_PLUGIN.run(ctx)


def test_generic_csv_plugin_raises_when_category_missing():
    ctx, _ = _make_ctx("generic-csv", {"path": "/tmp/x.csv", "service": "svc"})
    with pytest.raises(RuntimeError, match="category"):
        GENERIC_CSV_PLUGIN.run(ctx)


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
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient", lambda: fake_client)
    # BR12: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx(
        "generic-csv",
        {"path": str(fake_csv), "service": "svc", "category": "watched", "fingerprint": "none"},
    )
    GENERIC_CSV_PLUGIN.run(ctx)

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
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient", lambda: fake_client)
    # BR12: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx(
        "generic-csv",
        {"path": str(fake_csv), "service": "svc", "category": "watched", "fingerprint": "auto"},
    )
    GENERIC_CSV_PLUGIN.run(ctx)

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
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient", lambda: fake_client)
    # BR12: pre-populate state so the resolver guard exits without a network call.
    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: _make_bootstrapped_media_state())

    ctx, _ = _make_ctx(
        "generic-csv",
        {"path": str(fake_csv), "service": "svc", "category": "watched", "fingerprint": "music"},
    )
    GENERIC_CSV_PLUGIN.run(ctx)

    assert received["fingerprint_kind"] == "music"


# ---------------------------------------------------------------------------
# BR12 regression-guard tests for generic-csv resolver
# ---------------------------------------------------------------------------

def test_generic_csv_plugin_canonical_definition_name_is_none():
    """BR12: generic-csv has no static canonical name — it depends on runtime
    config.  The Plugin object must NOT declare canonical_definition_name."""
    assert GENERIC_CSV_PLUGIN.canonical_definition_name is None


def test_generic_csv_uses_resolver_for_watched_category(monkeypatch, tmp_path):
    """BR12 regression: when category='watched' and watched_definition_id is
    absent, the resolver is called with canonical_name='Watched'."""
    fake_csv = tmp_path / "data.csv"
    fake_csv.write_text("timestamp,title,artist,id\n")

    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.library.resolve", lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_csv.parse_media_csv",
        lambda *a, **kw: iter([]),
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-generic-csv-watched"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="generic-csv",
        config={"path": str(fake_csv), "service": "svc", "category": "watched"},
        credentials={},
        state=PluginState("generic-csv"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    GENERIC_CSV_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Watched"]
    assert fake_def_client.create_calls[0]["name"] == "Watched"
    assert empty_media_state.watched_definition_id == "def-generic-csv-watched"
    assert len(saved_states) == 1
    assert saved_states[0].watched_definition_id == "def-generic-csv-watched"


def test_generic_csv_uses_resolver_for_listened_category(monkeypatch, tmp_path):
    """BR12 regression: when category='listened' and listened_definition_id is
    absent, the resolver is called with canonical_name='Listened'."""
    fake_csv = tmp_path / "data.csv"
    fake_csv.write_text("timestamp,title,artist,id\n")

    empty_media_state = _make_empty_media_state()
    saved_states: list = []

    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: empty_media_state)
    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_save",
                        lambda state, path=None: saved_states.append(state))
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.library.resolve", lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_csv.parse_media_csv",
        lambda *a, **kw: iter([]),
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient",
                        lambda: FakeClient())

    class _FakeDefinitionClient:
        def __init__(self):
            self.list_calls: list = []
            self.create_calls: list = []

        def list_definitions(self, *, name: str) -> list:
            self.list_calls.append(name)
            return []

        def create_definition(self, *, name: str, **spec) -> dict:
            self.create_calls.append({"name": name, **spec})
            return {"id": "def-generic-csv-listened"}

    fake_def_client = _FakeDefinitionClient()

    ctx = RunContext(
        plugin_id="generic-csv",
        config={"path": str(fake_csv), "service": "svc", "category": "listened"},
        credentials={},
        state=PluginState("generic-csv"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
        _fulcra_client_factory=lambda: fake_def_client,
    )
    GENERIC_CSV_PLUGIN.run(ctx)

    assert fake_def_client.list_calls == ["Listened"]
    assert fake_def_client.create_calls[0]["name"] == "Listened"
    assert empty_media_state.listened_definition_id == "def-generic-csv-listened"
    assert len(saved_states) == 1
    assert saved_states[0].listened_definition_id == "def-generic-csv-listened"


def test_generic_csv_does_not_call_resolver_when_definition_already_bootstrapped(
    monkeypatch, tmp_path
):
    """When the target definition_id is already in the media state, the resolver
    must NOT be called — no unnecessary network trip."""
    fake_csv = tmp_path / "data.csv"
    fake_csv.write_text("timestamp,title,artist,id\n")

    monkeypatch.setattr("fulcra_media.plugins.generic_csv._state_load",
                        lambda path: _make_bootstrapped_media_state())
    monkeypatch.setattr("fulcra_media.plugins.generic_csv.library.resolve", lambda p: Path(p))
    monkeypatch.setattr(
        "fulcra_media.plugins.generic_csv.parse_media_csv",
        lambda *a, **kw: iter([]),
    )

    class FakeResult:
        posted = 0
        skipped_existing = 0
        verified = 0

    class FakeClient:
        def ensure_tag(self, name, state):
            pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.generic_csv.FulcraClient",
                        lambda: FakeClient())

    resolver_calls: list = []

    def _fake_resolver(spec, *, canonical_name):
        resolver_calls.append(canonical_name)
        return "should-not-be-returned"

    ctx = RunContext(
        plugin_id="generic-csv",
        config={"path": str(fake_csv), "service": "svc", "category": "watched"},
        credentials={},
        state=PluginState("generic-csv"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    monkeypatch.setattr(RunContext, "resolved_definition_id", _fake_resolver)

    GENERIC_CSV_PLUGIN.run(ctx)

    assert resolver_calls == [], "resolver must not be called when def id is already cached"


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

    def fake_fetch_with_headers(api_headers):
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
        "fulcra_media.plugins.trakt.find_low_conf_twins",
        lambda events, extra_pool: [],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.trakt.twin_cache.load_for_twin_lookup",
        lambda: [],
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
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None): return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.trakt.FulcraClient", lambda: FakeClient())

    st = PluginState("trakt")
    ctx = RunContext(
        plugin_id="trakt",
        config={},
        credentials={"access_token": "my-access-tok", "client_id": "my-client-id"},
        state=st,
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )
    TRAKT_PLUGIN.run(ctx)

    # fetch_history_with_headers must have been called (not the legacy fetch_history)
    assert len(captured_headers) == 1, "fetch_history_with_headers should be called once"
    headers = captured_headers[0]
    assert headers["Authorization"] == "Bearer my-access-tok"
    assert headers["trakt-api-key"] == "my-client-id"
    assert headers["trakt-api-version"] == "2"


# ---------------------------------------------------------------------------
# GAP 14 — annotation events emit verification
# ---------------------------------------------------------------------------

def test_lastfm_emits_annotation_event_on_successful_writes(monkeypatch):
    """When lastfm successfully posts new scrobbles, _run_lastfm emits an
    annotation event (via ctx.annotation) so the dashboard activity feed sees
    the write.  Catches silently-broken plugins that stop reporting writes."""
    emitted: list[dict] = []

    monkeypatch.setattr(
        "fulcra_media.plugins.lastfm.fetch_recent_tracks",
        lambda creds, since, max_pages: [{"raw": 1}],
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.lastfm.normalize_history",
        lambda raw: ["event-1"],
    )

    class FakeResult:
        posted = 3       # non-zero → annotation event must be emitted
        skipped_existing = 0
        verified = 3

    class FakeClient:
        def ensure_tag(self, name, state): pass
        def run_import(self, events, state, check_only=False, claim=None, unclaim=None): return FakeResult()

    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient", lambda: FakeClient())
    monkeypatch.setattr(
        "fulcra_media.plugins.lastfm.newest_event_iso",
        lambda events: "2026-05-22T12:00:00Z",
    )
    monkeypatch.setattr(
        "fulcra_media.plugins.lastfm._state_load",
        lambda path: _make_bootstrapped_media_state(),
    )

    st = PluginState("lastfm")
    ctx = RunContext(
        plugin_id="lastfm",
        config={"username": "test_user"},
        credentials={"api-key": "K"},
        state=st,
        log=logging.getLogger("t"),
        _emit=emitted.append,
    )
    LASTFM_PLUGIN.run(ctx)

    annotation_events = [e for e in emitted if e.get("type") == "annotation"]
    assert len(annotation_events) >= 1, "at least one annotation event must be emitted"
    evt = annotation_events[0]
    assert evt["ok"] is True
    # The summary should reference the tag name and count
    assert "lastfm" in evt["summary"].lower() or "3" in evt["summary"]
