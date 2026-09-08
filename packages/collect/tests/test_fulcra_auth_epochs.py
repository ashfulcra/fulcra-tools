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
    cfg.plugin_settings['todoist'] = {'selected_projects': ['synthetic-project']}
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
    client, before = prepared_client()
    credentials.set_user_secret('bearer-token', 'synthetic-before-token')
    observed = []
    set_secret, delete_secret = credentials.set_user_secret, credentials.delete_user_secret
    def assert_invalidated():
        current = config.load().plugin_epochs
        assert all(current[plugin] != epoch for plugin, epoch in before.items())
        observed.append(current)
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
    monkeypatch.setattr(config, 'invalidate_all_plugin_work', fail_invalidation)
    with pytest.raises(OSError, match='synthetic unavailable configuration store'):
        if method == 'post':
            client.post('/api/fulcra/auth/token', json={'token': 'synthetic-new-token'})
        else:
            client.delete('/api/fulcra/auth/token')
    assert credentials.get_user_secret('bearer-token') == 'synthetic-before-token'
    assert config.load().plugin_epochs == before
