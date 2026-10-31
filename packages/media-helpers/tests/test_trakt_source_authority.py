"""Synthetic Trakt owner acceptance; guards precede source/runtime imports."""
from contextlib import contextmanager
import json
import os
from types import SimpleNamespace

import pytest

from test_poll_cursors import configured as configured  # noqa: F401


@pytest.fixture(autouse=True)
def contained(monkeypatch, tmp_path):
    import socket
    import subprocess
    def forbidden(*args, **kwargs):
        pytest.fail('uncontained auth/network/child')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(os, 'system', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    from fulcra_collect import credentials
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: False)
    store = {}
    monkeypatch.setattr(credentials.keyring, 'get_password', lambda s, k: store.get((s, k)))
    monkeypatch.setattr(credentials.keyring, 'set_password', lambda s, k, v: store.__setitem__((s, k), v))
    monkeypatch.setattr(credentials.keyring, 'delete_password', lambda s, k: store.pop((s, k), None))
    monkeypatch.setattr(credentials, 'authorize_secret', lambda p, k: (p, k) and bool(store.get(('fulcra-collect:' + p, k))))
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile'))
    return SimpleNamespace(store=store, root=tmp_path / 'owner')


@pytest.fixture
def owner(contained, monkeypatch):
    from fulcra_collect import trakt_source as source
    monkeypatch.setattr(source, '_os_user_root', lambda: contained.root)
    from fulcra_collect import credentials
    original_init = source.KeychainStore.__init__
    def synthetic_init(self, *, _quiet_write=None):
        def quiet(k, v):
            if v is None:
                credentials.delete_secret('trakt', k)
            else:
                credentials.set_secret('trakt', k, v)
        original_init(self, _quiet_write=_quiet_write or quiet)
    monkeypatch.setattr(source.KeychainStore, '__init__', synthetic_init)
    adapter = source.KeychainStore()
    for key, value in {'client_id': 'synthetic-client', 'access_token': 'synthetic-access',
                       'refresh_token': 'synthetic-refresh'}.items():
        adapter.set(key, value)
    return source.Owner(adapter)


def test_owner_claim_survives_resolution_and_other_store(owner, tmp_path):
    from fulcra_collect import trakt_source as source
    binding = owner.capture(lambda **_: None, lambda: None)
    calls = []
    binding.refresh(lambda old: calls.append(old) or {'access_token': 'next-access',
        'refresh_token': 'next-refresh', 'created_at': 1, 'expires_in': 100})
    path = tmp_path / 'trakt.json'
    source._atomic_json(path, {'client_id': 'different-client', 'client_secret': 'secret',
        'access_token': 'old-copy', 'refresh_token': 'synthetic-refresh',
        'created_at': 1, 'expires_in': 2})
    copied = source.Owner(source.FileStore(path)).capture(lambda **_: None, lambda: None)
    with pytest.raises(source.SourceHold):
        copied.refresh(lambda _: pytest.fail('consumed grant replay'))
    assert len(calls) == 1
    assert 'synthetic-refresh' not in (owner.root / 'journal.json').read_text()
    assert 'next-access' not in (owner.root / 'journal.json').read_text()


def test_receipt_created_ack_lost_recovers_without_grant(owner, monkeypatch):
    binding = owner.capture(lambda **_: None, lambda: None)
    publish = owner._publish
    monkeypatch.setattr(owner, '_publish', lambda *a: (_ for _ in ()).throw(OSError('crash')))
    with pytest.raises(OSError):
        binding.refresh(lambda _: {'access_token': 'new-access', 'refresh_token': 'new-refresh'})
    monkeypatch.setattr(owner, '_publish', publish)
    recovered = owner.capture(lambda **_: None, lambda: None)
    assert recovered.values['access_token'] == 'new-access'
    recovered.check_before_source()


def test_manual_pair_incomplete_same_bytes_revoke_disconnect(owner):
    from fulcra_collect import trakt_source as source
    old = owner.capture(lambda **_: None, lambda: None)
    assert owner.edit('access_token', 'synthetic-access')['ready'] is False
    with pytest.raises(source.SourceHold):
        old.check_before_source()
    with pytest.raises(source.SourceHold):
        owner.capture(lambda **_: None, lambda: None)
    assert owner.edit('refresh_token', 'synthetic-refresh')['ready'] is True
    active = owner.capture(lambda **_: None, lambda: None)
    owner.edit('access_token', None)
    owner.edit('client_secret', 'legacy')
    with pytest.raises(source.SourceHold):
        active.check_before_source()
    with pytest.raises(source.SourceHold):
        owner.capture(lambda **_: None, lambda: None)


def test_late_refresh_quarantined_after_edit(owner):
    from fulcra_collect import trakt_source as source
    binding = owner.capture(lambda **_: None, lambda: None)
    def grant(_):
        owner.edit('access_token', 'manual-access')
        return {'access_token': 'late-access', 'refresh_token': 'late-refresh'}
    with pytest.raises(source.SourceHold):
        binding.refresh(grant)
    assert owner.store.snapshot()['access_token'] == 'manual-access'
    journal = source._read_json(owner.root / 'journal.json')
    attempt = next(iter(journal['attempts'].values()))
    assert attempt['status'] == 'quarantined'
    assert owner.store.receipt(attempt['id'])['values']['access_token'] == 'late-access'


