"""Synthetic SDK device-grant ownership and bounded polling."""
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from fulcra_collect import credentials
from fulcra_collect.auth_attempts import AuthAttempts, AuthAttemptError
from fulcra_collect.owned_auth import CollectAuth
from collect_test_helpers import synthetic_account_token

TOKEN = synthetic_account_token(marker='owned-auth')


class Provider:
    def get_device_code(self):
        return ('synthetic-device', 'https://fulcra.us.auth0.com/activate?user_code=TEST-CODE',
                'TEST-CODE', 900, 5)

    def get_token(self, grant, payload):
        assert grant == 'urn:ietf:params:oauth:grant-type:device_code'
        assert payload == {'device_code': 'synthetic-device'}
        return SimpleNamespace(access_token=TOKEN, refresh_token='synthetic-refresh')


def service(collect_home, *, provider=None, validate=lambda token: None, clock=lambda: 1000):
    return CollectAuth(journal=AuthAttempts(collect_home / 'auth-attempts.sqlite3', clock=clock),
                       provider=provider or Provider(), validate=validate)


def test_owned_device_flow_never_reads_cli_store(collect_home, _in_memory_keyring, monkeypatch):
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: pytest.fail('no CLI adoption'))
    actor = service(collect_home)
    start = actor.start()
    assert start['protocol'] == 2 and start['expires_in'] == 900
    assert credentials.get_user_secret_fresh('bearer-token') is None
    assert actor.poll(start['attempt_id'], start['device_code']) == {'ok': True, 'protocol': 2}
    assert credentials.get_user_secret_fresh('bearer-token') == TOKEN
    owned = json.loads(credentials.get_user_secret_fresh('oauth-credentials'))
    assert owned == {'version': 1, 'attempt_id': start['attempt_id'],
                     'access_token': TOKEN, 'refresh_token': 'synthetic-refresh'}
    with pytest.raises(AuthAttemptError):
        actor.poll(start['attempt_id'], start['device_code'])


@pytest.mark.parametrize('replacement', ['token', 'clear', 'device'])
def test_late_provider_response_never_overwrites_newer_intent(
        collect_home, _in_memory_keyring, replacement):
    actor = service(collect_home)
    old = actor.start()
    new_token = synthetic_account_token(marker='newer')
    def delayed(grant, payload):
        if replacement == 'token':
            actor.set_token(new_token)
        elif replacement == 'clear':
            actor.clear()
        else:
            actor.start()
        return SimpleNamespace(access_token=TOKEN, refresh_token='synthetic-refresh')
    actor.provider.get_token = delayed
    with pytest.raises(AuthAttemptError):
        actor.poll(old['attempt_id'], old['device_code'])
    assert credentials.get_user_secret_fresh('bearer-token') == (new_token if replacement == 'token' else None)


def test_manual_token_and_clear_discard_owned_refresh(collect_home, _in_memory_keyring):
    actor = service(collect_home)
    start = actor.start()
    actor.poll(start['attempt_id'], start['device_code'])
    actor.set_token(TOKEN)
    assert credentials.get_user_secret_fresh('oauth-credentials') is None
    assert actor.journal.committed()[1] == 'token'
    actor.clear()
    assert credentials.get_user_secret_fresh('bearer-token') is None
    assert actor.journal.committed()[1] == 'clear'


@pytest.mark.parametrize('error,status,slow', [('authorization_pending',403,False), ('slow_down',403,True), ('rate_limited',429,True)])
def test_one_pending_poll_and_server_spacing(collect_home, _in_memory_keyring, error,status,slow):
    actor = service(collect_home)
    start = actor.start()
    calls = []
    def pending(*args):
        calls.append(args)
        raise HTTPError('https://example.com', status, 'synthetic', {},
                        io.BytesIO(json.dumps({'error': error}).encode()))
    actor.provider.get_token = pending
    result = actor.poll(start['attempt_id'], start['device_code'])
    assert result == {'ok': False, 'pending': True, 'protocol': 2, 'retry_after': 10 if slow else 5}
    assert actor.poll(start['attempt_id'], start['device_code']) == result
    assert len(calls) == 1
    assert credentials.get_user_secret_fresh('bearer-token') is None


@pytest.mark.parametrize('result', [None, SimpleNamespace(access_token='bad', refresh_token='r'),
                                  SimpleNamespace(access_token=TOKEN, refresh_token=None)])
def test_malformed_credentials_never_commit(collect_home, _in_memory_keyring, result):
    actor = service(collect_home)
    actor.provider.get_token = lambda *args: result
    start = actor.start()
    with pytest.raises(AuthAttemptError):
        actor.poll(start['attempt_id'], start['device_code'])
    assert credentials.get_user_secret_fresh('bearer-token') is None


