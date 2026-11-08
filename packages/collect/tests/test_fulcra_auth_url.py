"""The served browser flow uses SDK grants and nonce-bound protocol 2."""
import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fulcra_collect import credentials, owned_auth
from fulcra_collect.daemon import Config, Daemon
from fulcra_collect.registry import RegistryResult
from fulcra_collect.web import _ensure_token, build_app
from collect_test_helpers import synthetic_account_token


def _build_daemon(collect_home):
    return Daemon(registry=RegistryResult(plugins={}), config=Config())


def _client(daemon):
    client = TestClient(build_app(daemon))
    client.headers['Authorization'] = f'Bearer {_ensure_token()}'
    return client


@pytest.fixture
def sdk(monkeypatch):
    provider = SimpleNamespace(get_device_code=lambda: (
        'synthetic-secret-device', 'https://fulcra.us.auth0.com/activate?user_code=TEST-CODE',
        'TEST-CODE', 900, 5), get_token=lambda *args: SimpleNamespace(
            access_token=synthetic_account_token(), refresh_token='synthetic-refresh'))
    monkeypatch.setattr(owned_auth, 'provider', lambda: provider)
    monkeypatch.setattr(owned_auth, 'validate_token', lambda token: None)
    return provider


def test_start_poll_uses_owned_sdk_and_never_cli(collect_home, _in_memory_keyring, monkeypatch, sdk, caplog):
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: pytest.fail('must not read CLI account'))
    client = _client(_build_daemon(collect_home))
    with caplog.at_level(logging.INFO):
        start = client.post('/api/fulcra/auth/cli_login_start').json()
        assert start['protocol'] == 2 and start['attempt_id'] and start['interval'] == 5
        response = client.post('/api/fulcra/auth/cli_login_poll', json={
            'protocol': 2, 'attempt_id': start['attempt_id'], 'device_code': start['device_code']})
    assert response.status_code == 200 and response.json() == {'ok': True, 'protocol': 2}
    assert credentials.get_user_secret('bearer-token') == synthetic_account_token()
    assert 'synthetic-secret-device' not in caplog.text and 'synthetic-refresh' not in caplog.text


@pytest.mark.parametrize('payload', [{}, {'device_code':'synthetic-device'},
    {'device_code':'synthetic-device','attempt_id':'not-a-uuid','protocol':2},
    {'device_code':'synthetic-device','attempt_id':'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee','protocol':1}])
def test_old_or_unknown_completion_has_no_effects(collect_home, _in_memory_keyring, sdk, payload):
    client = _client(_build_daemon(collect_home))
    sdk.get_token = lambda *args: pytest.fail('unscoped completion cannot exchange a grant')
    result = client.post('/api/fulcra/auth/cli_login_poll', json=payload)
    assert result.status_code in (409,422)
    assert credentials.get_user_secret('bearer-token') is None


def test_late_completion_after_real_signout_route_is_refused(collect_home, _in_memory_keyring, sdk):
    client = _client(_build_daemon(collect_home))
    start = client.post('/api/fulcra/auth/cli_login_start').json()
    original = sdk.get_token
    def late(*args):
        assert client.delete('/api/fulcra/auth/token').status_code == 200
        return original(*args)
    sdk.get_token = late
    result = client.post('/api/fulcra/auth/cli_login_poll', json={
        'protocol':2, 'attempt_id':start['attempt_id'], 'device_code':start['device_code']})
    assert result.status_code == 409
    assert credentials.get_user_secret('bearer-token') is None


def test_status_has_no_cli_dependency(collect_home, _in_memory_keyring, monkeypatch):
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: pytest.fail('no CLI probe'))
    client = _client(_build_daemon(collect_home))
    assert client.get('/api/fulcra/auth/cli_status').json() == {
        'available': True, 'signed_in': False, 'protocol':2}


@pytest.mark.parametrize('path', ['cli_status','cli_login','cli_login_start','cli_login_poll'])
def test_auth_routes_require_local_session(collect_home, path):
    client = TestClient(build_app(_build_daemon(collect_home)))
    method = client.get if path == 'cli_status' else client.post
    assert method('/api/fulcra/auth/'+path).status_code in (401,403)


def test_daemon_serves_wizard_with_auth_url_flow(collect_home):
    _ensure_token()
    client = TestClient(build_app(_build_daemon(collect_home)))
    assert 'src="/static/browser-signin.js?' in client.get('/').text
    response = client.get('/static/browser-signin.js')
    assert response.status_code == 200
    assert 'cli_login_start' in response.text and 'cli_login_poll' in response.text
    assert 'browserSignin' in client.get('/static/onboarding.js').text
    assert 'browserSignin' in client.get('/static/settings.js').text


def test_cancel_fences_grant_and_is_idempotent(collect_home, _in_memory_keyring, sdk):
    client = _client(_build_daemon(collect_home))
    start = client.post('/api/fulcra/auth/cli_login_start').json()
    sdk.get_token = lambda *args: pytest.fail('cancelled grant must not be consumed')
    for _ in range(2):
        assert client.post('/api/fulcra/auth/cancel', json={'attempt_id':start['attempt_id']}).status_code == 200
    result = client.post('/api/fulcra/auth/cli_login_poll', json={
        'protocol':2,'attempt_id':start['attempt_id'],'device_code':start['device_code']})
    assert result.status_code == 409
    assert credentials.get_user_secret('bearer-token') is None


def test_cancel_of_superseded_attempt_cannot_cancel_newer_one(collect_home, _in_memory_keyring, sdk):
    client = _client(_build_daemon(collect_home))
    old = client.post('/api/fulcra/auth/cli_login_start').json()
    new = client.post('/api/fulcra/auth/cli_login_start').json()
    client.post('/api/fulcra/auth/cancel', json={'attempt_id':old['attempt_id']})
    assert client.post('/api/fulcra/auth/cli_login_poll', json={
        'protocol':2,'attempt_id':new['attempt_id'],'device_code':new['device_code']}).status_code == 200
