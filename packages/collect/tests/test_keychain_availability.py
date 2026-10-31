"""Fresh, quiet availability and explicit scope recovery; no real Keychain."""
import io
import json

import pytest


@pytest.fixture
def local_auth(collect_home, monkeypatch):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from fulcra_collect import credentials, macos_keychain
    from fulcra_collect.daemon import Daemon, Config
    from fulcra_collect.registry import RegistryResult
    from fulcra_collect.web import build_app, _ensure_token
    backend = SimpleNamespace(denied=False, allow_once=False, reads=[],
                              values={('fulcra-collect:user', 'bearer-token'): 'PRIVATE'})
    def read(service, key, *, allow_ui=False):
        backend.reads.append((service, key, allow_ui))
        if backend.denied and not (allow_ui and backend.allow_once):
            raise credentials.KeychainAccessRequired('PRIVATE')
        return backend.values.get((service, key))
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: True)
    monkeypatch.setattr(macos_keychain, 'get_password', read)
    daemon = Daemon(registry=RegistryResult(), config=Config())
    client = TestClient(build_app(daemon))
    client.headers['Authorization'] = 'Bearer ' + _ensure_token()
    return backend, client


def test_warm_cache_cannot_claim_current_signin_access(local_auth):
    from fulcra_collect import credentials
    backend, client = local_auth
    assert credentials.get_user_secret('bearer-token') == 'PRIVATE'
    backend.denied = True
    reply = client.get('/api/fulcra/auth/status').json()
    assert reply['authenticated'] is None
    assert reply['availability'] == 'access_required'
    assert reply['saved_presence'] is True
    assert reply['provenance'] == 'fresh_quiet_keychain'
    assert reply['server_authenticated'] is None
    assert all(not allow for _, _, allow in backend.reads)
    assert 'PRIVATE' not in json.dumps(reply)


def test_explicit_authorization_cannot_ignore_false(local_auth, monkeypatch):
    from fulcra_collect import credentials
    _, client = local_auth
    monkeypatch.setattr(credentials, 'authorize_user_secret', lambda key: False)
    reply = client.post('/api/fulcra/auth/keychain_access').json()
    assert reply['ok'] is False
    assert reply['items']['bearer-token']['status'] == 'missing'


def test_allow_once_stays_unverified_after_quiet_confirmation(local_auth):
    backend, client = local_auth
    backend.denied = backend.allow_once = True
    reply = client.post('/api/fulcra/auth/keychain_access').json()
    assert reply['ok'] is False
    assert reply['items']['bearer-token']['status'] == 'unverified'
    assert backend.reads[:2] == [('fulcra-collect:user', 'bearer-token', True),
                                ('fulcra-collect:user', 'bearer-token', False)]
    assert 'PRIVATE' not in json.dumps(reply)


def test_manual_bearer_does_not_require_optional_refresh(local_auth):
    _, client = local_auth
    reply = client.post('/api/fulcra/auth/keychain_access').json()
    assert reply['ok'] is True
    assert reply['items']['oauth-credentials']['status'] == 'missing'
    status = client.get('/api/fulcra/auth/status').json()
    assert status['authenticated'] is True
    assert status['availability'] == 'present'
    assert status['refresh_available'] is None


@pytest.mark.parametrize('stage', ['credential_hydration', 'collect_account'])
def test_actual_worker_distinguishes_plugin_key_from_collect_bearer(collect_home, monkeypatch, stage):
    from fulcra_collect import credentials, macos_keychain, worker
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.v1_session import CollectRecordSession
    called = []
    def read(service, key, *, allow_ui=False):
        assert allow_ui is False
        if (stage == 'credential_hydration' and service == 'fulcra-collect:sample') or key == 'bearer-token':
            raise credentials.KeychainAccessRequired('PRIVATE')
        return 'plugin-secret'
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: True)
    monkeypatch.setattr(macos_keychain, 'get_password', read)
    def run(ctx):
        called.append('run')
        CollectRecordSession(ctx)  # Denial occurs before any principal/provider I/O.
    plugin = Plugin(id='sample', name='Synthetic', kind='manual', collect_mode='historical', run=run,
                    required_credentials=(Credential(key='api-key', label='API', help=''),))
    out = io.StringIO()
    assert worker.run_plugin(plugin, out=out) == 'error'
    event = json.loads(out.getvalue())
    assert event.get('credential_error') == {
        'stage': stage, 'scope': 'plugin' if stage == 'credential_hydration' else 'user',
        'key': 'api-key' if stage == 'credential_hydration' else 'bearer-token',
        'availability': 'access_required'}
    assert called == ([] if stage == 'credential_hydration' else ['run'])
    assert 'PRIVATE' not in out.getvalue()


