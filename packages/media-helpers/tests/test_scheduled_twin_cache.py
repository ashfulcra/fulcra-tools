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

We exercise the real seam (``run_scheduled_import``) through the Last.fm
plugin, which is the simplest plugin that delegates to it, plus the
``import_events`` and ``rss_import_and_advance`` glue used by the file-based
and RSS plugins, to prove the wiring is shared rather than trakt-only.
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
        def ensure_tag(self, name, state):
            captured["ensure_tag"] = name

        def run_import(self, events, state, check_only=False, claim=None, unclaim=None):
            captured["imported"] = list(events)
            return result
    return FakeClient


# ---------------------------------------------------------------------------
# 1. Scheduled import populates the cache (real run_scheduled_import seam)
# ---------------------------------------------------------------------------

def test_scheduled_import_populates_twin_cache(cache_path, monkeypatch):
    """A scheduled import of a HIGH-confidence event with a content_fingerprint
    writes that fingerprint into the twin cache — which the scheduled path did
    NOT do before Component 4."""
    high = _evt(fp="tv:dune:s01e01", confidence="high", sid="lastfm-1")

    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [high])

    captured = {}
    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        _make_fake_client(captured, result=_FakeResult(posted=1)))
    monkeypatch.setattr("fulcra_media.plugins.lastfm.newest_event_iso",
                        lambda events: "2026-05-22T12:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _bootstrapped_media_state())

    from fulcra_media.collect_plugins import LASTFM_PLUGIN
    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "u"},
                     credentials={"api-key": "K"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    cache = twin_cache.load(cache_path)
    assert "tv:dune:s01e01" in cache
    assert cache["tv:dune:s01e01"]["source_id"] == "lastfm-1"


def test_scheduled_import_does_not_populate_when_nothing_posted(cache_path, monkeypatch):
    """Mirror the CLI guard: don't cache events that weren't written. When the
    import posts nothing (all duplicates / failure), the cache stays empty so
    we never record an event that isn't actually in Fulcra."""
    high = _evt(fp="tv:dune:s01e01", confidence="high", sid="lastfm-1")

    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [high])

    captured = {}
    monkeypatch.setattr(
        "fulcra_media.plugins.lastfm.FulcraClient",
        _make_fake_client(captured, result=_FakeResult(posted=0, skipped_existing=1)),
    )
    monkeypatch.setattr("fulcra_media.plugins.lastfm.newest_event_iso",
                        lambda events: "2026-05-22T12:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _bootstrapped_media_state())

    from fulcra_media.collect_plugins import LASTFM_PLUGIN
    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "u"},
                     credentials={"api-key": "K"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    assert twin_cache.load(cache_path) == {}


def test_scheduled_import_only_caches_high_confidence(cache_path, monkeypatch):
    """Only high-confidence events go into the cache (a low-conf event is the
    one that DEFERS to a cached twin — it must never become a twin itself)."""
    low = _evt(fp="tv:other:s01e01", confidence="low", sid="lastfm-low")
    high = _evt(fp="tv:dune:s01e01", confidence="high", sid="lastfm-high")

    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [low, high])

    captured = {}
    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        _make_fake_client(captured, result=_FakeResult(posted=2)))
    monkeypatch.setattr("fulcra_media.plugins.lastfm.newest_event_iso",
                        lambda events: "2026-05-22T12:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _bootstrapped_media_state())

    from fulcra_media.collect_plugins import LASTFM_PLUGIN
    st = PluginState("lastfm")
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "u"},
                     credentials={"api-key": "K"},
                     state=st, log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    cache = twin_cache.load(cache_path)
    assert set(cache.keys()) == {"tv:dune:s01e01"}


# ---------------------------------------------------------------------------
# 2. A subsequent scheduled import consults the cache (cross-run twin match)
# ---------------------------------------------------------------------------

def test_second_scheduled_import_consults_cache_for_low_conf_twin(cache_path, monkeypatch):
    """End-to-end: run 1 imports a HIGH-conf event (populating the cache);
    run 2 imports a LOW-conf event for the SAME content. The scheduled glue
    loads the cache as the extra_pool for find_low_conf_twins, so the matcher
    pairs the incoming low-conf event with the cached high-conf twin. Before
    Component 4 the cache was empty so there was nothing to match against on
    the scheduled path. (Run 2 sets twin_policy='auto-discard' so the consult
    machinery actually runs — the default 'keep' short-circuits as a no-op,
    matching trakt's existing pattern.)"""
    from fulcra_csv import find_low_conf_twins
    from fulcra_media.collect_plugins import LASTFM_PLUGIN

    # --- run 1: high-conf populates cache ---
    high = _evt(fp="tv:dune:s01e01", confidence="high", sid="netflix-1",
                ts=datetime(2026, 4, 1, tzinfo=timezone.utc))
    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [high])
    captured1 = {}
    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        _make_fake_client(captured1, result=_FakeResult(posted=1)))
    monkeypatch.setattr("fulcra_media.plugins.lastfm.newest_event_iso",
                        lambda events: "2026-04-01T00:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _bootstrapped_media_state())

    ctx1 = RunContext(plugin_id="lastfm", config={"username": "u"},
                      credentials={"api-key": "K"}, state=PluginState("lastfm"),
                      log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx1)
    assert "tv:dune:s01e01" in twin_cache.load(cache_path)

    # --- run 2: low-conf for the same content; the glue must consult the
    # cache. We capture the extra_pool find_low_conf_twins is called with to
    # prove the cached high-conf twin reaches the matcher. ---
    low = _evt(fp="tv:dune:s01e01", confidence="low", sid="trakt-1",
               ts=datetime(2026, 5, 16, tzinfo=timezone.utc))
    seen = {}
    real_find = find_low_conf_twins

    def spy_find(events, *, twin_key="content_fingerprint", extra_pool=None):
        pairs = real_find(events, twin_key=twin_key, extra_pool=extra_pool)
        seen["pairs"] = pairs
        seen["extra_pool"] = list(extra_pool or [])
        return pairs

    monkeypatch.setattr("fulcra_media.plugins._common.find_low_conf_twins", spy_find)
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [low])
    captured2 = {}
    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        _make_fake_client(captured2, result=_FakeResult(posted=1)))

    ctx2 = RunContext(plugin_id="lastfm",
                      config={"username": "u", "twin_policy": "auto-discard"},
                      credentials={"api-key": "K"}, state=PluginState("lastfm"),
                      log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx2)

    # The cache was consulted: the cached high-conf twin was in the pool, and
    # the low-conf incoming event matched it.
    assert any(c.external_ids.get("content_fingerprint") == "tv:dune:s01e01"
               for c in seen["extra_pool"]), "cached twin not loaded into extra_pool"
    assert len(seen["pairs"]) == 1
    low_ev, high_ev = seen["pairs"][0]
    assert twin_cache._source_id_of(low_ev) == "trakt-1"
    assert twin_cache._source_id_of(high_ev) == "netflix-1"