def test_worker_unknown_optional_is_not_absence(contained, monkeypatch):
    from fulcra_collect import config, credentials, worker
    from fulcra_collect.plugin import Plugin, Credential
    cfg = config.load()
    monkeypatch.setattr(worker.config, 'load', lambda: cfg)
    def get(_, key):
        if key == 'client_secret':
            raise TimeoutError('synthetic unknown')
        return 'synthetic'
    monkeypatch.setattr(credentials, 'get_secret', get)
    plugin = Plugin(id='trakt', name='synthetic', kind='manual', collect_mode='historical', run=lambda _: pytest.fail('UNKNOWN entered worker'),
                    required_credentials=(Credential('client_id', 'id', ''), Credential('client_secret', 'secret', '', required=False)))
    import io
    out = io.StringIO()
    assert worker.run_plugin(plugin, out=out) == 'error'
    assert json.loads(out.getvalue())['code'] == 'keychain_access_required'


def test_wrapper_guard_lasts_through_cursor_after_original_recovery(contained, monkeypatch):
    from fulcra_media import collect_v1
    from contextlib import nullcontext
    trace = []
    session = SimpleNamespace(account_id='account')
    def authority(**kwargs):
        trace.append('authority')
    session.guard = authority
    session._source_local_guard = lambda: None
    def commit(fn):
        session.guard()
        fn()
    session.commit = commit
    monkeypatch.setattr(collect_v1, 'CollectRecordSession', lambda _: nullcontext(session))
    class Writer:
        def __init__(self, *_):
            pass
        def resume(self):
            trace.append('original')
            session.guard()
    monkeypatch.setattr(collect_v1, 'MediaRecordWriter', Writer)
    ctx = SimpleNamespace(plugin_id='trakt', state=SimpleNamespace(watermark=None),
        config={}, config_epoch='epoch', kv_get=lambda *a: None,
        kv_set=lambda *a: pytest.fail('stale cursor committed'))
    def local():
        trace.append('source')
        raise RuntimeError('revoked')
    @collect_v1.media_run
    def plugin(ctx):
        assert trace == ['original', 'authority']
        ctx._register_current_work_guard(local)
        ctx.state.watermark = '2026-01-01T00:00:00Z'
    with pytest.raises(RuntimeError, match='revoked'):
        plugin(ctx)
    assert trace == ['original', 'authority', 'authority', 'source']
    assert session.guard is authority
    assert not hasattr(ctx, '_register_current_work_guard')


def test_oauth_latest_attempt_captured_fields(owner):
    from fulcra_collect import trakt_source as source
    first = owner.begin_authorization('state1', 'verifier1', 'http://127.0.0.1/cb')
    second = owner.begin_authorization('state2', 'verifier2', 'http://127.0.0.1/cb')
    with pytest.raises(source.SourceHold):
        first.exchange('code', lambda _: pytest.fail('stale code exchanged'))
    calls = []
    second.exchange('code', lambda payload: calls.append(payload) or {
        'access_token': 'oauth-access', 'refresh_token': 'oauth-refresh',
        'created_at': '1', 'expires_in': '2', 'redirect_uri': 'http://127.0.0.1/cb'})
    assert calls[0]['client_id'] == 'synthetic-client'
    assert calls[0]['code_verifier'] == 'verifier2'
    assert owner.capture(lambda **_: None, lambda: None).values['access_token'] == 'oauth-access'


def test_background_keychain_writes_require_quiet_backend(owner, monkeypatch):
    from fulcra_collect import credentials
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: False)
    owner.store._quiet_write = None
    binding = owner.capture(lambda **_: None, lambda: None)
    with pytest.raises(Exception):
        binding.refresh(lambda _: {'access_token': 'new', 'refresh_token': 'new-refresh'})
    assert owner.store.snapshot()['access_token'] == 'synthetic-access'


def test_containment_rejects_real_default_paths_before_open(contained, monkeypatch, tmp_path):
    from fulcra_collect import config, trakt_source as source
    from pathlib import Path
    # Failure is the canary outcome; no actual path is opened.
    with pytest.raises(pytest.fail.Exception, match='default-root'):
        source.Owner(source.KeychainStore(), _root=Path('/unapproved-default-root'))
    with pytest.raises(pytest.fail.Exception, match='default-root'):
        source.FileStore(Path('/unapproved-legacy-file'))
    monkeypatch.setenv('FULCRA_COLLECT_HOME', '/unapproved-config-root')
    with pytest.raises(pytest.fail.Exception, match='default-root'):
        config.profile_path()
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'private-profile'))
    selected = source.Owner(source.FileStore(tmp_path / 'private-trakt.json'))
    assert selected.root.is_relative_to(tmp_path)
    assert config.profile_path().is_relative_to(tmp_path)


def test_oauth_receipt_recovery_after_callback_crash(owner, monkeypatch):
    attempt = owner.begin_authorization('state', 'verifier', 'http://127.0.0.1/cb')
    publish = owner._publish
    monkeypatch.setattr(owner, '_publish', lambda *a: (_ for _ in ()).throw(OSError('crash')))
    with pytest.raises(OSError):
        attempt.exchange('code', lambda _: {'access_token': 'new-a', 'refresh_token': 'new-r',
            'created_at': '1', 'expires_in': '2', 'redirect_uri': 'http://127.0.0.1/cb'})
    monkeypatch.setattr(owner, '_publish', publish)
    assert owner.capture(lambda **_: None, lambda: None).values['refresh_token'] == 'new-r'


