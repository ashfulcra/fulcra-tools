"""Apple TV collect-plugin wrapper, health check, and registration tests."""
from __future__ import annotations

import gzip
import json
import logging
from datetime import timedelta

import pytest

from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_media.apple_tv_health import apple_tv_health_check
from fulcra_media.plugins.apple_tv import PLUGIN as APPLE_TV_PLUGIN
from fulcra_media.state import State as MediaState

from test_apple_tv_importer import (  # noqa: F401 - shared fixture helpers
    CANVAS_URL,
    HISTORY_FIXTURE,
    UPNEXT_FIXTURE,
    _make_cache_db,
)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def test_plugin_metadata_is_scheduled_local_no_credentials():
    assert APPLE_TV_PLUGIN.id == "apple-tv"
    assert APPLE_TV_PLUGIN.kind == "scheduled"
    assert APPLE_TV_PLUGIN.collect_mode == "live_polled"
    assert APPLE_TV_PLUGIN.default_interval == timedelta(hours=6)
    assert APPLE_TV_PLUGIN.requires_network is False
    assert APPLE_TV_PLUGIN.category == "video"
    assert APPLE_TV_PLUGIN.canonical_definition_name == "Watched"
    # The whole point of the UTS-cache pathway: no creds, no FDA.
    assert APPLE_TV_PLUGIN.required_credentials == ()
    assert APPLE_TV_PLUGIN.required_permissions == ()


def test_plugin_declares_a_health_check():
    assert APPLE_TV_PLUGIN.health_check is not None


def test_setup_steps_order():
    kinds = [s.kind for s in APPLE_TV_PLUGIN.setup_steps]
    assert kinds[0] == "intro"
    assert kinds[-1] == "done"
    assert "test_connection" in kinds
    assert "definition_picker" in kinds
    # Verify the cache is readable BEFORE binding a Fulcra definition.
    assert kinds.index("test_connection") < kinds.index("definition_picker")


def test_plugin_entry_point_resolves():
    """The fulcra_collect.plugins entry point must load this exact object."""
    from importlib.metadata import entry_points
    eps = [ep for ep in entry_points(group="fulcra_collect.plugins")
           if ep.name == "apple-tv"]
    assert eps, "apple-tv entry point not registered"
    assert eps[0].load() is APPLE_TV_PLUGIN


def test_bundled_manifest_contains_apple_tv():
    """The frozen-build fallback manifest must stay in sync with the
    entry-point table (test_manifest_matches_entry_points enforces the
    full-set equality; this pins our row specifically)."""
    from fulcra_collect._bundled_plugins import BUNDLED_PLUGINS
    assert ("apple-tv", "fulcra_media.plugins.apple_tv:PLUGIN") in BUNDLED_PLUGINS


def test_plugin_contract_serializes():
    """Mirror the daemon's /api/plugin/{id}/contract serializer — every
    field it reads must exist and the result must be JSON-clean."""
    p = APPLE_TV_PLUGIN
    contract = {
        "id": p.id,
        "name": p.name,
        "kind": p.kind,
        "category": p.category,
        "description": p.description,
        "canonical_definition_name": p.canonical_definition_name,
        "default_interval_s": (
            int(p.default_interval.total_seconds()) if p.default_interval else None
        ),
        "required_settings": [
            {"key": s.key, "label": s.label, "kind": s.kind, "help": s.help,
             "enum_values": list(s.enum_values) if s.enum_values else None,
             "enum_labels": list(s.enum_labels) if s.enum_labels else None,
             "default": s.default, "required": s.required,
             "placeholder": s.placeholder}
            for s in p.required_settings
        ],
        "required_credentials": [
            {"key": c.key, "label": c.label, "help": c.help}
            for c in p.required_credentials
        ],
        "required_permissions": [
            {"id": perm.id, "explanation": perm.explanation}
            for perm in p.required_permissions
        ],
        "setup_steps": [
            {"kind": s.kind, "title": s.title, "body_md": s.body_md,
             "settings_keys": list(s.settings_keys),
             "external_link": s.external_link,
             "annotation_type": s.annotation_type}
            for s in p.setup_steps
        ],
        "health_check_available": p.health_check is not None,
        "permission_check_available": p.permission_check is not None,
    }
    parsed = json.loads(json.dumps(contract))
    assert parsed["id"] == "apple-tv"
    assert parsed["default_interval_s"] == 6 * 3600