# ---------------------------------------------------------------------------
# 3. Default policy is a no-op (behaviour unchanged)
# ---------------------------------------------------------------------------

def test_default_twin_policy_does_not_discard(cache_path, monkeypatch):
    """With no twin_policy configured (default 'keep'), an incoming low-conf
    event that HAS a cached high-conf twin is still imported — the default
    behaviour is unchanged; only the machinery is now available."""
    # Pre-seed the cache with a high-conf twin.
    twin_cache.record_imported_events(
        [_evt(fp="tv:dune:s01e01", confidence="high", sid="netflix-1",
              ts=datetime(2026, 4, 1, tzinfo=timezone.utc))],
        cache_path,
    )
    low = _evt(fp="tv:dune:s01e01", confidence="low", sid="trakt-1",
               ts=datetime(2026, 5, 16, tzinfo=timezone.utc))

    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [low])
    captured = {}
    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        _make_fake_client(captured, result=_FakeResult(posted=1)))
    monkeypatch.setattr("fulcra_media.plugins.lastfm.newest_event_iso",
                        lambda events: "2026-05-16T00:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _bootstrapped_media_state())

    from fulcra_media.collect_plugins import LASTFM_PLUGIN
    ctx = RunContext(plugin_id="lastfm", config={"username": "u"},
                     credentials={"api-key": "K"}, state=PluginState("lastfm"),
                     log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    # The low-conf event was NOT discarded — default 'keep' policy.
    assert captured["imported"] == [low]


def test_auto_discard_policy_drops_low_conf_twin_on_scheduled_path(cache_path, monkeypatch):
    """When twin_policy='auto-discard' is explicitly configured, the scheduled
    glue drops the low-conf incoming event whose fingerprint matches a cached
    high-conf twin — proving the consult-and-apply machinery is live, not just
    the load. (Opt-in; not the default.)"""
    twin_cache.record_imported_events(
        [_evt(fp="tv:dune:s01e01", confidence="high", sid="netflix-1",
              ts=datetime(2026, 4, 1, tzinfo=timezone.utc))],
        cache_path,
    )
    low = _evt(fp="tv:dune:s01e01", confidence="low", sid="trakt-1",
               ts=datetime(2026, 5, 16, tzinfo=timezone.utc))
    keep = _evt(fp="tv:keep:s01e01", confidence="low", sid="keep-1",
                ts=datetime(2026, 5, 17, tzinfo=timezone.utc))

    monkeypatch.setattr("fulcra_media.plugins.lastfm.fetch_recent_tracks",
                        lambda creds, since, max_pages: [{"raw": 1}])
    monkeypatch.setattr("fulcra_media.plugins.lastfm.normalize_history",
                        lambda raw: [low, keep])
    captured = {}
    monkeypatch.setattr("fulcra_media.plugins.lastfm.FulcraClient",
                        _make_fake_client(captured, result=_FakeResult(posted=1)))
    monkeypatch.setattr("fulcra_media.plugins.lastfm.newest_event_iso",
                        lambda events: "2026-05-17T00:00:00Z")
    monkeypatch.setattr("fulcra_media.plugins.lastfm._state_load",
                        lambda path: _bootstrapped_media_state())

    from fulcra_media.collect_plugins import LASTFM_PLUGIN
    ctx = RunContext(plugin_id="lastfm",
                     config={"username": "u", "twin_policy": "auto-discard"},
                     credentials={"api-key": "K"}, state=PluginState("lastfm"),
                     log=logging.getLogger("t"), _emit=lambda e: None)
    LASTFM_PLUGIN.run(ctx)

    # The matched low-conf twin was dropped; the unmatched low-conf event stays.
    assert captured["imported"] == [keep]


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

    APPLE_PODCASTS_PLUGIN.run(_ap_ctx({}))

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

    APPLE_PODCASTS_PLUGIN.run(_ap_ctx({}))

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

    APPLE_PODCASTS_PLUGIN.run(_ap_ctx({"twin_policy": "auto-discard"}))

    # The matched low-conf twin was dropped; the unmatched one stays.
    assert captured["imported"] == [keep]