def test_successful_refresh_ignores_unconsumed_provider_fields(owner):
    binding = owner.capture(lambda **_: None, lambda: None)
    binding.refresh(lambda _: {'access_token': 'new-a', 'refresh_token': 'new-r',
                               'token_type': 'Bearer', 'scope': 'public', 'client_id': 'untrusted'})
    assert binding.values['client_id'] == 'synthetic-client'
    assert 'token_type' not in binding.values


def test_late_pair_quarantines_even_when_old_full_authority_revoked(owner):
    from fulcra_collect import trakt_source as source
    current = [True]
    def authority():
        if not current[0]:
            raise RuntimeError('old authority revoked')
    binding = owner.capture(authority, lambda: None)
    def grant(_):
        owner.edit('access_token', None)
        current[0] = False
        return {'access_token': 'late-a', 'refresh_token': 'late-r'}
    with pytest.raises(Exception):
        binding.refresh(grant)
    journal = source._read_json(owner.root / 'journal.json')
    assert next(iter(journal['attempts'].values()))['status'] == 'quarantined'


def test_actual_control_and_cli_manual_pair_protocol(owner, monkeypatch):
    from fulcra_collect.daemon import Daemon
    from fulcra_collect.registry import RegistryResult
    from fulcra_collect import config
    from fulcra_media.plugins.trakt import PLUGIN
    from fulcra_collect.cli import cli
    from click.testing import CliRunner
    import click
    from fulcra_collect.trakt_source import SourceHold
    old = owner.capture(lambda **_: None, lambda: None)
    daemon = Daemon(registry=RegistryResult(plugins={'trakt': PLUGIN}), config=config.load())
    response = daemon.handle_request({'cmd': 'set_credential', 'plugin': 'trakt',
                                      'key': 'access_token', 'secret': 'explicit-access'})
    assert response == {'ok': True, 'ready': False, 'status': 'editing'}
    with pytest.raises(SourceHold):
        old.check_before_source()
    monkeypatch.setattr(click, 'prompt', lambda *a, **kw: 'explicit-refresh')
    result = CliRunner().invoke(cli, ['set-credential', 'trakt', 'refresh_token'])
    assert result.exit_code == 0, result.output
    assert owner.capture(lambda **_: None, lambda: None).values['refresh_token'] == 'explicit-refresh'
    assert daemon.handle_request({'cmd': 'delete_credential', 'plugin': 'trakt', 'key': 'refresh_token'})['status'] == 'disconnected'
    with pytest.raises(SourceHold):
        owner.capture(lambda **_: None, lambda: None)


def test_actual_oauth_routes_captured_client_and_latest_attempt(owner, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fulcra_collect.routes.oauth import register
    from fulcra_media.plugins.trakt import PLUGIN
    from fulcra_media import trakt_oauth
    import httpx
    calls = []
    def grant(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={'access_token': 'oauth-a', 'refresh_token': 'oauth-r',
                                        'created_at': 1, 'expires_in': 100})
    original_client = httpx.Client
    monkeypatch.setattr(trakt_oauth, 'httpx', SimpleNamespace(
        Client=lambda **kw: original_client(transport=httpx.MockTransport(grant), **kw)))
    daemon = SimpleNamespace(registry=SimpleNamespace(plugins={'trakt': PLUGIN}), _web_url='http://127.0.0.1:9292')
    app = FastAPI()
    register(app, SimpleNamespace(daemon=daemon, require_token=lambda: None))
    with TestClient(app) as client:
        first = client.post('/api/oauth/trakt/start')
        second = client.post('/api/oauth/trakt/start')
        assert first.status_code == second.status_code == 200
        stale = client.get('/api/oauth/trakt/callback', params={'state': first.json()['state'], 'code': 'old'})
        assert stale.status_code == 409 and calls == []
        current = client.get('/api/oauth/trakt/callback', params={'state': second.json()['state'], 'code': 'current'})
        assert current.status_code == 200
        assert client.get('/api/oauth/trakt/callback', params={'state': second.json()['state'], 'code': 'replay'}).status_code == 400
    assert calls[0]['client_id'] == 'synthetic-client'
    assert calls[0]['redirect_uri'] == 'http://127.0.0.1:9292/api/oauth/trakt/callback'
    assert calls[0]['grant_type'] == 'authorization_code'
    assert 'client_secret' not in calls[0]
    saved = owner.capture(lambda **_: None, lambda: None).values
    assert saved['created_at'] == '1' and saved['expires_in'] == '100'


@contextmanager
def _threaded_process_parent():
    """Make the whole-suite threaded-parent condition deterministic and fatal."""
    import threading
    import warnings
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, name='inert-trakt-process-parent')
    thread.start()
    try:
        # CPython clears warning-as-error exceptions inside os.fork(). Record
        # the real warning, then promote it outside that C boundary instead.
        with warnings.catch_warnings(record=True) as observed:
            warnings.filterwarnings('always', category=DeprecationWarning,
                                    message=r'This process .* is multi-threaded')
            assert thread.is_alive()
            yield
        for warning in observed:
            if (issubclass(warning.category, DeprecationWarning)
                    and 'is multi-threaded, use of fork()' in str(warning.message)):
                raise warning.category(str(warning.message))
            warnings.warn_explicit(warning.message, warning.category, warning.filename,
                                   warning.lineno)
    finally:
        stop.set()
        thread.join(5)
    assert not thread.is_alive()