def _runner_with_result(collect_home, monkeypatch, *, change_account=False, detail=None):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from fulcra_collect import config, runner, state
    from fulcra_collect.daemon import Daemon
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult
    plugin = Plugin(id='sample', name='Synthetic', kind='manual', collect_mode='historical', run=lambda _: None,
                    required_credentials=(Credential(key='api-key', label='API', help=''),))
    cfg = config.load()
    cfg.plugin_settings['sample'] = {'selected': ['synthetic']}
    config.save(cfg)
    daemon = Daemon(registry=RegistryResult(plugins={'sample': plugin}), config=cfg)
    state.save(state.PluginState('sample', last_outcome='done'))
    event = {'type': 'result', 'outcome': 'error', 'error': 'preserved static failure', 'credential_error': detail}
    def captured(*args, **kwargs):
        if change_account:
            with config.fulcra_account_transition():
                pass
        return runner._CapturedOutput(json.dumps(event).encode(), b'', False, False)
    monkeypatch.setattr(runner.subprocess, 'Popen', lambda *args, **kw: SimpleNamespace())
    monkeypatch.setattr(runner, '_bounded_communicate', captured)
    runner.run('sample', ['synthetic-never-executed'], now=datetime(2026, 10, 1, tzinfo=timezone.utc), daemon=daemon)
    return daemon, daemon.handle_request({'cmd': 'status'})['plugins'][0]


def test_actual_runner_surfaces_current_shared_error_at_settings(collect_home, monkeypatch):
    detail = {'stage': 'collect_account', 'scope': 'user', 'key': 'bearer-token', 'availability': 'access_required'}
    _, row = _runner_with_result(collect_home, monkeypatch, detail=detail)
    assert row.get('credential_error') == {**detail, 'recovery': 'settings'}


def test_old_account_worker_result_cannot_poison_new_status(collect_home, monkeypatch):
    from fulcra_collect import state
    detail = {'stage': 'credential_hydration', 'scope': 'plugin', 'key': 'api-key', 'availability': 'access_required'}
    _, row = _runner_with_result(collect_home, monkeypatch, change_account=True, detail=detail)
    assert row['last_outcome'] == 'done'
    assert row.get('credential_error') is None
    assert state.load('sample').last_outcome == 'done'


def test_status_pending_is_quiet_singleflight_and_discards_late_result(local_auth, monkeypatch):
    import threading
    from fulcra_collect import credentials, macos_keychain
    backend, client = local_auth
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def blocked(*args, allow_ui=False):
        assert not allow_ui
        backend.reads.append(('blocked', 'bearer-token', allow_ui))
        started.set()
        try:
            assert release.wait(2)
            return 'PRIVATE'
        finally:
            finished.set()
    monkeypatch.setattr(credentials, '_KEYCHAIN_READ_TIMEOUT_S', .01)
    monkeypatch.setattr(macos_keychain, 'get_password', blocked)
    try:
        first = client.get('/api/fulcra/auth/status').json()
        assert started.is_set() and first['availability'] == 'pending'
        second = client.post('/api/fulcra/auth/keychain_access').json()
        assert second['items']['bearer-token']['availability'] == 'pending'
        assert len(backend.reads) == 1
    finally:
        release.set()
        assert finished.wait(1)
    def denied(*args, **kwargs):
        raise credentials.KeychainAccessRequired('PRIVATE')
    monkeypatch.setattr(macos_keychain, 'get_password', denied)
    assert client.get('/api/fulcra/auth/status').json()['availability'] == 'access_required'


def test_status_during_credential_generation_change_stays_unknown(local_auth, monkeypatch):
    from fulcra_collect import credentials, macos_keychain
    _, client = local_auth
    def changed(*args, **kwargs):
        credentials._clear_caches()
        return 'PRIVATE'
    monkeypatch.setattr(macos_keychain, 'get_password', changed)
    result = client.get('/api/fulcra/auth/status').json()
    assert result['availability'] == 'changed'
    assert result['authenticated'] is None
    assert 'PRIVATE' not in json.dumps(result)


def test_status_never_refreshes_or_checks_provider(local_auth, monkeypatch):
    from fulcra_collect import credentials, owned_auth
    _, client = local_auth
    def forbidden(*args, **kwargs):
        pytest.fail('status must stay local and quiet')
    monkeypatch.setattr(credentials, 'refresh_fulcra_access_token', forbidden)
    monkeypatch.setattr(owned_auth, 'provider', forbidden)
    result = client.get('/api/fulcra/auth/status').json()
    assert result['availability'] == 'present'
    assert result['server_authenticated'] is None


