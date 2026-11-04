"""The Day One fulcra-collect plugin."""
from __future__ import annotations

import logging

import pytest

from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

from fulcra_dayone.collect_plugin import PLUGIN, dayone_permission_check


def _ctx(config: dict) -> RunContext:
    return RunContext(plugin_id="dayone", config=config, credentials={},
                      state=PluginState("dayone"), log=logging.getLogger("t"),
                      _emit=lambda e: None)


def test_plugin_metadata_is_scheduled():
    # Switched from manual to scheduled (every 6 h) so live-app mode picks
    # up new entries automatically; export-file users can still trigger via
    # Run Now and the run() naturally no-ops when there's nothing new.
    from datetime import timedelta
    assert PLUGIN.id == "dayone"
    assert PLUGIN.kind == "scheduled"
    assert PLUGIN.default_interval == timedelta(hours=6)


def test_full_disk_access_guidance_names_the_installed_app():
    body = "\n".join(step.body_md for step in PLUGIN.setup_steps)
    assert "Fulcra Collect" in body
    assert "terminal you're running" not in body
    assert "once it exists" not in body


def test_permission_failure_names_the_installed_app(tmp_path, monkeypatch):
    db = tmp_path / "DayOne.sqlite"
    db.touch()

    def deny_access(*args, **kwargs):
        import sqlite3
        raise sqlite3.OperationalError("authorization denied")

    monkeypatch.setattr("sqlite3.connect", deny_access)

    result = dayone_permission_check(_ctx({
        "local_db": "live_app",
        "db_path": str(db),
    }))

    assert result["granted"] is False
    assert "Fulcra Collect" in result["hint"]
    assert "source install" in result["hint"]


def test_local_db_mode_runs_the_pipeline(monkeypatch):
    seen = {}
    monkeypatch.setattr("fulcra_dayone.collect_plugin.read",
                        lambda source, local_db, db_path: ["entry"])
    monkeypatch.setattr("fulcra_dayone.collect_plugin.select",
                        lambda entries, **kw: list(entries))
    monkeypatch.setattr("fulcra_dayone.collect_plugin.to_event",
                        lambda e: f"event-{e}")

    class FakeResult:
        posted = 1
        skipped_existing = 0
        verified = 1

    class FakeClient:
        def ensure_journal_definition(self):
            return "def-journal"
        def ensure_tag(self, name):
            return f"tag-{name}"
        def run_import(self, events, definition_id, tag_id_for):
            seen["events"] = list(events)
            seen["definition_id"] = definition_id
            return FakeResult()

    monkeypatch.setattr("fulcra_dayone.collect_plugin.DayOneFulcraClient",
                        lambda: FakeClient())

    PLUGIN.run(_ctx({"local_db": True}))
    assert seen["events"] == ["event-entry"]
    assert seen["definition_id"] == "def-journal"


def test_missing_source_config_raises_a_clear_error():
    with pytest.raises(RuntimeError, match="local_db.*path"):
        PLUGIN.run(_ctx({}))