def test_process_child_rejects_escape_before_import_or_mutation(contained, tmp_path, monkeypatch):
    import builtins
    from pathlib import Path
    from _trakt_process_child import _prepare
    base = tmp_path / 'spawn-base'
    base.mkdir(mode=0o700)
    before = dict(os.environ)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith('fulcra'):
            pytest.fail('source imported before child path validation')
        return original_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail('child mutated before path validation')

    with monkeypatch.context() as guard:
        guard.setattr(builtins, '__import__', guarded_import)
        guard.setattr(os, 'open', forbidden)
        guard.setattr(Path, 'mkdir', forbidden)
        with pytest.raises(ValueError, match='escapes its private temporary directory'):
            _prepare(base, tmp_path / 'outside-source.json', base / 'owner', base / 'profile')
    assert dict(os.environ) == before


def test_two_private_processes_one_grant_across_profiles(contained, tmp_path):
    import multiprocessing
    from _trakt_process_child import file_child, private_paths
    root = tmp_path / 'common-owner'
    paths = [tmp_path / ('store' + str(i) + '.json') for i in range(2)]
    profiles = [tmp_path / ('profile' + str(i)) for i in range(2)]
    grants = tmp_path / 'grants'
    private_paths(tmp_path, root, *paths, *profiles, grants)
    from fulcra_collect.trakt_source import _atomic_json, _read_json
    for path in paths:
        _atomic_json(path, {'client_id': 'client', 'client_secret': 'secret',
            'access_token': 'synthetic-access', 'refresh_token': 'same-refresh',
            'created_at': 1, 'expires_in': 2})
    context = multiprocessing.get_context('spawn')
    ready, results, start = context.Queue(), context.Queue(), context.Event()
    children = [context.Process(target=file_child,
        args=(str(tmp_path), str(path), str(root), str(profile), ready, start, results, str(grants)))
        for path, profile in zip(paths, profiles)]
    assert all(path.is_relative_to(tmp_path) for path in [root, *paths, *profiles, grants])
    try:
        with _threaded_process_parent():
            for child in children:
                child.start()
        assert [ready.get(timeout=5), ready.get(timeout=5)] == ['ready', 'ready']
        start.set()
        outcomes = sorted([results.get(timeout=5), results.get(timeout=5)])
        assert outcomes == ['committed', 'held']
        for child in children:
            child.join(5)
            assert child.exitcode == 0
        assert grants.read_text() == 'one-grant\n'
        assert len(_read_json(root / 'journal.json')['consumed']) == 1
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
            if child.pid is not None:
                child.join(5)
        ready.close()
        results.close()


@pytest.mark.parametrize('failure', ['second-write', 'readback', 'journal-commit', 'no-receipt', 'corrupt-receipt'])
def test_publication_crash_holds_then_local_recovery_without_grant(owner, monkeypatch, failure):
    from fulcra_collect import trakt_source as source
    binding = owner.capture(lambda **_: None, lambda: None)
    original_write, original_publish, original_save = owner.store._background, owner.store.publish, owner._save
    def write(key, value):
        if key == 'refresh_token':
            raise OSError('synthetic second-write failure')
        original_write(key, value)
    def bad_readback(values):
        original_publish(values)
        owner.store.set('access_token', 'unverified')
    def bad_commit(journal):
        if next(iter(journal['sources'].values()))['status'] == 'committed':
            raise OSError('synthetic journal commit failure')
        original_save(journal)
    if failure == 'second-write':
        monkeypatch.setattr(owner.store, '_background', write)
    elif failure == 'readback':
        monkeypatch.setattr(owner.store, 'publish', bad_readback)
    elif failure == 'journal-commit':
        monkeypatch.setattr(owner, '_save', bad_commit)
    elif failure == 'corrupt-receipt':
        monkeypatch.setattr(owner.store, 'save_receipt', lambda *a: None)
    calls = []
    def grant(_):
        calls.append('grant')
        if failure == 'no-receipt':
            raise TimeoutError('ambiguous provider outcome')
        return {'access_token': 'recovered-a', 'refresh_token': 'recovered-r'}
    with pytest.raises(Exception):
        binding.refresh(grant)
    monkeypatch.setattr(owner.store, '_background', original_write)
    monkeypatch.setattr(owner.store, 'publish', original_publish)
    monkeypatch.setattr(owner, '_save', original_save)
    if failure in ('no-receipt', 'corrupt-receipt', 'readback'):
        with pytest.raises(Exception):
            owner.capture(lambda **_: None, lambda: None)
    else:
        recovered = owner.capture(lambda **_: None, lambda: None)
        assert recovered.values['refresh_token'] == 'recovered-r'
        recovered.check_before_source()
    assert calls == ['grant']
    assert len(source._read_json(owner.root / 'journal.json')['consumed']) == 1


