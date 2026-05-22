"""The Last.fm fulcra-collect plugin."""
from __future__ import annotations

import logging

from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_media.collect_plugins import LASTFM_PLUGIN


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
    import pytest
    with pytest.raises(RuntimeError, match="api-key"):
        LASTFM_PLUGIN.run(ctx)