def test_validation_cannot_override_signout(collect_home, _in_memory_keyring):
    actor = service(collect_home)
    start = actor.start()
    actor.validate = lambda token: actor.clear()
    with pytest.raises(AuthAttemptError):
        actor.poll(start['attempt_id'], start['device_code'])
    assert credentials.get_user_secret_fresh('bearer-token') is None


def test_provider_error_does_not_escape_codes(collect_home, _in_memory_keyring):
    actor = service(collect_home)
    def broken():
        raise RuntimeError('synthetic-secret-device-code')
    actor.provider.get_device_code = broken
    with pytest.raises(AuthAttemptError) as exc:
        actor.start()
    assert 'synthetic-secret' not in str(exc.value)


def test_ambiguous_commit_leaves_workers_gated(collect_home, _in_memory_keyring, monkeypatch):
    from fulcra_collect import config
    actor = service(collect_home)
    start = actor.start()
    original = credentials.set_user_secret
    def partial(key, value):
        if key == 'bearer-token':
            raise RuntimeError('synthetic keychain failure')
        original(key, value)
    monkeypatch.setattr(credentials, 'set_user_secret', partial)
    with pytest.raises(AuthAttemptError):
        actor.poll(start['attempt_id'], start['device_code'])
    assert config.load().account_transition
    with pytest.raises(AuthAttemptError):
        actor.poll(start['attempt_id'], start['device_code'])


def test_owned_refresh_rotates_only_collect_store(collect_home, _in_memory_keyring, monkeypatch):
    from fulcra_collect import owned_auth
    actor = service(collect_home)
    start = actor.start()
    actor.poll(start['attempt_id'], start['device_code'])
    renewed = synthetic_account_token(marker='renewed')
    calls = []
    def get_token(grant, payload):
        calls.append((grant, payload))
        return SimpleNamespace(access_token=renewed, refresh_token='rotated-refresh')
    monkeypatch.setattr(owned_auth, 'provider', lambda: SimpleNamespace(get_token=get_token, scope='offline_access'))
    monkeypatch.setattr(owned_auth, 'validate_token', lambda token: None)
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: pytest.fail('owned refresh cannot call CLI'))
    assert credentials.refresh_fulcra_access_token(expected_token=TOKEN) == renewed
    assert len(calls) == 1 and calls[0][0] == 'refresh_token'
    assert credentials.refresh_fulcra_access_token(expected_token=TOKEN) == renewed
    assert len(calls) == 1
    assert json.loads(credentials.get_user_secret_fresh('oauth-credentials'))['refresh_token'] == 'rotated-refresh'


@pytest.mark.parametrize('replacement', ['token', 'clear'])
def test_late_owned_refresh_cannot_replace_interactive_commit(
        collect_home, _in_memory_keyring, monkeypatch, replacement):
    from fulcra_collect import owned_auth
    actor = service(collect_home)
    start = actor.start()
    actor.poll(start['attempt_id'], start['device_code'])
    def delayed(*args):
        actor.set_token(TOKEN) if replacement == 'token' else actor.clear()
        return SimpleNamespace(access_token=synthetic_account_token(marker='late'), refresh_token='rotated')
    monkeypatch.setattr(owned_auth, 'provider', lambda: SimpleNamespace(get_token=delayed, scope='offline_access'))
    monkeypatch.setattr(owned_auth, 'validate_token', lambda token: None)
    assert credentials.refresh_fulcra_access_token(expected_token=TOKEN) is None
    assert credentials.get_user_secret_fresh('bearer-token') == (TOKEN if replacement == 'token' else None)
    assert credentials.get_user_secret_fresh('oauth-credentials') is None
    assert credentials.is_refresh_failed() is False


def test_manual_token_never_renews_via_cli_account(collect_home, _in_memory_keyring, monkeypatch):
    actor = service(collect_home)
    actor.set_token(TOKEN)
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: pytest.fail('manual token cannot adopt CLI credentials'))
    assert credentials.refresh_fulcra_access_token(expected_token=TOKEN) is None


def test_late_legacy_cli_refresh_cannot_replace_same_token_manual_signin(
        collect_home, _in_memory_keyring, monkeypatch):
    import subprocess
    actor = service(collect_home)
    credentials.set_user_secret('bearer-token', TOKEN)
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: '/synthetic/fulcra')
    def late(*args, **kwargs):
        actor.set_token(TOKEN)
        return subprocess.CompletedProcess(args, 0, stdout=synthetic_account_token(marker='late-cli'))
    monkeypatch.setattr(credentials.subprocess, 'run', late)
    assert credentials.refresh_fulcra_access_token(expected_token=TOKEN) is None
    assert credentials.get_user_secret_fresh('bearer-token') == TOKEN


