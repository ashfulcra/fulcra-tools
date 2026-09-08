"""Real concurrent config edits surface as actionable errors, without values."""
from datetime import timedelta

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from fulcra_collect import cli as cli_module, config, web
from fulcra_collect.daemon import Daemon
from fulcra_collect.plugin import Plugin, Setting
from fulcra_collect.registry import RegistryResult


def synthetic_plugin():
    return Plugin(id='demo', name='Synthetic plugin', kind='scheduled',
                  collect_mode='live_polled', default_interval=timedelta(minutes=5),
                  run=lambda ctx: None, required_settings=(
                      Setting(key='choice', label='Choice', kind='text'),
                      Setting(key='dry_run', label='Preview', kind='toggle'),
                  ))


def seed():
    cfg = config.load()
    cfg.plugin_settings['demo'] = {'choice': 'synthetic-initial-value', 'dry_run': True}
    config.save(cfg)


def competing_save(monkeypatch, key='choice', value='synthetic-concurrent-value'):
    original_save = config.save
    def interleave(requested):
        concurrent = config.load()
        concurrent.plugin_settings['demo'][key] = value
        original_save(concurrent)
        original_save(requested)
    monkeypatch.setattr(config, 'save', interleave)


def test_web_conflicting_settings_return_409_without_reloading(collect_home, monkeypatch):
    seed()
    daemon = Daemon(registry=RegistryResult(plugins={'demo': synthetic_plugin()}), config=config.load())
    client = TestClient(web.build_app(daemon), raise_server_exceptions=False)
    client.headers['Authorization'] = f'Bearer {web._ensure_token()}'
    reloads = []
    monkeypatch.setattr(daemon, 'handle_request', lambda request: reloads.append(request))
    competing_save(monkeypatch)
    response = client.put('/api/plugin/demo/settings', json={'choice': 'synthetic-request-value'})
    assert response.status_code == 409
    assert 'reload' in response.json()['detail'].lower()
    assert 'synthetic-' not in response.text
    assert config.load().plugin_settings['demo']['choice'] == 'synthetic-concurrent-value'
    assert not reloads


def test_cli_conflicting_settings_explain_retry_without_reloading(collect_home, monkeypatch):
    seed()
    monkeypatch.setattr(cli_module.registry, 'discover', lambda: RegistryResult(
        plugins={'demo': synthetic_plugin()}))
    reloads = []
    monkeypatch.setattr(cli_module, 'send_request', lambda *args: reloads.append(args))
    competing_save(monkeypatch)
    result = CliRunner().invoke(cli_module.cli, ['set-setting', 'demo', 'choice', 'synthetic-request-value'])
    assert result.exit_code == 1
    assert 'retry' in result.output.lower()
    assert 'synthetic-' not in result.output
    assert config.load().plugin_settings['demo']['choice'] == 'synthetic-concurrent-value'
    assert not reloads


def test_cli_explicit_preview_reassertion_cannot_silently_keep_sync_enabled(collect_home, monkeypatch):
    seed()
    monkeypatch.setattr(cli_module.registry, 'discover', lambda: RegistryResult(
        plugins={'demo': synthetic_plugin()}))
    monkeypatch.setattr(cli_module, 'send_request', lambda *args: {'ok': True})
    competing_save(monkeypatch, key='dry_run', value=False)
    result = CliRunner().invoke(cli_module.cli, ['set-setting', 'demo', 'dry_run', 'true'])
    assert result.exit_code == 1
    assert 'retry' in result.output.lower()
    assert config.load().plugin_settings['demo']['dry_run'] is False


@pytest.mark.parametrize('command', ['enable', 'disable', 'set-interval'])
def test_cli_other_config_commands_surface_real_conflicts(collect_home, monkeypatch, command):
    cfg = config.load()
    if command == 'enable':
        cfg.enable('demo')
    cfg.set_interval('demo', 60)
    config.save(cfg)
    original_save = config.save
    def competing_change(requested):
        current = config.load()
        if command == 'enable':
            current.disable('demo')
        elif command == 'disable':
            current.enable('demo')
        else:
            current.set_interval('demo', 180)
        original_save(current)
        original_save(requested)
    monkeypatch.setattr(config, 'save', competing_change)
    reloads = []
    monkeypatch.setattr(cli_module, 'send_request', lambda *args: reloads.append(args))
    args = [command, 'demo'] + (['120'] if command == 'set-interval' else [])
    result = CliRunner().invoke(cli_module.cli, args)
    assert result.exit_code == 1
    assert 'retry' in result.output.lower()
    assert not reloads


def test_non_conflict_storage_error_is_not_mislabeled_as_concurrent_edit(collect_home, monkeypatch):
    def fail_storage(_):
        raise OSError('synthetic storage failure')
    monkeypatch.setattr(config, 'save', fail_storage)
    result = CliRunner().invoke(cli_module.cli, ['set-interval', 'demo', '60'])
    assert isinstance(result.exception, OSError)
    assert 'configuration changed' not in result.output.lower()