@pytest.mark.parametrize('crash', ['receipt-ack-lost', 'active-published-journal-lost'])
def test_private_process_receipt_recovery_survives_restart(contained, tmp_path, crash):
    import multiprocessing
    from _trakt_process_child import private_paths, receipt_child
    path, root, profile = tmp_path / 'source.json', tmp_path / 'owner', tmp_path / 'profile'
    private_paths(tmp_path, path, root, profile)
    from fulcra_collect.trakt_source import Owner, FileStore, _atomic_json
    _atomic_json(path, {'client_id': 'client', 'client_secret': 'secret', 'access_token': 'old-a',
        'refresh_token': 'old-r', 'created_at': 1, 'expires_in': 2})
    assert all(p.is_relative_to(tmp_path) for p in (path, root, profile))
    child = multiprocessing.get_context('spawn').Process(target=receipt_child,
        args=(str(tmp_path), str(path), str(root), str(profile), crash))
    try:
        with _threaded_process_parent():
            child.start()
        child.join(5)
        assert child.exitcode == 0
        # Local-only recovery; this path has no provider grant callback at all.
        recovered = Owner(FileStore(path), _root=root).capture(lambda **_: None, lambda: None)
        assert recovered.values['refresh_token'] == 'after-crash-r'
    finally:
        if child.is_alive():
            child.terminate()
        if child.pid is not None:
            child.join(5)


def test_actual_os_user_owner_root_ignores_home_and_profile(_private_trakt_owner, monkeypatch, tmp_path):
    from fulcra_collect import trakt_source as source
    monkeypatch.setattr(source.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_dir=str(tmp_path / 'actual-os-user')))
    monkeypatch.setenv('HOME', str(tmp_path / 'different-home'))
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'different-profile'))
    assert _private_trakt_owner() == tmp_path / 'actual-os-user/.local/state/fulcra-trakt'


def test_unknown_journal_never_reinitializes_or_selects(owner):
    from fulcra_collect.trakt_source import SourceHold
    owner.capture(lambda **_: None, lambda: None)
    path = owner.root / 'journal.json'
    path.write_text('{"version":1,"sources":{},"sources":{},"attempts":{},"consumed":{}}')
    before = path.read_bytes()
    with pytest.raises(SourceHold):
        owner.capture(lambda **_: None, lambda: None)
    assert path.read_bytes() == before


@pytest.mark.parametrize('body', ['{"client_id":"a","client_id":"b"}', '{"expires_in":NaN}', 'null', '[]'])
def test_strict_file_unknown_is_never_missing_or_rewritten(contained, tmp_path, body):
    from fulcra_collect.trakt_source import Owner, FileStore, SourceHold
    path = tmp_path / 'source.json'
    path.write_text(body)
    with pytest.raises(SourceHold):
        Owner(FileStore(path)).capture(lambda **_: None, lambda: None)
    assert path.read_text() == body


def test_source_sampling_runs_final_local_check_after_slow_snapshot(owner, monkeypatch):
    changed = [False]
    def local():
        if changed[0]:
            raise RuntimeError('local authority changed during secret read')
    binding = owner.capture(lambda **_: None, local)
    original = owner.store.snapshot
    def sampled():
        result = original()
        changed[0] = True
        return result
    monkeypatch.setattr(owner.store, 'snapshot', sampled)
    with pytest.raises(RuntimeError, match='during secret read'):
        binding.check_before_source()


def test_successful_pair_never_overwrites_observed_external_tuple_change(owner):
    from fulcra_collect.trakt_source import SourceHold
    binding = owner.capture(lambda **_: None, lambda: None)
    def grant(_):
        owner.store.set('access_token', 'external-new-selection')
        return {'access_token': 'late-a', 'refresh_token': 'late-r'}
    with pytest.raises(SourceHold):
        binding.refresh(grant)
    assert owner.store.snapshot()['access_token'] == 'external-new-selection'


def test_file_rotation_preserves_unrelated_fields_and_private_receipt(contained, tmp_path):
    import stat
    from fulcra_collect.trakt_source import Owner, FileStore, _atomic_json, _read_json
    path = tmp_path / 'source.json'
    values = {'client_id': 'client', 'client_secret': 'legacy-secret', 'access_token': 'old-a',
        'refresh_token': 'old-r', 'created_at': 1, 'expires_in': 2, 'unrelated': {'keep': ['original']}}
    _atomic_json(path, values)
    binding = Owner(FileStore(path)).capture(lambda **_: None, lambda: None)
    consent = binding.row['consent']
    generation = binding.row['generation']
    binding.refresh(lambda _: {'access_token': 'new-a', 'refresh_token': 'new-r', 'created_at': 3, 'expires_in': 4})
    assert binding.row['consent'] == consent and binding.row['generation'] != generation
    assert _read_json(path)['unrelated'] == {'keep': ['original']}
    receipt = next(tmp_path.glob('.source.json.receipt-*'))
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert _read_json(receipt, private=True)['original'] == values


def test_unwrapped_trakt_without_strict_backend_refuses_before_source(contained, monkeypatch):
    from fulcra_media.plugins import trakt
    from fulcra_collect.plugin import RunContext
    from fulcra_collect.state import PluginState
    import logging
    monkeypatch.setattr(trakt.trakt_importer, 'fetch_history', lambda **kw: pytest.fail('implicit file fallback'))
    monkeypatch.setattr(trakt.trakt_importer, 'fetch_history_with_headers', lambda **kw: pytest.fail('unbound source'))
    ctx = RunContext('trakt', {}, {}, PluginState('trakt'), logging.getLogger('synthetic'), lambda _: None)
    with pytest.raises(RuntimeError, match='strict credential backend unavailable'):
        trakt.PLUGIN.run.__wrapped__(ctx)


