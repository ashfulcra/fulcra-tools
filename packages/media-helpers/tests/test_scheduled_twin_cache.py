"""Component 4: the twin cache must be populated AND consulted on the
scheduled-plugin path, not just the CLI path.

Before this component, ``record_imported_events`` was called only from
``cli_common.run_and_emit`` (the CLI import path). A scheduled-only user
therefore built an empty twin cache and the cross-source twin matcher was
inert. These tests drive the shared scheduled-import glue in
``fulcra_media.plugins._common`` so that:

  1. A scheduled import of a HIGH-confidence event populates the twin cache.
  2. A subsequent scheduled import of a LOW-confidence event for the same
     content can find the cached high-conf twin via ``find_low_conf_twins``.
  3. The default twin policy ("keep") leaves the incoming events untouched —
     the machinery is wired but the default behaviour is unchanged.

Guarded poll tests exercise actual configured decorators, strict source scans,
native writer visibility and account/destination-scoped hints. Synthetic
high/low variants change only normalized twin metadata. Remaining helper tests
cover the retained legacy glue separately; its global JSON cache is never an
authority for the five guarded poll routes.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_media import twin_cache
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.plugins import _common
from fulcra_media.state import State as MediaState


def _bootstrapped_media_state() -> MediaState:
    return MediaState(
        watched_definition_id="def-watched-123",
        listened_definition_id="def-listened-456",
        read_definition_id="def-read-789",
    )


def _evt(*, fp: str, confidence: str, sid: str,
         ts: datetime | None = None) -> NormalizedEvent:
    ts = ts or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return NormalizedEvent(
        importer="lastfm",
        service="lastfm",
        category="listened",
        note="x",
        title="x",
        start_time=ts,
        end_time=ts,
        deterministic_id=sid,
        timestamp_confidence=confidence,
        external_ids={"content_fingerprint": fp},
    )


@pytest.fixture
def cache_path(tmp_path, monkeypatch):
    """Redirect the twin cache to a temp file via the env-var seam the
    cache uses for its default path, so production code that calls
    ``record_imported_events()`` / ``load_for_twin_lookup()`` with no
    explicit path still hits an isolated cache."""
    p = tmp_path / "twin_cache.json"
    monkeypatch.setattr(twin_cache, "DEFAULT_CACHE_PATH", p)
    return p


class _FakeResult:
    def __init__(self, posted=1, skipped_existing=0):
        self.posted = posted
        self.skipped_existing = skipped_existing
        self.verified = posted


def _make_fake_client(captured, *, result):
    class FakeClient:
        def __init__(self, **_kwargs):
            pass
        def ensure_tag(self, name, state):
            captured["ensure_tag"] = name

        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            captured["imported"] = list(events)
            return result
    return FakeClient


# The six former Last.fm.__wrapped__/global-JSON-cache tests now drive
# configured decorated routes. The legacy helper tests below remain separate.
from test_daemon_poll import (  # noqa: E402
    ACCOUNT, DESTINATION, daemon as daemon, save_settings,
)
from fulcra_media import collect_v1  # noqa: E402
from fulcra_media.collect_twins import MediaTwinIndex  # noqa: E402


def _variants(daemon, monkeypatch, transform):
    """Keep real strict scan/normalization; vary only synthetic twin metadata."""
    import importlib
    from dataclasses import replace
    name = daemon.name
    module = daemon.plugin if name == "lastfm" else importlib.import_module("fulcra_media.importers." + name)
    method = "normalize_history" if name in ("lastfm", "deezer") else "normalize_feed" if name == "generic_rss" else "fetch_diary"
    actual = getattr(module, method)
    def normalized(*args, **kwargs):
        events = list(actual(*args, **kwargs))
        return [replace(event, **transform(index, event)) for index, event in enumerate(events)]
    monkeypatch.setattr(module, method, normalized)


def _fingerprints(daemon):
    return [event.external_ids["content_fingerprint"] for event in daemon.info["events"]]


def _preview_then_seed_high(daemon):
    """Seed a real verified other-importer row in this owned destination."""
    from dataclasses import replace
    save_settings(daemon, preview_only=True)
    daemon.invoke()
    event = daemon.info["events"][0]
    assert not daemon.info["posts"]
    save_settings(daemon, preview_only=False)
    high = replace(event, importer="synthetic-other", deterministic_id="com.fulcra.media.synthetic-other.v1.high",
        timestamp_confidence="high", extra_source_ids=(),
        external_ids={**event.external_ids, "content_fingerprint": "daemon-twin:match"})
    with collect_v1.CollectRecordSession(daemon.ctx) as session:
        result = collect_v1.MediaRecordWriter(daemon.ctx, session).run_import([high])
    assert result.posted == result.verified == result.total == 1
    identifier = daemon.info["posts"][0]["id"]
    assert identifier in daemon.info["rows"]
    daemon.info["posts"].clear()
    return identifier


def _low_variant(index, event):
    return {"timestamp_confidence": "low", "external_ids": {**event.external_ids,
        "content_fingerprint": "daemon-twin:match" if index == 0 else f"daemon-twin:unmatched-{index}"}}


def _observe_native_high_authority(monkeypatch):
    reads, exclusions = [], []
    # The underlying canonical class is used by the fixture constructor.
    from fulcra_collect.v1_session import CollectRecordSession
    get_records, excluded_ids = CollectRecordSession.get_records, CollectRecordSession.excluded_ids
    def records(session, destination, identifiers):
        reads.extend((destination, identifier) for identifier in identifiers)
        return get_records(session, destination, identifiers)
    def excluded(session, destination, identifiers, **kwargs):
        exclusions.extend((destination, identifier) for identifier in identifiers)
        return excluded_ids(session, destination, identifiers, **kwargs)
    monkeypatch.setattr(CollectRecordSession, "get_records", records)
    monkeypatch.setattr(CollectRecordSession, "excluded_ids", excluded)
    return reads, exclusions


def test_scheduled_guarded_import_populates_scoped_verified_index(daemon, cache_path, monkeypatch):
    _variants(daemon, monkeypatch, lambda i, e: {"timestamp_confidence": "high",
        "external_ids": {**e.external_ids, "content_fingerprint": f"daemon-twin:{i}"}})
    daemon.invoke()
    found = MediaTwinIndex().lookup(ACCOUNT, DESTINATION, _fingerprints(daemon))
    assert found and all(identifier in daemon.info["rows"] for identifier in found.values())
    assert not MediaTwinIndex().lookup("other-account", DESTINATION, _fingerprints(daemon))
    assert not MediaTwinIndex().lookup(ACCOUNT, "Event/aaaaaaaa-2222-4333-8444-555555555555", _fingerprints(daemon))
    assert twin_cache.load(cache_path) == {}  # machine-wide cache cannot authorize guarded routes


def test_scheduled_zero_post_exact_existing_can_seed_scoped_index(daemon, monkeypatch):
    _variants(daemon, monkeypatch, lambda i, e: {"timestamp_confidence": "high",
        "external_ids": {**e.external_ids, "content_fingerprint": f"daemon-twin:{i}"}})
    daemon.invoke()
    count = len(daemon.info["posts"])
    index = MediaTwinIndex()
    index.path.unlink()
    daemon.invoke()
    assert len(daemon.info["posts"]) == count
    found = index.lookup(ACCOUNT, DESTINATION, _fingerprints(daemon))
    assert found and all(identifier in daemon.info["rows"] for identifier in found.values())
    assert any(e.get("stage") == "imported" and e.get("posted") == 0 and e.get("skipped", 0) > 0 for e in daemon.emitted)


def test_scheduled_guarded_index_only_caches_high_confidence(daemon, monkeypatch):
    _variants(daemon, monkeypatch, lambda i, e: {"timestamp_confidence": "low" if i == 0 else "high",
        "external_ids": {**e.external_ids, "content_fingerprint": f"daemon-twin:{i}"}})
    daemon.invoke()
    assert daemon.info["posts"]
    found = MediaTwinIndex().lookup(ACCOUNT, DESTINATION, _fingerprints(daemon))
    assert "daemon-twin:0" not in found
    assert found and all(daemon.info["rows"][identifier]["timestamp_confidence"] == "high" for identifier in found.values())


def test_scheduled_guarded_auto_discard_rechecks_native_high_and_exclusions(daemon, monkeypatch):
    high = _preview_then_seed_high(daemon)
    save_settings(daemon, twin_policy="auto-discard")
    _variants(daemon, monkeypatch, _low_variant)
    reads, exclusions = _observe_native_high_authority(monkeypatch)
    daemon.invoke()
    assert (DESTINATION, high) in reads and (DESTINATION, high) in exclusions
    assert len(daemon.info["posts"]) == len(daemon.info["events"]) - 1
    assert all(row["external_ids"]["content_fingerprint"] != "daemon-twin:match" for row in daemon.info["posts"])


def test_scheduled_guarded_default_keep_does_not_discard_native_twin(daemon, monkeypatch):
    _preview_then_seed_high(daemon)
    _variants(daemon, monkeypatch, _low_variant)
    daemon.invoke()
    assert len(daemon.info["posts"]) == len(daemon.info["events"])
    assert any(row["external_ids"]["content_fingerprint"] == "daemon-twin:match" for row in daemon.info["posts"])


def test_scheduled_guarded_all_low_twins_skip_without_post_or_low_index(daemon, monkeypatch):
    high = _preview_then_seed_high(daemon)
    save_settings(daemon, twin_policy="auto-discard")
    _variants(daemon, monkeypatch, lambda i, e: {"timestamp_confidence": "low",
        "external_ids": {**e.external_ids, "content_fingerprint": "daemon-twin:match"}})
    reads, exclusions = _observe_native_high_authority(monkeypatch)
    daemon.invoke()
    assert not daemon.info["posts"]
    assert (DESTINATION, high) in reads and (DESTINATION, high) in exclusions
    assert MediaTwinIndex().lookup(ACCOUNT, DESTINATION, ["daemon-twin:match"]) == {"daemon-twin:match": high}
    assert any(e.get("stage") == "imported" and e.get("posted") == 0 and e.get("skipped") == len(daemon.info["events"]) for e in daemon.emitted)


# ---------------------------------------------------------------------------
# Direct unit coverage of the shared helpers (so every glue path benefits)
# ---------------------------------------------------------------------------

def test_apply_twin_policy_keep_is_noop():
    events = [_evt(fp="a", confidence="low", sid="1")]
    out = _common.apply_twin_policy(events, twin_policy="keep", cached_pool=[])
    assert out == events


def test_apply_twin_policy_ask_raises():
    with pytest.raises(RuntimeError, match="ask"):
        _common.apply_twin_policy([], twin_policy="ask", cached_pool=[])


def test_record_twins_after_post_only_runs_on_posted(cache_path):
    high = _evt(fp="x", confidence="high", sid="s1")
    # posted == 0 → no write
    _common.record_twins_after_post([high], posted=0)
    assert twin_cache.load(cache_path) == {}
    # posted > 0 → write
    _common.record_twins_after_post([high], posted=1)
    assert "x" in twin_cache.load(cache_path)


# ---------------------------------------------------------------------------
# Apple Podcasts: the inline run_import plugins must also sit on the twin seam.
#
# apple_podcasts / apple_podcasts_timemachine call client.run_import inline and
# bypassed the shared funnels, so before this fix they neither populated nor
# consulted the twin cache. These drive the real plugin run functions.
# ---------------------------------------------------------------------------

def _ap_evt(*, fp: str, confidence: str, sid: str,
            ts: datetime | None = None) -> NormalizedEvent:
    ts = ts or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return NormalizedEvent(
        importer="apple-podcasts",
        service="apple-podcasts",
        category="listened",
        note="x",
        title="x",
        start_time=ts,
        end_time=ts,
        deterministic_id=sid,
        timestamp_confidence=confidence,
        external_ids={"content_fingerprint": fp},
    )


def _ap_ctx(config: dict) -> RunContext:
    return RunContext(
        plugin_id="apple-podcasts",
        config=config,
        credentials={},
        state=PluginState("apple-podcasts"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )


def test_apple_podcasts_scheduled_import_populates_twin_cache(cache_path, monkeypatch):
    """A HIGH-conf apple-podcasts scheduled import writes its fingerprint into
    the twin cache — which the inline plugin did NOT do before this fix."""
    from fulcra_media.collect_plugins import APPLE_PODCASTS_PLUGIN

    high = _ap_evt(fp="pod:show:ep1", confidence="high", sid="ap-1")
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.ap.parse_db",
                        lambda db_path: iter([high]))
    captured = {}
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.FulcraClient",
                        _make_fake_client(captured, result=_FakeResult(posted=1)))
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.newest_event_iso",
                        lambda events: "2026-01-01T00:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_load",
                        lambda path: _bootstrapped_media_state())

    APPLE_PODCASTS_PLUGIN.run.__wrapped__(_ap_ctx({}))

    cache = twin_cache.load(cache_path)
    assert "pod:show:ep1" in cache
    assert cache["pod:show:ep1"]["source_id"] == "ap-1"


def test_apple_podcasts_does_not_populate_when_nothing_posted(cache_path, monkeypatch):
    """No POST (all duplicates) → no cache write, mirroring the shared seam."""
    from fulcra_media.collect_plugins import APPLE_PODCASTS_PLUGIN

    high = _ap_evt(fp="pod:show:ep1", confidence="high", sid="ap-1")
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.ap.parse_db",
                        lambda db_path: iter([high]))
    captured = {}
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_podcasts.FulcraClient",
        _make_fake_client(captured, result=_FakeResult(posted=0, skipped_existing=1)),
    )
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.newest_event_iso",
                        lambda events: "2026-01-01T00:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_load",
                        lambda path: _bootstrapped_media_state())

    APPLE_PODCASTS_PLUGIN.run.__wrapped__(_ap_ctx({}))

    assert twin_cache.load(cache_path) == {}


def test_apple_podcasts_auto_discard_consults_cache(cache_path, monkeypatch):
    """A LOW-conf apple-podcasts import with twin_policy='auto-discard' consults
    the cache and drops the incoming low-conf event whose fingerprint matches a
    cached high-conf twin. Before this fix the inline plugin never consulted."""
    from fulcra_media.collect_plugins import APPLE_PODCASTS_PLUGIN

    twin_cache.record_imported_events(
        [_ap_evt(fp="pod:show:ep1", confidence="high", sid="netflix-1",
                 ts=datetime(2026, 4, 1, tzinfo=timezone.utc))],
        cache_path,
    )
    low = _ap_evt(fp="pod:show:ep1", confidence="low", sid="ap-low",
                  ts=datetime(2026, 5, 16, tzinfo=timezone.utc))
    keep = _ap_evt(fp="pod:show:ep2", confidence="low", sid="ap-keep",
                   ts=datetime(2026, 5, 17, tzinfo=timezone.utc))

    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.ap.parse_db",
                        lambda db_path: iter([low, keep]))
    captured = {}
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.FulcraClient",
                        _make_fake_client(captured, result=_FakeResult(posted=1)))
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts.newest_event_iso",
                        lambda events: "2026-05-17T00:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.apple_podcasts._state_load",
                        lambda path: _bootstrapped_media_state())

    APPLE_PODCASTS_PLUGIN.run.__wrapped__(_ap_ctx({"twin_policy": "auto-discard"}))

    # The matched low-conf twin was dropped; the unmatched one stays.
    assert captured["imported"] == [keep]


# ---------------------------------------------------------------------------
# Plugin-default twin policy: apple-tv / netflix / apple-takeout ship with
# default_twin_policy="auto-discard" so low-conf twins of an already-imported
# high-conf watch (e.g. Trakt) are dropped even without operator config.
# ---------------------------------------------------------------------------

def test_import_events_default_policy_auto_discard_drops_twin(cache_path):
    """import_events(default_twin_policy='auto-discard') with NO config
    override drops a low-conf event whose fingerprint matches a cached
    high-conf twin — the plugin default alone is enough."""
    from fulcra_media.plugins import _common
    twin_cache.record_imported_events(
        [_evt(fp="tv:dune:s01e01", confidence="high", sid="trakt-1",
              ts=datetime(2026, 4, 1, tzinfo=timezone.utc))],
        cache_path)
    low = _evt(fp="tv:dune:s01e01", confidence="low", sid="appletv-1",
               ts=datetime(2026, 5, 16, tzinfo=timezone.utc))
    captured = {}
    ctx = RunContext(plugin_id="apple-tv", config={}, credentials={},
                     state=PluginState("apple-tv"),
                     log=logging.getLogger("t"), _emit=lambda e: None)
    _common.import_events(
        ctx, [low], "apple-tv",
        fulcra_client_cls=_make_fake_client(captured, result=_FakeResult(posted=0)),
        state_load=lambda p: _bootstrapped_media_state(),
        default_twin_policy="auto-discard")
    assert captured["imported"] == []


def test_import_events_config_overrides_default_policy(cache_path):
    """Operator config twin_policy='keep' wins over a plugin default of
    auto-discard — the low-conf twin is kept."""
    from fulcra_media.plugins import _common
    twin_cache.record_imported_events(
        [_evt(fp="tv:dune:s01e01", confidence="high", sid="trakt-1",
              ts=datetime(2026, 4, 1, tzinfo=timezone.utc))],
        cache_path)
    low = _evt(fp="tv:dune:s01e01", confidence="low", sid="appletv-1",
               ts=datetime(2026, 5, 16, tzinfo=timezone.utc))
    captured = {}
    ctx = RunContext(plugin_id="apple-tv", config={"twin_policy": "keep"},
                     credentials={}, state=PluginState("apple-tv"),
                     log=logging.getLogger("t"), _emit=lambda e: None)
    _common.import_events(
        ctx, [low], "apple-tv",
        fulcra_client_cls=_make_fake_client(captured, result=_FakeResult(posted=1)),
        state_load=lambda p: _bootstrapped_media_state(),
        default_twin_policy="auto-discard")
    assert captured["imported"] == [low]


def _capture_default_policy(monkeypatch, module_path, func_name):
    captured = {}

    def spy(*args, **kwargs):
        captured["policy"] = kwargs.get("default_twin_policy", "keep")

    monkeypatch.setattr(f"{module_path}.{func_name}", spy)
    return captured


def test_apple_tv_plugin_defaults_to_auto_discard(monkeypatch):
    from fulcra_media.collect_plugins import APPLE_TV_PLUGIN
    captured = _capture_default_policy(
        monkeypatch, "fulcra_media.plugins.apple_tv", "import_events")
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_tv.apple_tv_importer.parse_cache",
        lambda cache_dir: [])
    monkeypatch.setattr("fulcra_media.plugins.apple_tv._state_load",
                        lambda p: _bootstrapped_media_state())
    ctx = RunContext(plugin_id="apple-tv", config={}, credentials={},
                     state=PluginState("apple-tv"),
                     log=logging.getLogger("t"), _emit=lambda e: None)
    APPLE_TV_PLUGIN.run.__wrapped__(ctx)
    assert captured["policy"] == "auto-discard"


def test_netflix_plugin_defaults_to_auto_discard(monkeypatch):
    from fulcra_media.collect_plugins import NETFLIX_PLUGIN
    captured = _capture_default_policy(
        monkeypatch, "fulcra_media.plugins.netflix", "run_file_import")
    # netflix.run ensures the Watched definition before importing; stub that
    # pre-step (it needs a real client factory) so the test isolates the
    # policy passed to run_file_import.
    monkeypatch.setattr("fulcra_media.plugins.netflix._state_load",
                        lambda p: _bootstrapped_media_state())
    ctx = RunContext(plugin_id="netflix", config={"path": "/x.csv"},
                     credentials={}, state=PluginState("netflix"),
                     log=logging.getLogger("t"), _emit=lambda e: None)
    NETFLIX_PLUGIN.run.__wrapped__(ctx)
    assert captured["policy"] == "auto-discard"


def test_apple_takeout_plugin_defaults_to_auto_discard(monkeypatch):
    from fulcra_media.collect_plugins import APPLE_TAKEOUT_PLUGIN
    captured = _capture_default_policy(
        monkeypatch, "fulcra_media.plugins.apple_takeout", "import_events")
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_takeout.apple_takeout_importer.parse_any",
        lambda resolved, since, until: [])
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout.resolve_path",
                        lambda ctx, lib: "/x.zip")
    monkeypatch.setattr("fulcra_media.plugins.apple_takeout._state_load",
                        lambda p: _bootstrapped_media_state())
    ctx = RunContext(plugin_id="apple-takeout", config={"path": "/x.zip"},
                     credentials={}, state=PluginState("apple-takeout"),
                     log=logging.getLogger("t"), _emit=lambda e: None)
    APPLE_TAKEOUT_PLUGIN.run.__wrapped__(ctx)
    assert captured["policy"] == "auto-discard"
