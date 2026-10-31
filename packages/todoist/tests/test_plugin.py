import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fulcra_todoist import collect_plugin as plugin


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "collect"))
    monkeypatch.setattr(plugin.collect_credentials, "get_secret", lambda *a: "synthetic-token")


def context(settings=None):
    from fulcra_collect import config
    from fulcra_collect.plugin import RunContext
    from fulcra_collect.state import PluginState
    settings = settings or {'selected_projects': ['one'], 'dry_run': True}
    cfg = config.load()
    cfg.enable('todoist')
    cfg.update_plugin_settings('todoist', settings)
    config.save(cfg)
    cfg = config.load()
    ctx = RunContext(plugin_id='todoist', config=settings, config_epoch=cfg.plugin_epochs['todoist'],
        credentials={'api_token': 'synthetic-token'}, state=PluginState('todoist'), log=logging.getLogger('synthetic'), _emit=lambda _:None)
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', 'exp':4102444800}).encode()).decode().rstrip('=')
    ctx.collect_token = Mock(return_value='synthetic.' + claims + '.signature')
    ctx.fulcra_token = ctx.collect_token
    ctx.kv_get, ctx.kv_set, ctx.progress = Mock(return_value=None), Mock(), Mock()
    return ctx


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


def test_missing_collect_sign_in_prevents_source_access(monkeypatch):
    ctx = context()
    ctx.collect_token.return_value = None
    factory = Mock(side_effect=AssertionError("source access before sign-in"))
    monkeypatch.setattr(plugin, "TodoistProvider", factory)
    with pytest.raises(ValueError, match="Reconnect to Fulcra in Collect"):
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
    monkeypatch.setattr(plugin, "FulcraVault", lambda **kw: vault)
    sync = Mock(return_value=SimpleNamespace(counts={}, errors=[], remaining=0, partial=False))
    monkeypatch.setattr(plugin, "run_sync", sync)
    plugin.run(ctx)
    assert factory.call_args.kwargs["load_state"] == ctx.kv_get
    assert factory.call_args.kwargs["save_state"] == ctx.kv_set
    assert factory.call_args.kwargs["dry_run"] is True
    assert sync.call_args.kwargs["selected_ids"] == {"one"}
    from fulcra_collect.config_leases import task_mutation_scope
    assert sync.call_args.kwargs["mutation_scope"] is task_mutation_scope
    assert sync.call_args.kwargs["dry_run"] is True
    provider.close.assert_called_once()
    vault.close.assert_called_once()


def test_source_error_is_not_echoed_and_clients_close(monkeypatch):
    provider = Mock()
    vault = Mock()
    monkeypatch.setattr(plugin, "TodoistProvider", Mock(return_value=provider))
    monkeypatch.setattr(plugin, "FulcraVault", lambda **kw: vault)
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


@pytest.mark.parametrize('preview', [True, False])
@pytest.mark.parametrize('change', ['account', 'settings', 'disable', 'source-token'])
def test_actual_caller_injects_read_fences_for_captured_configuration(monkeypatch, preview, change):
    from fulcra_collect import config
    ctx = context({'selected_projects': ['one'], 'dry_run': preview})
    callbacks = []
    def source(*a, **kw):
        callbacks.append(kw['still_allowed'])
        return SimpleNamespace(close=Mock())
    monkeypatch.setattr(plugin, 'TodoistProvider', source)
    monkeypatch.setattr(plugin, 'FulcraVault', lambda **kw: SimpleNamespace(close=Mock()))
    def sync(**kw):
        assert kw['still_selected']() == {'one'}
        if change == 'account':
            claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'}).encode()).decode().rstrip('=')
            ctx.collect_token.return_value = 'synthetic.' + claims + '.signature'
        elif change == 'source-token':
            monkeypatch.setattr(plugin.collect_credentials, 'get_secret', lambda *a: 'synthetic-other-token')
        else:
            changed = config.load()
            if change == 'disable':
                changed.enabled.clear()
            else:
                changed.plugin_settings['todoist']['unrelated'] = 'synthetic-change'
            monkeypatch.setattr(config, 'load', lambda: changed)
        callbacks[0]()
        pytest.fail('source request continued after revocation')
    monkeypatch.setattr(plugin, 'run_sync', sync)
    with pytest.raises(RuntimeError, match='changed'):
        plugin.run(ctx)
    ctx.progress.assert_not_called()


def test_actual_engine_does_not_recheck_keychain_for_each_materialized_version(monkeypatch):
    from fulcra_task_sync.models import Collection, Task
    ctx = context({'selected_projects': ['one'], 'dry_run': True})
    source = SimpleNamespace(namespace='synthetic-source', name='todoist', close=Mock(),
        collections=lambda:[Collection('one', 'Synthetic')],
        tasks=lambda selected:[Task('synthetic-task', 'one', 'Synthetic', '', False)])
    monkeypatch.setattr(plugin, 'TodoistProvider', lambda *a, **kw:source)
    counts = []
    def versions(path):
        counts.append(ctx.collect_token.call_count)
        return [f'synthetic-version-{i}' for i in range(500)]
    def read_version(version):
        counts.append(ctx.collect_token.call_count)
        raise RuntimeError('synthetic stop after measuring metadata iteration')
    vault = SimpleNamespace(namespace='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', close=Mock(),
        versions=versions, read_version=read_version)
    monkeypatch.setattr(plugin, 'FulcraVault', lambda **kw:vault)
    with pytest.raises(RuntimeError, match='task sync'):
        plugin.run(ctx)
    assert len(counts) == 2
    assert counts[1] - counts[0] <= 10