def test_client_change_discards_pending_manual_pair(owner):
    from fulcra_collect.trakt_source import SourceHold
    owner.capture(lambda **_: None, lambda: None)
    owner.edit('access_token', 'new-a')
    owner.edit('client_id', 'new-client')
    assert owner.edit('refresh_token', 'new-r')['ready'] is False
    with pytest.raises(SourceHold):
        owner.capture(lambda **_: None, lambda: None)
    assert owner.edit('access_token', 'new-a')['ready'] is True
    assert owner.capture(lambda **_: None, lambda: None).values['client_id'] == 'new-client'


def test_ancillary_edit_preserves_proven_pair_but_revokes_old_work(owner):
    from fulcra_collect.trakt_source import SourceHold
    old = owner.capture(lambda **_: None, lambda: None)
    assert owner.edit('client_secret', 'intentional-legacy-secret')['ready'] is True
    with pytest.raises(SourceHold):
        old.check_before_source()
    selected = owner.capture(lambda **_: None, lambda: None)
    assert selected.values['access_token'] == old.values['access_token']
    assert selected.values['refresh_token'] == old.values['refresh_token']
    assert owner.edit('client_secret', None)['ready'] is True
    assert owner.capture(lambda **_: None, lambda: None).values['client_secret'] is None


@pytest.mark.parametrize('supersede', ['manual', 'disconnect', 'login'])
def test_late_oauth_response_is_quarantined(owner, supersede):
    from fulcra_collect import trakt_source as source
    attempt = owner.begin_authorization('state1', 'verifier', 'http://127.0.0.1/cb')
    def grant(_):
        if supersede == 'login':
            owner.begin_authorization('state2', 'new-verifier', 'http://127.0.0.1/cb')
        else:
            owner.edit('access_token', 'manual-selection' if supersede == 'manual' else None)
        return {'access_token': 'late-a', 'refresh_token': 'late-r', 'created_at': '1',
                'expires_in': '2', 'redirect_uri': 'http://127.0.0.1/cb'}
    with pytest.raises(source.SourceHold):
        attempt.exchange('code', grant)
    assert owner.store.snapshot()['access_token'] != 'late-a'
    journal = source._read_json(owner.root / 'journal.json')
    assert journal['attempts'][attempt.attempt['id']]['status'] == 'quarantined'
    assert owner.store.receipt(attempt.attempt['id'])['values']['refresh_token'] == 'late-r'


def test_cli_final_local_guard_follows_saved_authority_reads(contained, configured, monkeypatch):
    from fulcra_media import media_config
    with configured.open() as run:
        def during_journal(*args):
            run.session.cancel()
        monkeypatch.setattr(media_config, '_journal_check', during_journal)
        with pytest.raises(RuntimeError, match='Standalone authority changed'):
            run.ctx._media_local_authority()


@pytest.mark.parametrize('field', ['access_token', 'refresh_token'])
@pytest.mark.parametrize('failure', ['before-write', 'after-write', 'readback', 'credit-save'])
@pytest.mark.parametrize('later', ['pair', 'ancillary'])
def test_round1_manual_attempt_requires_verified_field(owner, monkeypatch, field, failure, later):
    from fulcra_collect import trakt_source as source
    owner.capture(lambda **_: None, lambda: None)
    other = 'refresh_token' if field == 'access_token' else 'access_token'
    original_set, original_snapshot, original_save = owner.store.set, owner.store.snapshot, owner._save
    attempted = []
    def setter(key, value):
        if key == field:
            attempted.append(True)
            if failure == 'before-write':
                raise OSError('synthetic before write')
        original_set(key, value)
        if key == field and failure == 'after-write':
            raise OSError('synthetic acknowledgement lost')
    def snapshot():
        if attempted and failure == 'readback':
            raise OSError('synthetic readback unavailable')
        return original_snapshot()
    def save(journal):
        if attempted and failure == 'credit-save':
            raise OSError('synthetic before verified credit publication')
        original_save(journal)
    monkeypatch.setattr(owner.store, 'set', setter)
    monkeypatch.setattr(owner.store, 'snapshot', snapshot)
    monkeypatch.setattr(owner, '_save', save)
    with pytest.raises(OSError):
        owner.edit(field, 'explicit-' + field)
    monkeypatch.setattr(owner.store, 'set', original_set)
    monkeypatch.setattr(owner.store, 'snapshot', original_snapshot)
    monkeypatch.setattr(owner, '_save', original_save)
    result = owner.edit(other, 'explicit-' + other)
    assert result['ready'] is False
    if later == 'ancillary':
        assert owner.edit('client_secret', 'explicit-secret')['ready'] is False
    with pytest.raises(source.SourceHold):
        owner.capture(lambda **_: None, lambda: None)
    assert owner.edit(field, 'explicit-' + field)['ready'] is True
    binding = owner.capture(lambda **_: None, lambda: None)
    assert binding.values[field] == 'explicit-' + field
    assert binding.values[other] == 'explicit-' + other


