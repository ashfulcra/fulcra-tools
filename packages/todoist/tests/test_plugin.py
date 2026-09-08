from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fulcra_todoist import collect_plugin as plugin


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "collect"))


def context(settings=None):
    return SimpleNamespace(
        config=settings or {"selected_projects": ["one"], "dry_run": True},
        credentials={"api_token": "synthetic-token"},
        kv_get=Mock(),
        kv_set=Mock(),
        fulcra_token=Mock(return_value="synthetic-fulcra"),
        progress=Mock(),
    )


def test_empty_projects_and_missing_token_prevent_network(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(plugin, "TodoistProvider", factory)
    ctx = context({"selected_projects": [], "dry_run": True})
    with pytest.raises(ValueError):
        plugin.run(ctx)
    factory.assert_not_called()
    ctx = context()
    ctx.credentials = {}
    with pytest.raises(ValueError):
        plugin.run(ctx)
    factory.assert_not_called()


def test_project_discovery_is_read_only_and_closes_provider(monkeypatch):
    provider = Mock()
    provider.collections.return_value = [
        SimpleNamespace(id="one", name="Example project", writable=True)
    ]
    factory = Mock(return_value=provider)
    monkeypatch.setattr(plugin, "TodoistProvider", factory)
    assert plugin.setting_options(context(), "selected_projects") == [
        {"value": "one", "label": "Example project", "disabled": False}
    ]
    assert factory.call_args.kwargs["dry_run"] is True
    provider.close.assert_called_once()


def test_run_scopes_provider_observations_and_preview(monkeypatch):
    provider = Mock()
    vault = Mock()
    ctx = context()
    factory = Mock(return_value=provider)
    monkeypatch.setattr(plugin, "TodoistProvider", factory)
    monkeypatch.setattr(plugin, "FulcraVault", lambda token: vault)
    sync = Mock(return_value=SimpleNamespace(counts={}, errors=[], remaining=0, partial=False))
    monkeypatch.setattr(plugin, "run_sync", sync)
    plugin.run(ctx)
    assert factory.call_args.kwargs["load_state"] == ctx.kv_get
    assert factory.call_args.kwargs["save_state"] == ctx.kv_set
    assert factory.call_args.kwargs["dry_run"] is True
    assert sync.call_args.kwargs["selected_ids"] == {"one"}
    assert sync.call_args.kwargs["dry_run"] is True
    provider.close.assert_called_once()
    vault.close.assert_called_once()


def test_reconnect_disablement_and_preview_stop_writes(monkeypatch):
    cfg = SimpleNamespace(
        enabled={"todoist"},
        plugin_settings={"todoist": {"selected_projects": ["one"], "dry_run": False}},
    )
    monkeypatch.setattr(plugin.collect_config, "load", lambda: cfg)
    monkeypatch.setattr(plugin.collect_credentials, "get_secret", lambda *args: "current-token")
    assert plugin.current_selection("current-token") == {"one"}
    assert plugin.current_selection("old-token") == set()
    cfg.plugin_settings["todoist"]["dry_run"] = True
    assert plugin.current_selection("current-token") == set()
    cfg.plugin_settings["todoist"]["dry_run"] = False
    cfg.enabled.clear()
    assert plugin.current_selection("current-token") == set()


def test_source_error_is_not_echoed_and_clients_close(monkeypatch):
    provider = Mock()
    vault = Mock()
    monkeypatch.setattr(plugin, "TodoistProvider", Mock(return_value=provider))
    monkeypatch.setattr(plugin, "FulcraVault", lambda token: vault)
    monkeypatch.setattr(
        plugin,
        "run_sync",
        Mock(
            return_value=SimpleNamespace(
                counts={}, errors=["private account error"], remaining=1, partial=True
            )
        ),
    )
    with pytest.raises(RuntimeError) as error:
        plugin.run(context())
    assert "private account" not in str(error.value)
    provider.close.assert_called_once()
    vault.close.assert_called_once()


def test_token_is_credential_and_projects_default_empty():
    assert plugin.PLUGIN.required_credentials[0].key == "api_token"
    assert plugin.PLUGIN.required_credentials[0].user_level is False
    settings = {s.key: s for s in plugin.PLUGIN.required_settings}
    assert "api_token" not in settings
    assert settings["selected_projects"].default == []
    assert settings["dry_run"].default is True
