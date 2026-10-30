"""Automatic credential publication must not enter an interactive backend."""
import io
import json
import subprocess
from types import SimpleNamespace

import pytest

from fulcra_collect import credentials, macos_keychain, owned_auth
from fulcra_collect.auth_attempts import AuthAttempts
from collect_test_helpers import synthetic_account_token

OLD = synthetic_account_token(marker='quiet-before')
NEW = synthetic_account_token(marker='quiet-renewed')


@pytest.fixture
def renewal(collect_home, _in_memory_keyring, monkeypatch):
    provider = SimpleNamespace(
        get_device_code=lambda: ('device', 'https://fulcra.us.auth0.com/activate?user_code=TEST', 'TEST', 900, 5),
        get_token=lambda *args: SimpleNamespace(access_token=OLD, refresh_token='synthetic-refresh'))
    actor = owned_auth.CollectAuth(journal=AuthAttempts(collect_home / 'auth-attempts.sqlite3'),
                                 provider=provider, validate=lambda token: None)
    writes, grants = [], []
    def quiet(key, value):
        writes.append(key)
        _in_memory_keyring[('fulcra-collect:user', key)] = value
    monkeypatch.setattr(credentials, '_mutate_user_secret_quiet', quiet, raising=False)
    def grant(*args):
        grants.append(args)
        return SimpleNamespace(access_token=NEW, refresh_token='synthetic-rotated')
    monkeypatch.setattr(owned_auth, 'provider', lambda: SimpleNamespace(get_token=grant, scope='offline_access'))
    monkeypatch.setattr(owned_auth, 'validate_token', lambda token: None)
    return actor, writes, grants, quiet


@pytest.mark.parametrize('mode', ['owned', 'legacy'])
def test_actual_refresh_never_publishes_with_interactive_keyring(renewal, monkeypatch, mode):
    actor, writes, grants, quiet = renewal
    if mode == 'owned':
        start = actor.start()
        actor.poll(start['attempt_id'], start['device_code'])
        monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: pytest.fail('owned CLI fallback'))
    else:
        credentials.set_user_secret('bearer-token', OLD)
        monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: '/synthetic/fulcra')
        monkeypatch.setattr(credentials.subprocess, 'run', lambda *a, **kw:
            subprocess.CompletedProcess(a, 0, stdout=NEW))
    monkeypatch.setattr(credentials.keyring, 'set_password', lambda *args: pytest.fail('interactive writer'))
    assert credentials.refresh_fulcra_access_token(expected_token=OLD) == NEW
    assert writes == (['oauth-credentials', 'bearer-token'] if mode == 'owned' else ['bearer-token'])
    assert credentials.get_user_secret_fresh('bearer-token') == NEW
    assert len(grants) == (1 if mode == 'owned' else 0)


@pytest.mark.parametrize('failure', ['denied', 'timeout', 'ambiguous_second'])
def test_owned_quiet_publication_failure_never_replays_consumed_grant(renewal, monkeypatch, failure):
    actor, writes, grants, quiet = renewal
    start = actor.start()
    actor.poll(start['attempt_id'], start['device_code'])
    def fail(key, value):
        if failure == 'ambiguous_second' and key == 'oauth-credentials':
            return quiet(key, value)
        if failure == 'timeout':
            raise TimeoutError('Synthetic write deadline')
        raise credentials.KeychainAccessRequired('Synthetic unavailable write')
    monkeypatch.setattr(credentials, '_mutate_user_secret_quiet', fail)
    monkeypatch.setattr(credentials.keyring, 'set_password', lambda *args: pytest.fail('interactive fallback'))
    monkeypatch.setattr(credentials, '_find_fulcra_cli', lambda: pytest.fail('CLI fallback'))
    assert credentials.refresh_fulcra_access_token(expected_token=OLD) is None
    assert credentials.refresh_fulcra_access_token(expected_token=OLD) is None
    assert len(grants) == 1
    assert credentials.get_user_secret_fresh('bearer-token') == OLD
    stored = json.loads(credentials.get_user_secret_fresh('oauth-credentials'))
    assert stored['access_token'] == (NEW if failure == 'ambiguous_second' else OLD)
    assert credentials.is_refresh_failed()


@pytest.mark.parametrize('key', ['bearer-token', 'oauth-credentials'])
def test_user_quiet_child_packet_disables_interaction_before_mutation(monkeypatch, key):
    order = []
    monkeypatch.setattr(macos_keychain, '_api', lambda: SimpleNamespace(disable_interaction=lambda: order.append('disable')))
    monkeypatch.setattr(macos_keychain, '_mutate_password', lambda request: order.append((request['service'], request['account'])))
    monkeypatch.setattr(macos_keychain.sys, 'stdin', io.StringIO(json.dumps({
        'op': 'set', 'service': 'fulcra-collect:user', 'account': key, 'value': 'SYNTHETIC'})))
    out = io.StringIO()
    monkeypatch.setattr(macos_keychain.sys, 'stdout', out)
    assert macos_keychain.child_main() == 0
    assert order == ['disable', ('fulcra-collect:user', key)]
    assert json.loads(out.getvalue()) == {'status': 'ok'}


@pytest.mark.parametrize('packet', [
    {'op':'set','service':'fulcra-collect:user','account':'other','value':'x'},
    {'op':'delete','service':'fulcra-collect:user','account':'bearer-token'},
    {'op':'set','service':'fulcra-collect:user','account':'bearer-token','value':'x'*65537},
    {'op':'set','service':'fulcra-collect:user-other','account':'bearer-token','value':'x'},
])
def test_user_mutation_scope_does_not_expand_to_other_items_or_deletion(packet):
    assert not macos_keychain._valid_mutation(packet)


def test_quiet_user_backend_refuses_unsupported_platform_without_interactive_fallback(monkeypatch):
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: False)
    monkeypatch.setattr(credentials.keyring, 'set_password', lambda *args: pytest.fail('interactive fallback'))
    with pytest.raises(credentials.KeychainAccessRequired):
        credentials.set_user_secret_quiet('bearer-token', 'SYNTHETIC')


@pytest.mark.parametrize('failure', ['timeout', 'denied', 'ambiguous'])
def test_quiet_user_native_request_is_bounded_and_acknowledgement_failures_do_not_retry(monkeypatch, failure):
    actions, packets = [], []
    proc = SimpleNamespace(pid=12345, returncode=0, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
                           wait=lambda **kw: actions.append('wait'))
    monkeypatch.setattr(macos_keychain.subprocess, 'Popen', lambda *a, **kw: actions.append('start') or proc)
    monkeypatch.setattr(macos_keychain.os, 'killpg', lambda pid, sig: actions.append(('kill', pid)))
    def exchange(_proc, packet, deadline):
        packets.append(json.loads(packet))
        assert deadline > 0
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('synthetic', 2, output=b'SYNTHETIC_PRIVATE')
        return json.dumps({'status':'access_required'} if failure == 'denied' else {'status':'ok','value':'UNEXPECTED'}).encode()
    monkeypatch.setattr(macos_keychain, '_communicate', exchange)
    monkeypatch.setattr(credentials.keyring, 'set_password', lambda *args: pytest.fail('interactive fallback'))
    with pytest.raises(macos_keychain.KeyringError) as exc:
        macos_keychain.mutate_user_password('bearer-token', 'SYNTHETIC_PRIVATE')
    assert packets == [{'op':'set','service':'fulcra-collect:user','account':'bearer-token','value':'SYNTHETIC_PRIVATE'}]
    assert actions == ['start', ('kill',12345), 'wait']
    assert 'SYNTHETIC_PRIVATE' not in str(exc.value)