# ---------------------------------------------------------------------------
# Run function
# ---------------------------------------------------------------------------

def _make_ctx(config: dict) -> RunContext:
    return RunContext(
        plugin_id="apple-tv",
        config=config,
        credentials={},
        state=PluginState("apple-tv"),
        log=logging.getLogger("t"),
        _emit=lambda e: None,
    )


def _bootstrapped_media_state() -> MediaState:
    return MediaState(
        watched_definition_id="def-watched-123",
        listened_definition_id="def-listened-456",
        read_definition_id="def-read-789",
    )


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


def test_plugin_run_parses_cache_and_imports(monkeypatch, tmp_path):
    fake_client = _FakeClient()
    monkeypatch.setattr("fulcra_media.plugins.apple_tv.FulcraClient",
                        lambda: fake_client)
    monkeypatch.setattr("fulcra_media.plugins.apple_tv._state_load",
                        lambda path: _bootstrapped_media_state())
    monkeypatch.setattr(
        "fulcra_media.plugins.apple_tv.apple_tv_importer.parse_cache",
        lambda cache_dir: ["ev-apple-tv"])

    ctx = _make_ctx({"cache_dir": str(tmp_path)})
    APPLE_TV_PLUGIN.run(ctx)

    assert fake_client.calls["imported"] == ["ev-apple-tv"]
    assert fake_client.calls["ensure_tag"] == "apple-tv"


def test_plugin_run_surfaces_snapshot_error_as_runtime_error(monkeypatch):
    from fulcra_media.importers.apple_tv import SnapshotError

    monkeypatch.setattr("fulcra_media.plugins.apple_tv._state_load",
                        lambda path: _bootstrapped_media_state())

    def _boom(cache_dir):
        raise SnapshotError("copy timed out")

    monkeypatch.setattr(
        "fulcra_media.plugins.apple_tv.apple_tv_importer.parse_cache", _boom)

    with pytest.raises(RuntimeError, match="App Group protection"):
        APPLE_TV_PLUGIN.run(_make_ctx({}))


def test_snapshot_timeout_is_fast_fail():
    """The snapshot deadline must stay short. This cache is ~1-2MB (a healthy
    clonefile is sub-second); a copy that runs seconds means macOS App Group
    protection is gating the open (a per-app TCC grant is missing and the
    open(2) blocks until the user answers the prompt), so we must fail fast
    and retry next interval rather than pin a worker for two minutes. Guards
    against a copy-paste back to podcasts' 120s (justified there by a 347MB
    library, not here)."""
    from fulcra_media.importers import apple_tv
    assert apple_tv.SNAPSHOT_TIMEOUT_SECONDS <= 30


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class _HealthCtx:
    def __init__(self, config):
        self.config = config


def test_health_check_missing_cache(tmp_path):
    result = apple_tv_health_check(_HealthCtx({"cache_dir": str(tmp_path / "nope")}))
    assert result.ok is False
    assert "open the TV app once" in result.summary


def test_health_check_reports_freshness_and_counts(tmp_path):
    upnext = gzip.compress(UPNEXT_FIXTURE.read_bytes())
    history = gzip.compress(HISTORY_FIXTURE.read_bytes())
    _make_cache_db(tmp_path, [
        (CANVAS_URL, "2026-07-06 10:00:00", 0, upnext),
        (CANVAS_URL + "&nextToken=10", "2026-07-06 10:00:05", 0, history),
    ])
    result = apple_tv_health_check(_HealthCtx({"cache_dir": str(tmp_path)}))
    assert result.ok is True
    assert "27 watch events" in result.summary
    assert "6 in progress" in result.summary
    assert "20 from history" in result.summary
    assert "2 Watch Now snapshots" in result.summary
    assert len(result.preview) == 3
    assert all({"title", "watched_at"} <= set(p) for p in result.preview)


def test_health_check_readable_but_empty_cache(tmp_path):
    _make_cache_db(tmp_path, [])
    result = apple_tv_health_check(_HealthCtx({"cache_dir": str(tmp_path)}))
    assert result.ok is True
    assert "no Watch Now snapshots" in result.summary
