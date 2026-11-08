"""Synthetic interactive account transitions revoke pending plugin consent."""
import subprocess
import logging
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from fulcra_collect import config, credentials, web, owned_auth
from types import SimpleNamespace
from fulcra_collect.daemon import Daemon
from fulcra_collect.registry import RegistryResult
from collect_test_helpers import synthetic_account_token
from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState
from fulcra_collect.v1_session import CollectRecordSession

_BEFORE_TOKEN = synthetic_account_token(marker='before')
_NEW_TOKEN = synthetic_account_token(marker='new')


def task_selection(plugin_id, epoch):
    """Exercise the shared authority used by the actual task callers."""
    cfg = config.load()
    ctx = RunContext(plugin_id=plugin_id, config=cfg.plugin_settings[plugin_id],
        config_epoch=epoch, credentials={}, state=PluginState(plugin_id),
        log=logging.getLogger('synthetic'), _emit=lambda _:None)
    try:
        with CollectRecordSession(ctx) as session:
            session.guard(mutating=True)
    except (RuntimeError, ValueError):
        return set()
    key = 'selected_lists' if plugin_id == 'apple-reminders' else 'selected_projects'
    return set(ctx.config[key])


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
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(json.dumps({'userid': credentials._token_principal(_NEW_TOKEN)}).encode())))))


@pytest.mark.parametrize('flow', ['paste', 'poll', 'clear'])
def test_interactive_auth_invalidates_all_epochs_before_token_mutation(
        collect_home, _in_memory_keyring, monkeypatch, flow):
    client, before = prepared_client()
    credentials.set_user_secret('bearer-token', _BEFORE_TOKEN)
    credentials.set_secret('todoist', 'api_token', 'synthetic-todoist-token')
    observed = []
    set_secret, delete_secret = credentials.set_user_secret, credentials.delete_user_secret
    def assert_invalidated():
        # A newly started worker captures the *during-transition* epoch and
        # cached old token here, inside the real token-mutation boundary.
        current = config.load().plugin_epochs
        assert all(current[plugin] != epoch for plugin, epoch in before.items())
        observed.append(current)
        assert task_selection('apple-reminders', current['apple-reminders']) == set()
        assert task_selection('todoist', current['todoist']) == set()
    def set_after_invalidation(key, value):
        assert key in {'bearer-token', 'oauth-credentials'}
        if key == 'bearer-token':
            assert_invalidated()
        return set_secret(key, value)
    def delete_after_invalidation(key):
        assert key in {'bearer-token', 'oauth-credentials'}
        if key == 'bearer-token':
            assert_invalidated()
        return delete_secret(key)
    monkeypatch.setattr(credentials, 'set_user_secret', set_after_invalidation)
    monkeypatch.setattr(credentials, 'delete_user_secret', delete_after_invalidation)
    validation_success(monkeypatch)
    monkeypatch.setattr(owned_auth, 'provider', lambda: SimpleNamespace(
        get_device_code=lambda: ('synthetic-device', 'https://fulcra.us.auth0.com/activate?user_code=TEST-CODE', 'TEST-CODE', 900, 5),
        get_token=lambda *args: SimpleNamespace(access_token=_NEW_TOKEN, refresh_token='synthetic-refresh')))
    if flow == 'paste':
        response = client.post('/api/fulcra/auth/token', json={'token': _NEW_TOKEN})
    elif flow == 'clear':
        response = client.delete('/api/fulcra/auth/token')
    else:
        start = client.post('/api/fulcra/auth/cli_login_start').json()
        response = client.post('/api/fulcra/auth/cli_login_poll', json={
            'protocol': 2, 'attempt_id': start['attempt_id'], 'device_code': start['device_code']})
    assert response.status_code == 200
    assert len(observed) == 1
    assert task_selection('apple-reminders', observed[0]['apple-reminders']) == set()
    assert task_selection('todoist', observed[0]['todoist']) == set()
    after = config.load()
    assert after.account_transition is False
    assert task_selection('apple-reminders', after.plugin_epochs['apple-reminders']) == (set() if flow == 'clear' else {'synthetic-list'})
    assert task_selection('todoist', after.plugin_epochs['todoist']) == (set() if flow == 'clear' else {'synthetic-project'})
    assert credentials.get_user_secret('bearer-token') == (
        None if flow == 'clear' else _NEW_TOKEN)


def test_automatic_refresh_preserves_active_plugin_epochs(
        collect_home, _in_memory_keyring, monkeypatch):
    _, before = prepared_client()
    credentials.set_user_secret('bearer-token', synthetic_account_token(exp=1, marker='expired'))
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: '/synthetic/fulcra')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, stdout=synthetic_account_token(marker='refreshed'), stderr=''))
    assert credentials.refresh_fulcra_access_token() == synthetic_account_token(marker='refreshed')
    assert config.load().plugin_epochs == before


@pytest.mark.parametrize('method', ['post', 'delete'])
def test_failed_epoch_invalidation_prevents_interactive_token_change(
        collect_home, _in_memory_keyring, monkeypatch, method):
    client, before = prepared_client()
    credentials.set_user_secret('bearer-token', _BEFORE_TOKEN)
    validation_success(monkeypatch)
    def fail_invalidation():
        raise OSError('synthetic unavailable configuration store')
    monkeypatch.setattr(config, 'fulcra_account_transition', fail_invalidation)
    response = (client.post('/api/fulcra/auth/token', json={'token': _NEW_TOKEN})
                if method == 'post' else client.delete('/api/fulcra/auth/token'))
    assert response.status_code == 409
    assert credentials.get_user_secret('bearer-token') == _BEFORE_TOKEN
    assert config.load().plugin_epochs == before


def test_failed_transition_completion_keeps_task_writes_blocked_after_token_changes(
        collect_home, _in_memory_keyring, monkeypatch):
    client, _ = prepared_client()
    credentials.set_user_secret('bearer-token', _BEFORE_TOKEN)
    credentials.set_secret('todoist', 'api_token', 'synthetic-todoist-token')
    validation_success(monkeypatch)
    atomic_write = config._atomic_write
    def fail_final_save(path, text):
        if config.tomlkit.parse(text).get('account_transition') is False:
            raise OSError('synthetic failed transition completion')
        atomic_write(path, text)
    monkeypatch.setattr(config, '_atomic_write', fail_final_save)
    response = client.post('/api/fulcra/auth/token', json={'token': _NEW_TOKEN})
    assert response.status_code == 409
    current = config.load()
    assert credentials.get_user_secret('bearer-token') == _NEW_TOKEN
    assert current.account_transition is True
    availability = client.get('/api/fulcra/auth/status').json()
    assert availability['authenticated'] is None
    assert availability['availability'] == 'transition'
    assert availability['refresh_failed'] is False
    assert task_selection('apple-reminders', current.plugin_epochs['apple-reminders']) == set()
    assert task_selection('todoist', current.plugin_epochs['todoist']) == set()
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: '/synthetic/fulcra')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kwargs: subprocess.CompletedProcess(
        args, 0, stdout='synthetic-refreshed-token\n', stderr=''))
    assert credentials.refresh_fulcra_access_token() is None
    assert credentials.get_user_secret('bearer-token') == _NEW_TOKEN
    assert config.load().account_transition is True
    assert config.load().plugin_epochs == current.plugin_epochs