@pytest.mark.parametrize('later', ['pair', 'ancillary'])
def test_round1_verified_pending_field_digest_cannot_drift(owner, later):
    from fulcra_collect import trakt_source as source
    owner.capture(lambda **_: None, lambda: None)
    assert owner.edit('access_token', 'verified-access')['ready'] is False
    owner.store.set('access_token', 'unverified-drift')
    assert owner.edit('refresh_token', 'verified-refresh')['ready'] is False
    if later == 'ancillary':
        assert owner.edit('client_secret', 'explicit-secret')['ready'] is False
    with pytest.raises(source.SourceHold):
        owner.capture(lambda **_: None, lambda: None)
    assert owner.edit('access_token', 'verified-access')['ready'] is True


@pytest.mark.parametrize('boundary', ['refresh', 'recovery'])
def test_round1_successor_never_adopts_other_profile_consent(owner, monkeypatch, tmp_path, boundary):
    from fulcra_collect import config, trakt_source as source
    cfg = config.load()
    cfg.enable('trakt')
    config.save(cfg)
    profile_a, epoch_a = config.profile_path(), config.load().plugin_epochs['trakt']
    def authority_a():
        assert config.profile_path() == profile_a
        assert config.load().plugin_epochs['trakt'] == epoch_a
    binding = owner.capture(authority_a, authority_a)
    consent = binding.row['consent']
    original_publish = owner._publish
    if boundary == 'recovery':
        monkeypatch.setattr(owner, '_publish', lambda *a: (_ for _ in ()).throw(OSError('synthetic before publish')))
        with pytest.raises(OSError):
            binding.refresh(lambda _: {'access_token': 'successor-a', 'refresh_token': 'successor-r'})
    def other_profile_after_publish(*args):
        result = original_publish(*args)
        with monkeypatch.context() as selected:
            selected.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile-b'))
            other = config.load()
            other.enable('trakt')
            config.save(other)
            assert owner.edit('access_token', 'other-profile-a')['ready'] is False
            assert owner.edit('refresh_token', 'other-profile-r')['ready'] is True
        authority_a()  # B's edit did not rotate A's Fulcra epoch.
        return result
    monkeypatch.setattr(owner, '_publish', other_profile_after_publish)
    with pytest.raises(source.SourceHold):
        if boundary == 'refresh':
            binding.refresh(lambda _: {'access_token': 'successor-a', 'refresh_token': 'successor-r'})
        else:
            owner.capture(authority_a, authority_a)
    assert binding.row['consent'] == consent
    assert owner.store.snapshot()['access_token'] == 'other-profile-a'
    authority_a()


@pytest.mark.parametrize('supersede', ['disconnect', 'new-setup'])
def test_round1_paused_setup_ui_does_not_hold_short_control_locks(owner, contained, monkeypatch, supersede):
    import threading
    from fulcra_collect import credentials, config, trakt_source as source
    from fulcra_collect.config_leases import settings_update_scope
    owner.capture(lambda **_: None, lambda: None)
    entered, release = threading.Event(), threading.Event()
    errors, completed = [], []
    original_set = credentials.keyring.set_password
    def paused():
        entered.set()
        assert release.wait(3), 'synthetic UI was not released'
    def ui_write(service, key, value):
        if key == 'access_token' and value == 'paused-explicit-access':
            paused()
        original_set(service, key, value)
    def authorize(plugin, key):
        assert plugin == 'trakt'
        if key == 'access_token':
            paused()
        return True
    # UI-capable legacy setter and new explicit authorization are both fake.
    # The quiet publication backend writes intrinsically to this private dict.
    def quiet(key, value):
        item = ('fulcra-collect:trakt', key)
        if value is None:
            contained.store.pop(item, None)
        else:
            contained.store[item] = value
    owner.store._quiet_write = quiet
    monkeypatch.setattr(credentials.keyring, 'set_password', ui_write)
    monkeypatch.setattr(credentials, 'authorize_secret', authorize)
    monkeypatch.setattr(source, 'settings_update_scope', lambda: settings_update_scope(timeout_s=.2))
    def old_setup():
        try:
            completed.append(owner.edit('access_token', 'paused-explicit-access'))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=old_setup)
    thread.start()
    try:
        assert entered.wait(1)
        if supersede == 'disconnect':
            assert owner.edit('refresh_token', None)['status'] == 'disconnected'
        else:
            latest = owner.begin_authorization('new-state', 'new-verifier', 'http://127.0.0.1/cb')
            assert latest.values['client_id'] == 'synthetic-client'
        epoch = config.load().plugin_epochs['trakt']
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert completed == []
    assert len(errors) == 1 and isinstance(errors[0], source.SourceHold)
    assert owner.store.snapshot()['access_token'] == 'synthetic-access'
    assert config.load().plugin_epochs['trakt'] == epoch


