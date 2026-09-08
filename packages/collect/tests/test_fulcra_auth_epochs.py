"""Synthetic interactive account transitions revoke pending plugin consent."""
import subprocess

import httpx
import pytest
from fastapi.testclient import TestClient

from fulcra_collect import config, credentials, web
from fulcra_collect.daemon import Daemon
from fulcra_collect.registry import RegistryResult


def prepared_client():
    cfg = config.load()
    cfg.enable('apple-reminders')
    cfg.enable('todoist')
    cfg.plugin_settings['apple-reminders'] = {
        'selected_lists': ['synthetic-list'], 'dry_run': False}
    cfg.plugin_settings['todoist'] = {
        'selected_projects': ['synthetic-project'], 'dry_run': False}
    cfg.rotate_plugin_epoch('previous-task-source')
    config.save(cfg)
    daemon = Daemon(registry=RegistryResult(plugins={}), config=cfg)
    client = TestClient(web.build_app(daemon))
    client.headers['Authorization'] = f'Bearer {web._ensure_token()}'
    return client, dict(config.load().plugin_epochs)


def validation_success(monkeypatch):
    client_class = httpx.Client
    monkeypatch.setattr(web.httpx, 'Client', lambda **kwargs: client_class(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[]))))


@pytest.mark.parametrize('flow', ['paste', 'classic', 'poll', 'clear'])
def test_interactive_auth_invalidates_all_epochs_before_token_mutation(
        collect_home, _in_memory_keyring, monkeypatch, flow):
    from fulcra_apple_reminders.collect_plugin import current_selection as reminders_selection
    from fulcra_todoist.collect_plugin import current_selection as todoist_selection

    client, before = prepared_client()
    credentials.set_user_secret('bearer-token', 'synthetic-before-token')
    credentials.set_secret('todoist', 'api_token', 'synthetic-todoist-token')
    observed = []
    set_secret, delete_secret = credentials.set_user_secret, credentials.delete_user_secret
    def assert_invalidated():
        # A newly started worker captures the *during-transition* epoch and
        # cached old token here, inside the real token-mutation boundary.
        current = config.load().plugin_epochs
        assert all(current[plugin] != epoch for plugin, epoch in before.items())
        observed.append(current)
        assert reminders_selection(current['apple-reminders']) == set()
        assert todoist_selection('synthetic-todoist-token', current['todoist']) == set()
    def set_after_invalidation(key, value):
        assert key == 'bearer-token'
        assert_invalidated()
        return set_secret(key, value)
    def delete_after_invalidation(key):
        assert key == 'bearer-token'
        assert_invalidated()
        return delete_secret(key)
    monkeypatch.setattr(credentials, 'set_user_secret', set_after_invalidation)
    monkeypatch.setattr(credentials, 'delete_user_secret', delete_after_invalidation)
    validation_success(monkeypatch)
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: '/synthetic/fulcra')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, stdout='synthetic-new-token\n' if 'print-access-token' in args else '', stderr=''))
    if flow == 'paste':
        response = client.post('/api/fulcra/auth/token', json={'token': 'synthetic-new-token'})
    elif flow == 'clear':
        response = client.delete('/api/fulcra/auth/token')
    elif flow == 'classic':
        response = client.post('/api/fulcra/auth/cli_login')
    else:
        response = client.post('/api/fulcra/auth/cli_login_poll',
                               json={'device_code': 'synthetic-device-code'})
    assert response.status_code == 200
    assert len(observed) == 1
    assert reminders_selection(observed[0]['apple-reminders']) == set()
    assert todoist_selection('synthetic-todoist-token', observed[0]['todoist']) == set()
    after = config.load()
    assert after.account_transition is False
    assert reminders_selection(after.plugin_epochs['apple-reminders']) == {'synthetic-list'}
    assert todoist_selection('synthetic-todoist-token', after.plugin_epochs['todoist']) == {'synthetic-project'}
    assert credentials.get_user_secret('bearer-token') == (
        None if flow == 'clear' else 'synthetic-new-token')


def test_automatic_refresh_preserves_active_plugin_epochs(
        collect_home, _in_memory_keyring, monkeypatch):
    _, before = prepared_client()
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: '/synthetic/fulcra')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, stdout='synthetic-refreshed-token\n', stderr=''))
    assert credentials.refresh_fulcra_access_token() == 'synthetic-refreshed-token'
    assert config.load().plugin_epochs == before


@pytest.mark.parametrize('method', ['post', 'delete'])
def test_failed_epoch_invalidation_prevents_interactive_token_change(
        collect_home, _in_memory_keyring, monkeypatch, method):
    client, before = prepared_client()
    credentials.set_user_secret('bearer-token', 'synthetic-before-token')
    validation_success(monkeypatch)
    def fail_invalidation():
        raise OSError('synthetic unavailable configuration store')
    monkeypatch.setattr(config, 'fulcra_account_transition', fail_invalidation)
    with pytest.raises(OSError, match='synthetic unavailable configuration store'):
        if method == 'post':
            client.post('/api/fulcra/auth/token', json={'token': 'synthetic-new-token'})
        else:
            client.delete('/api/fulcra/auth/token')
    assert credentials.get_user_secret('bearer-token') == 'synthetic-before-token'
    assert config.load().plugin_epochs == before


def test_failed_transition_completion_keeps_task_writes_blocked_after_token_changes(
        collect_home, _in_memory_keyring, monkeypatch):
    from fulcra_apple_reminders.collect_plugin import current_selection as reminders_selection
    from fulcra_todoist.collect_plugin import current_selection as todoist_selection

    client, _ = prepared_client()
    credentials.set_user_secret('bearer-token', 'synthetic-before-token')
    credentials.set_secret('todoist', 'api_token', 'synthetic-todoist-token')
    validation_success(monkeypatch)
    atomic_write = config._atomic_write
    def fail_final_save(path, text):
        if config.tomlkit.parse(text).get('account_transition') is False:
            raise OSError('synthetic failed transition completion')
        atomic_write(path, text)
    monkeypatch.setattr(config, '_atomic_write', fail_final_save)
    with pytest.raises(OSError, match='synthetic failed transition completion'):
        client.post('/api/fulcra/auth/token', json={'token': 'synthetic-new-token'})
    current = config.load()
    assert credentials.get_user_secret('bearer-token') == 'synthetic-new-token'
    assert current.account_transition is True
    assert reminders_selection(current.plugin_epochs['apple-reminders']) == set()
    assert todoist_selection('synthetic-todoist-token', current.plugin_epochs['todoist']) == set()
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: '/synthetic/fulcra')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, stdout='synthetic-refreshed-token\n', stderr=''))
    assert credentials.refresh_fulcra_access_token() == 'synthetic-refreshed-token'
    assert config.load().account_transition is True
    assert config.load().plugin_epochs == current.plugin_epochs
