import base64
import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from fulcra_apple_reminders import collect_plugin as plugin


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "collect"))


def context(settings=None):
    from fulcra_collect import config
    from fulcra_collect.plugin import RunContext
    from fulcra_collect.state import PluginState
    settings = settings
    cfg = config.load()
    cfg.enable('apple-reminders')
    cfg.update_plugin_settings('apple-reminders', settings)
    config.save(cfg)
    cfg = config.load()
    ctx = RunContext(plugin_id='apple-reminders', config=settings, config_epoch=cfg.plugin_epochs['apple-reminders'],
        credentials={}, state=PluginState('apple-reminders'), log=logging.getLogger('synthetic'), _emit=lambda _:None)
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee', 'exp':4102444800}).encode()).decode().rstrip('=')
    ctx.collect_token = Mock(return_value='synthetic.' + claims + '.signature')
    ctx.fulcra_token = ctx.collect_token
    ctx.kv_get, ctx.kv_set, ctx.progress = Mock(return_value=None), Mock(), Mock()
    return ctx


def test_empty_selection_does_not_construct_provider_or_vault(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(plugin, "AppleRemindersProvider", factory)
    with pytest.raises(ValueError, match="list"):
        plugin.run(context({"selected_lists": []}))
    factory.assert_not_called()


def test_missing_collect_sign_in_prevents_source_access(monkeypatch):
    ctx = context({"selected_lists": ["synthetic-list"], "dry_run": True})
    ctx.collect_token.return_value = None
    factory = Mock(side_effect=AssertionError("source access before sign-in"))
    monkeypatch.setattr(plugin, "AppleRemindersProvider", factory)
    with pytest.raises(ValueError, match="Reconnect to Fulcra in Collect"):
        plugin.run(ctx)
    factory.assert_not_called()


@pytest.mark.parametrize("value", ["list-one", [""], ["one", "one"], [1], None])
def test_invalid_selection_is_rejected(value):
    with pytest.raises(ValueError):
        plugin.selected_lists(value)


def test_read_only_permission_probe_never_requests(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(plugin, "AppleRemindersProvider", lambda **kw: provider)
    plugin.permission_check(context({}))
    provider.permission_check.assert_called_once_with(request=False)


def test_native_permission_requires_explicit_callback(monkeypatch):
    provider = Mock()
    monkeypatch.setattr(plugin, "AppleRemindersProvider", lambda **kw: provider)
    plugin.permission_request(context({}))
    provider.permission_check.assert_called_once_with(request=True)


def test_options_discovery_does_not_run_sync(monkeypatch):
    provider = Mock()
    provider.collections.return_value = [
        SimpleNamespace(id="one", name="Example list", writable=True),
        SimpleNamespace(id="two", name="Read only", writable=False),
    ]
    monkeypatch.setattr(plugin, "AppleRemindersProvider", lambda **kw: provider)
    sync = Mock()
    monkeypatch.setattr(plugin, "run_sync", sync)
    assert plugin.setting_options(context({}), "selected_lists") == [
        {"value": "one", "label": "Example list", "disabled": False},
        {"value": "two", "label": "Read only", "disabled": True},
    ]
    sync.assert_not_called()


def test_run_forwards_preview_selection_and_state(monkeypatch):
    provider = Mock()
    vault = Mock()
    monkeypatch.setattr(plugin, "AppleRemindersProvider", lambda **kw: provider)
    monkeypatch.setattr(plugin, "FulcraVault", lambda **kw: vault)
    result = SimpleNamespace(
        counts={"imported": 2}, errors=[], remaining=0, partial=False
    )
    sync = Mock(return_value=result)
    monkeypatch.setattr(plugin, "run_sync", sync)
    ctx = context({"selected_lists": ["one"], "dry_run": True})
    plugin.run(ctx)
    args = sync.call_args
    assert args.kwargs["selected_ids"] == {"one"}
    assert args.kwargs["dry_run"] is True
    assert args.kwargs["load_state"] == ctx.kv_get
    assert args.kwargs["save_state"] == ctx.kv_set
    assert args.kwargs["still_selected"]() == {"one"}
    from fulcra_collect.config_leases import task_mutation_scope
    assert args.kwargs["mutation_scope"] is task_mutation_scope
    vault.close.assert_called_once()


def test_run_failure_is_reported_without_private_error(monkeypatch):
    monkeypatch.setattr(plugin, "AppleRemindersProvider", Mock())
    vault = Mock()
    monkeypatch.setattr(plugin, "FulcraVault", lambda **kw: vault)
    monkeypatch.setattr(
        plugin,
        "run_sync",
        Mock(
            return_value=SimpleNamespace(
                counts={}, errors=["private task title"], remaining=1, partial=False
            )
        ),
    )
    ctx = context({"selected_lists": ["one"], "dry_run": False})
    with pytest.raises(RuntimeError, match="1 task") as error:
        plugin.run(ctx)
    assert "private" not in str(error.value)
    vault.close.assert_called_once()


def test_setup_has_empty_selection_and_explicit_permission():
    settings = {s.key: s for s in plugin.PLUGIN.required_settings}
    assert settings["selected_lists"].default == []
    assert settings["dry_run"].default is True
    assert plugin.PLUGIN.permission_request is plugin.permission_request
    assert plugin.PLUGIN.setup_steps[-1].kind == "done"


@pytest.mark.parametrize('preview', [True, False])
@pytest.mark.parametrize('change', ['account', 'settings', 'disable'])
def test_actual_caller_injects_read_fences_for_captured_configuration(monkeypatch, preview, change):
    from fulcra_collect import config
    ctx = context({'selected_lists': ['one'], 'dry_run': preview})
    callbacks = []
    def source(*a, **kw):
        callbacks.append(kw['still_allowed'])
        return SimpleNamespace(close=Mock())
    monkeypatch.setattr(plugin, 'AppleRemindersProvider', source)
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
                changed.plugin_settings['apple-reminders']['unrelated'] = 'synthetic-change'
            monkeypatch.setattr(config, 'load', lambda: changed)
        callbacks[0]()
        pytest.fail('source request continued after revocation')
    monkeypatch.setattr(plugin, 'run_sync', sync)
    with pytest.raises(RuntimeError, match='changed'):
        plugin.run(ctx)
    ctx.progress.assert_not_called()
