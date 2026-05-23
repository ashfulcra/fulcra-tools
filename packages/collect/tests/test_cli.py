"""The fulcra-collect CLI."""
from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from fulcra_collect import config as config_mod
from fulcra_collect.cli import cli


def test_enable_then_disable_update_config(collect_home: Path):
    runner = CliRunner()
    assert runner.invoke(cli, ["enable", "lastfm"]).exit_code == 0
    assert config_mod.load().enabled == {"lastfm"}
    assert runner.invoke(cli, ["disable", "lastfm"]).exit_code == 0
    assert config_mod.load().enabled == set()


def test_set_interval_writes_an_override(collect_home: Path):
    res = CliRunner().invoke(cli, ["set-interval", "lastfm", "1800"])
    assert res.exit_code == 0
    assert config_mod.load().interval_overrides == {"lastfm": 1800}


def test_status_reports_when_the_daemon_is_not_running(collect_home: Path):
    res = CliRunner().invoke(cli, ["status"])
    # No daemon -> a clean message, non-zero exit, not a traceback.
    assert res.exit_code != 0
    assert "daemon" in res.output.lower()


def test_status_prints_a_snapshot_from_a_stub_daemon(collect_home: Path, monkeypatch):
    snapshot = {"ok": True, "plugins": [
        {"id": "lastfm", "name": "Last.fm", "kind": "scheduled", "enabled": True,
         "last_run": None, "last_outcome": None, "last_error": None,
         "consecutive_failures": 0},
    ], "load_errors": {}}
    monkeypatch.setattr("fulcra_collect.cli.send_request", lambda *a, **k: snapshot)
    res = CliRunner().invoke(cli, ["status"])
    assert res.exit_code == 0
    assert "lastfm" in res.output


def test_set_credential_stores_into_the_keychain(collect_home: Path, monkeypatch):
    stored = {}
    monkeypatch.setattr("fulcra_collect.cli.credentials.set_secret",
                        lambda pid, key, val: stored.update({(pid, key): val}))
    res = CliRunner().invoke(cli, ["set-credential", "lastfm", "api-key"],
                             input="SECRET123\n")
    assert res.exit_code == 0
    assert stored == {("lastfm", "api-key"): "SECRET123"}


def test_reset_definition_clears_cache(collect_home: Path, monkeypatch):
    from fulcra_collect import state

    st = state.PluginState(plugin_id="lastfm", definition_id="cached-uuid")
    state.save(st)

    result = CliRunner().invoke(cli, ["plugin", "reset-definition", "lastfm"])
    assert result.exit_code == 0, result.output

    after = state.load("lastfm")
    assert after.definition_id is None