@pytest.mark.parametrize('field', ['access_token', 'refresh_token'])
@pytest.mark.parametrize('failure', ['before-write', 'after-write', 'readback', 'credit-save'])
def test_round1_failed_second_manual_field_cannot_be_completed_by_ancillary(owner, monkeypatch, field, failure):
    from fulcra_collect import trakt_source as source
    owner.capture(lambda **_: None, lambda: None)
    other = 'refresh_token' if field == 'access_token' else 'access_token'
    assert owner.edit(other, 'verified-' + other)['ready'] is False
    original_set, original_snapshot, original_save = owner.store.set, owner.store.snapshot, owner._save
    wrote = []
    def setter(key, value):
        if key == field:
            wrote.append(True)
            if failure == 'before-write':
                raise OSError('synthetic write failure')
        original_set(key, value)
        if key == field and failure == 'after-write':
            raise OSError('synthetic write acknowledgement lost')
    def snapshot():
        if wrote and failure == 'readback':
            raise OSError('synthetic readback failure')
        return original_snapshot()
    def save(journal):
        if wrote and failure == 'credit-save':
            raise OSError('synthetic verified credit not persisted')
        original_save(journal)
    with monkeypatch.context() as fault:
        fault.setattr(owner.store, 'set', setter)
        fault.setattr(owner.store, 'snapshot', snapshot)
        fault.setattr(owner, '_save', save)
        with pytest.raises(OSError):
            owner.edit(field, 'verified-' + field)
    assert owner.edit('client_secret', 'ancillary-only')['ready'] is False
    with pytest.raises(source.SourceHold):
        owner.capture(lambda **_: None, lambda: None)
    assert owner.edit(field, 'verified-' + field)['ready'] is True


def test_round1_verified_credit_ack_loss_requires_exact_durable_digest(owner, monkeypatch):
    from fulcra_collect import trakt_source as source
    owner.capture(lambda **_: None, lambda: None)
    original = owner._save
    def saved_then_lost(journal):
        original(journal)
        row = journal['sources'][owner.key]
        if isinstance(row.get('pending'), dict) and 'access_token' in row['pending']:
            raise OSError('synthetic durable credit acknowledgement lost')
    with monkeypatch.context() as lost:
        lost.setattr(owner, '_save', saved_then_lost)
        with pytest.raises(OSError):
            owner.edit('access_token', 'durably-verified-access')
    journal = source._read_json(owner.root / 'journal.json')
    assert journal['sources'][owner.key]['pending'] == {'access_token': source._hash('durably-verified-access')}
    assert 'durably-verified-access' not in (owner.root / 'journal.json').read_text()
    assert owner.edit('refresh_token', 'verified-refresh')['ready'] is True


@pytest.mark.parametrize('result', [False, None, 'denied', 'raise'])
def test_round1_explicit_authorization_absent_vs_unknown_is_strict(owner, monkeypatch, result):
    from fulcra_collect import credentials, trakt_source as source
    owner.capture(lambda **_: None, lambda: None)
    before = owner.store.snapshot()
    def authorize(*args):
        if result == 'raise':
            raise credentials.KeychainAccessRequired('synthetic access denied')
        return result
    monkeypatch.setattr(credentials, 'authorize_secret', authorize)
    if result is False:
        assert owner.edit('access_token', 'explicit-access')['ready'] is False
        assert owner.store.snapshot()['access_token'] == 'explicit-access'
    else:
        with pytest.raises((source.SourceHold, credentials.KeychainAccessRequired)):
            owner.edit('access_token', 'explicit-access')
        assert owner.store.snapshot() == before
        with pytest.raises(source.SourceHold):
            owner.capture(lambda **_: None, lambda: None)


def test_round1_actual_daemon_disconnect_supersedes_paused_manual_setup(owner, contained, monkeypatch):
    import threading
    from fulcra_collect import config, credentials, trakt_source as source
    from fulcra_collect.config_leases import settings_update_scope
    from fulcra_collect.daemon import Daemon
    from fulcra_collect.registry import RegistryResult
    from fulcra_media.plugins.trakt import PLUGIN
    owner.capture(lambda **_: None, lambda: None)
    entered, release = threading.Event(), threading.Event()
    def authorize(plugin, key):
        assert plugin == 'trakt'
        if key == 'access_token':
            entered.set()
            assert release.wait(3)
        return True
    def quiet(key, value):
        item = ('fulcra-collect:trakt', key)
        if value is None:
            contained.store.pop(item, None)
        else:
            contained.store[item] = value
    original_init = source.KeychainStore.__init__
    monkeypatch.setattr(source.KeychainStore, '__init__',
        lambda self, **kw: original_init(self, _quiet_write=quiet))
    monkeypatch.setattr(credentials, 'authorize_secret', authorize)
    monkeypatch.setattr(source, 'settings_update_scope', lambda: settings_update_scope(timeout_s=.2))
    daemon = Daemon(registry=RegistryResult(plugins={'trakt': PLUGIN}), config=config.load())
    responses = []
    thread = threading.Thread(target=lambda: responses.append(daemon.handle_request({
        'cmd': 'set_credential', 'plugin': 'trakt', 'key': 'access_token', 'secret': 'stale-active-value'})))
    thread.start()
    try:
        assert entered.wait(1)
        response = daemon.handle_request({'cmd': 'delete_credential', 'plugin': 'trakt', 'key': 'refresh_token'})
        assert response['ok'] is True and response['status'] == 'disconnected'
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert responses == [{'ok': False, 'error': 'keychain write failed'}]
    assert owner.store.snapshot()['access_token'] == 'synthetic-access'
    assert owner.store.snapshot()['refresh_token'] is None