def test_account_transition_gates_status_and_explicit_authorization(local_auth):
    from fulcra_collect import config
    backend, client = local_auth
    with config.fulcra_account_transition():
        result = client.get('/api/fulcra/auth/status').json()
        assert result['authenticated'] is None
        assert client.post('/api/fulcra/auth/keychain_access').json()['ok'] is False
        assert not backend.reads


def test_authorization_rejects_write_between_interactive_and_confirmation(local_auth, monkeypatch):
    from fulcra_collect import credentials
    _, client = local_auth
    real_read = credentials._keyring_get
    def changed(*args, **kwargs):
        result = real_read(*args, **kwargs)
        if kwargs.get('allow_ui'):
            credentials._clear_caches()
        return result
    monkeypatch.setattr(credentials, '_keyring_get', changed)
    reply = client.post('/api/fulcra/auth/keychain_access').json()
    assert reply['ok'] is False
    assert reply['items']['bearer-token']['availability'] == 'changed'


def test_plugin_hydration_recovery_and_restart_are_not_shared_auth_proof(collect_home, monkeypatch):
    from fulcra_collect.daemon import Daemon
    detail = {'stage': 'credential_hydration', 'scope': 'plugin', 'key': 'api-key', 'availability': 'access_required'}
    daemon, row = _runner_with_result(collect_home, monkeypatch, detail=detail)
    assert row['credential_error'] == {**detail, 'recovery': 'configure'}
    restarted = Daemon(registry=daemon.registry, config=daemon.config)
    after = restarted.handle_request({'cmd': 'status'})['plugins'][0]
    assert after['credential_error'] is None
    assert after['credential_error_status'] == 'unknown'
    assert after['last_error'] == 'preserved static failure'


@pytest.mark.parametrize('detail', [
    {'stage': 'collect_account', 'scope': 'user', 'key': 'private-undeclared', 'availability': 'access_required'},
    {'stage': 'credential_hydration', 'scope': 'plugin', 'key': 'api-key', 'availability': ['PRIVATE']},
    {'stage': 'collect_account', 'scope': 'user', 'key': 'bearer-token', 'availability': 'access_required', 'secret': 'PRIVATE'},
])
def test_worker_scope_metadata_requires_closed_declared_fields(collect_home, monkeypatch, detail):
    _, row = _runner_with_result(collect_home, monkeypatch, detail=detail)
    assert row.get('credential_error') is None
    assert 'PRIVATE' not in json.dumps(row)


@pytest.mark.parametrize('change_at', ['during-optional-read', 'between-items'])
def test_shared_authorization_rejects_stale_bearer_after_other_item_change(local_auth, monkeypatch, change_at):
    """An individually stable first read cannot authorize a stale whole response."""
    from fulcra_collect import credentials, macos_keychain
    backend, client = local_auth
    original_read = macos_keychain.get_password
    original_authorize = credentials.authorize_user_secret
    changed = []

    def revoke_synthetic_bearer():
        backend.values.pop(('fulcra-collect:user', 'bearer-token'))
        credentials._clear_caches()  # Same generation fence used by credential writes.
        changed.append(True)

    if change_at == 'during-optional-read':
        def read(service, key, *, allow_ui=False):
            if key == 'oauth-credentials' and allow_ui:
                revoke_synthetic_bearer()
            return original_read(service, key, allow_ui=allow_ui)
        monkeypatch.setattr(macos_keychain, 'get_password', read)
    else:
        def authorize(key):
            present = original_authorize(key)  # Actual interactive + quiet pair.
            if key == 'bearer-token':
                revoke_synthetic_bearer()  # After its pair returned, before next item.
            return present
        monkeypatch.setattr(credentials, 'authorize_user_secret', authorize)

    reply = client.post('/api/fulcra/auth/keychain_access').json()
    assert changed == [True]
    assert backend.reads[:2] == [('fulcra-collect:user', 'bearer-token', True),
                                ('fulcra-collect:user', 'bearer-token', False)]
    assert reply['ok'] is False
    assert reply['items']['bearer-token'] == {'status': 'unverified', 'availability': 'changed'}
    assert 'PRIVATE' not in json.dumps(reply)
    if change_at == 'during-optional-read':
        # A changed optional pair must not trigger further UI or quiet retries.
        assert backend.reads[2:] == [('fulcra-collect:user', 'oauth-credentials', True)]


def test_initial_credential_write_gate_does_not_claim_fresh_read(local_auth):
    from fulcra_collect import credentials
    backend, client = local_auth
    with credentials._credential_write():
        reply = client.get('/api/fulcra/auth/status').json()
    assert reply['authenticated'] is None
    assert reply['availability'] == 'changed'
    assert reply['provenance'] == 'not_checked'
    assert reply['checked_at'] is None
    assert backend.reads == []
    assert 'PRIVATE' not in json.dumps(reply)
