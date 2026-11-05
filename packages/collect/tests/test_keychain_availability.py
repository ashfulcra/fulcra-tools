"""Fresh, quiet availability and explicit scope recovery; no real Keychain."""
import io
import json

import pytest


@pytest.mark.parametrize('surface', ['status', 'authorize', 'worker', 'health'])
@pytest.mark.parametrize('failure,availability', [
    ('timeout', 'pending'), ('pending', 'pending'), ('changed', 'changed'),
])
def test_unverified_reads_never_request_another_grant(collect_home, monkeypatch, surface, failure, availability):
    from fastapi.testclient import TestClient
    from fulcra_collect import credentials, macos_keychain, worker
    from fulcra_collect.daemon import Daemon, Config
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.registry import RegistryResult
    from fulcra_collect.web import build_app, _ensure_token

    errors = {'timeout': macos_keychain.KeychainReadTimeout,
              'pending': credentials.KeychainReadPending,
              'changed': credentials.CredentialStateChanged}
    def read(*args, **kwargs):
        raise errors[failure]('synthetic-private')
    monkeypatch.setattr(credentials, '_keyring_get', read)
    def source(_):
        pytest.fail('source I/O before credential availability')
    plugin = Plugin(id='sample', name='Synthetic', kind='manual', collect_mode='historical',
                    run=source, health_check=source,
                    required_credentials=(Credential(key='source-key', label='Source', help=''),))
    daemon = Daemon(registry=RegistryResult(plugins={'sample': plugin}), config=Config())
    if surface == 'status':
        reply = daemon.handle_request({'cmd': 'credential_status', 'plugin': 'sample'})
        assert reply['credentials']['source-key'] == availability
    elif surface == 'authorize':
        reply = daemon.handle_request({'cmd': 'authorize_credential', 'plugin': 'sample', 'key': 'source-key'})
    elif surface == 'worker':
        out = io.StringIO()
        assert worker.run_plugin(plugin, out=out) == 'error'
        reply = json.loads(out.getvalue())
    else:
        client = TestClient(build_app(daemon))
        client.headers['Authorization'] = 'Bearer ' + _ensure_token()
        reply = client.post('/api/plugin/sample/health_check').json()
    assert reply['code'] == 'keychain_' + availability
    assert 'synthetic-private' not in json.dumps(reply)
    assert 'Allow Keychain access' not in json.dumps(reply)


@pytest.mark.parametrize('field', ['created_at', 'expires_in'])
def test_trakt_expiry_access_is_visible_and_recoverable(collect_home, monkeypatch, field):
    """A field consumed by the source cannot be invisible to Configure."""
    from fulcra_collect import credentials
    from fulcra_collect.daemon import Daemon, Config
    from fulcra_collect.registry import RegistryResult
    from fulcra_media.plugins.trakt import PLUGIN

    allowed = False
    reads = []

    def read(service, key, *, allow_ui=False):
        nonlocal allowed
        assert service == 'fulcra-collect:trakt'
        reads.append((key, allow_ui))
        if key == field:
            if allow_ui:
                allowed = True
            if not allowed:
                raise credentials.KeychainAccessRequired('synthetic denied item')
        return 'synthetic-value'

    monkeypatch.setattr(credentials, '_keyring_get', read)
    # Opt in explicitly to the synthetic backend, including when the root
    # Trakt guard was installed before this module was imported.
    monkeypatch.setattr(credentials, 'authorize_secret',
                        lambda plugin, key: credentials._authorize_item(credentials._service(plugin), key))
    daemon = Daemon(registry=RegistryResult(plugins={'trakt': PLUGIN}), config=Config())
    status = daemon.handle_request({'cmd': 'credential_status', 'plugin': 'trakt'})
    assert status['credentials'].get(field) == 'access_required'
    with pytest.raises(credentials.CredentialReadError) as error:
        credentials.get_plugin_credentials(PLUGIN)
    assert error.value.detail['key'] == field
    reply = daemon.handle_request({'cmd': 'authorize_credential', 'plugin': 'trakt', 'key': field})
    assert reply == {'ok': True, 'status': 'set'}
    assert (field, True) in reads
    assert reads[-1] == (field, False)
    assert credentials.get_plugin_credentials(PLUGIN)[field] == 'synthetic-value'
    assert 'synthetic-value' not in json.dumps([status, reply])


@pytest.mark.parametrize('field', ['created_at', 'expires_in'])
def test_trakt_owner_snapshot_retains_exact_pending_field(monkeypatch, field):
    from fulcra_collect import credentials, macos_keychain
    from fulcra_collect.trakt_source import KeychainStore
    def read(plugin, key):
        assert plugin == 'trakt'
        if key == field:
            raise macos_keychain.KeychainReadTimeout('synthetic-private')
        return 'synthetic-value'
    monkeypatch.setattr(credentials, 'get_secret', read)
    with pytest.raises(credentials.CredentialReadError) as error:
        KeychainStore().snapshot()
    assert error.value.detail == {'stage': 'credential_hydration', 'scope': 'plugin',
                                  'key': field, 'availability': 'pending'}


def test_fulcra_access_timeout_stays_pending_without_requesting_refresh_item(local_auth, monkeypatch):
    from fulcra_collect import macos_keychain
    _, client = local_auth
    reads = []
    def read(service, key, *, allow_ui=False):
        reads.append(key)
        raise macos_keychain.KeychainReadTimeout('synthetic-private')
    monkeypatch.setattr(macos_keychain, 'get_password', read)
    reply = client.post('/api/fulcra/auth/keychain_access').json()
    assert reply['code'] == 'keychain_pending'
    assert reply['items']['bearer-token']['availability'] == 'pending'
    assert reply['items']['oauth-credentials']['availability'] == 'not_checked'
    assert reads == ['bearer-token']
    assert 'synthetic-private' not in json.dumps(reply)


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