def test_ambiguous_owned_refresh_grant_is_not_consumed_twice(collect_home, _in_memory_keyring, monkeypatch):
    from fulcra_collect import owned_auth
    actor = service(collect_home)
    start = actor.start()
    actor.poll(start['attempt_id'], start['device_code'])
    calls = []
    def lost_response(*args):
        calls.append(args)
        raise TimeoutError('synthetic response lost after grant consumption')
    monkeypatch.setattr(owned_auth, 'provider', lambda: SimpleNamespace(get_token=lost_response, scope='offline_access'))
    monkeypatch.setattr(owned_auth, 'validate_token', lambda token: None)
    assert credentials.refresh_fulcra_access_token(expected_token=TOKEN) is None
    assert credentials.refresh_fulcra_access_token(expected_token=TOKEN) is None
    assert len(calls) == 1


def test_auth_revision_commit_failure_keeps_account_gate_closed(
        collect_home, _in_memory_keyring, monkeypatch):
    from contextlib import contextmanager
    from fulcra_collect import config
    actor = service(collect_home)
    start = actor.start()
    locked = actor.journal._locked
    class FailingCommit:
        def __init__(self, db):
            self.db = db
        def __getattr__(self, key):
            return getattr(self.db, key)
        def execute(self, sql, *args):
            if 'committed_attempt=?' in sql:
                raise OSError('synthetic auth revision commit failure')
            return self.db.execute(sql, *args)
    @contextmanager
    def failed_finalization():
        with locked() as db:
            yield FailingCommit(db)
    monkeypatch.setattr(actor.journal, '_locked', failed_finalization)
    with pytest.raises(AuthAttemptError):
        actor.poll(start['attempt_id'], start['device_code'])
    assert credentials.get_user_secret_fresh('bearer-token') == TOKEN
    assert config.load().account_transition is True
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: pytest.fail('partial auth must not enable CLI renewal'))
    assert credentials.refresh_fulcra_access_token(expected_token=TOKEN) is None


def test_account_proof_refuses_encoded_body_before_reading(collect_home, monkeypatch):
    import gzip
    import httpx
    from fulcra_collect.owned_auth import validate_token
    reads = []
    class Encoded(httpx.SyncByteStream):
        def __iter__(self):
            reads.append('read')
            yield gzip.compress(b' ' * (8 * 1024 * 1024))
    real_client = httpx.Client
    def handler(request):
        assert request.headers['accept-encoding'] == 'identity'
        return httpx.Response(200, headers={'Content-Encoding':'gzip'}, stream=Encoded())
    monkeypatch.setattr(httpx,'Client',lambda **kwargs:real_client(transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(AuthAttemptError):
        validate_token(TOKEN)
    assert reads == []


def test_owned_refresh_inside_account_scope_never_takes_interactive_auth_lease(
        collect_home, _in_memory_keyring, monkeypatch):
    import concurrent.futures
    import threading
    from fulcra_collect import config, owned_auth
    from fulcra_collect.config_leases import account_refresh_scope
    actor = service(collect_home)
    start = actor.start()
    actor.poll(start['attempt_id'], start['device_code'])
    renewed = synthetic_account_token(marker='nested-renewed')
    monkeypatch.setattr(owned_auth,'provider',lambda:SimpleNamespace(scope='offline_access',get_token=lambda *args:
        SimpleNamespace(access_token=renewed,refresh_token='rotated-refresh')))
    monkeypatch.setattr(owned_auth,'validate_token',lambda token:None)
    holding_auth = threading.Event()
    def interactive_waiting_for_account():
        with actor.journal._lease(actor.journal.path.with_suffix('.lock')):
            holding_auth.set()
            with config.fulcra_account_transition():
                pass
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        with account_refresh_scope():
            future = pool.submit(interactive_waiting_for_account)
            assert holding_auth.wait(2)
            assert credentials.refresh_fulcra_access_token(expected_token=TOKEN) == renewed
        future.result(timeout=2)


def test_old_account_request_cannot_mark_new_account_expired(collect_home, _in_memory_keyring):
    actor = service(collect_home)
    actor.set_token(TOKEN)
    old_request = synthetic_account_token(account='bbbbbbbb-cccc-4ddd-8eee-ffffffffffff')
    assert credentials.refresh_fulcra_access_token(expected_token=old_request) is None
    assert credentials.is_refresh_failed() is False


def test_old_same_account_request_reuses_new_manual_token_without_reconnect_flag(
        collect_home, _in_memory_keyring):
    actor = service(collect_home)
    current = synthetic_account_token(marker='new-manual')
    actor.set_token(current)
    old_request = synthetic_account_token(exp=1, marker='old-request')
    assert credentials.refresh_fulcra_access_token(expected_token=old_request) == current
    assert credentials.is_refresh_failed() is False
