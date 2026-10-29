"""Credential reads must not multiply macOS access prompts or hide UNKNOWN."""
from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import keyring
import pytest

from fulcra_collect import credentials


@pytest.fixture(autouse=True)
def _fake_backend(monkeypatch):
    # All native-boundary tests inject a fake API explicitly. No user item IO.
    monkeypatch.setattr(credentials, "_uses_macos_keychain", lambda: False)


def test_timeout_keeps_one_pending_os_read(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def read(*args):
        calls.append(args)
        started.set()
        release.wait(2)
        return 'old'

    monkeypatch.setattr(keyring, 'get_password', read)
    try:
        with pytest.raises(TimeoutError):
            credentials._keyring_get('singleflight-test', 'item', timeout=.01)
        assert started.is_set()
        with ThreadPoolExecutor(8) as pool:
            futures = [pool.submit(credentials._keyring_get, 'singleflight-test', 'item', timeout=.01)
                       for _ in range(8)]
            for future in futures:
                with pytest.raises(credentials.KeychainAccessRequired):
                    future.result()
        assert len(calls) == 1
    finally:
        release.set()


def test_default_timeout_is_read_at_call_time(monkeypatch):
    import time
    release = threading.Event()
    monkeypatch.setattr(credentials, '_KEYCHAIN_READ_TIMEOUT_S', .01)
    monkeypatch.setattr(keyring, 'get_password', lambda *_: release.wait(1))
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            credentials._keyring_get('timeout-test', 'item')
        assert time.monotonic() - start < .3
    finally:
        release.set()


def test_background_native_read_never_allows_ui(monkeypatch):
    reads = []
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: True)
    from fulcra_collect import macos_keychain
    monkeypatch.setattr(macos_keychain, 'get_password',
                        lambda service, key, *, allow_ui=False: reads.append((service, key, allow_ui)) or 'secret')
    assert credentials.get_secret('lastfm', 'api-key') == 'secret'
    assert reads == [('fulcra-collect:lastfm', 'api-key', False)]


def test_denied_native_read_has_no_interactive_fallback(monkeypatch):
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: True)
    from fulcra_collect import macos_keychain

    def denied(*_, **kw):
        assert kw['allow_ui'] is False
        raise credentials.KeychainAccessRequired('Access required')

    monkeypatch.setattr(macos_keychain, 'get_password', denied)
    monkeypatch.setattr(keyring, 'get_password', lambda *_: pytest.fail('interactive fallback'))
    with pytest.raises(credentials.KeychainAccessRequired):
        credentials.has_secret('lastfm', 'api-key')


def test_explicit_read_is_scoped_and_does_not_return_secret(monkeypatch):
    reads = []
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: True)
    from fulcra_collect import macos_keychain
    monkeypatch.setattr(macos_keychain, 'get_password',
                        lambda service, key, *, allow_ui=False: reads.append((service, key, allow_ui)) or 'PRIVATE')
    assert credentials.authorize_secret('lastfm', 'api-key') is True
    assert reads == [('fulcra-collect:lastfm', 'api-key', True),
                     ('fulcra-collect:lastfm', 'api-key', False)]


@pytest.mark.parametrize('mutation', ['set', 'delete', 'clear'])
def test_late_user_read_cannot_poison_cache(monkeypatch, mutation):
    started, release = threading.Event(), threading.Event()
    reads = iter(['old', 'new'])

    def read(*_):
        value = next(reads)
        if value == 'old':
            started.set()
            release.wait(2)
        return value

    monkeypatch.setattr(credentials, '_keyring_get', read)
    monkeypatch.setattr(keyring, 'set_password', lambda *_: None)
    monkeypatch.setattr(keyring, 'delete_password', lambda *_: None)
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(credentials.get_user_secret, 'race-key')
        assert started.wait(1)
        if mutation == 'set':
            credentials.set_user_secret('race-key', 'new')
        elif mutation == 'delete':
            credentials.delete_user_secret('race-key')
        else:
            credentials._clear_caches()
        release.set()
        with pytest.raises(credentials.KeychainAccessRequired):
            pending.result()
    if mutation == 'set':
        assert credentials.get_user_secret('race-key') == 'new'
    else:
        # Cache must not retain the old value; the next read is fresh.
        assert credentials.get_user_secret('race-key') == 'new'


def _plugin(*, user_level=False):
    from fulcra_collect.plugin import Credential, Plugin
    return Plugin(id='lastfm', name='Last.fm', kind='manual', collect_mode='historical',
                  run=lambda _: pytest.fail('provider work must not run'),
                  required_credentials=(Credential(key='api-key', label='API key', help='',
                                                   user_level=user_level),))


@pytest.mark.parametrize('user_level', [False, True])
def test_daemon_authorize_validates_declared_scope_and_hides_value(collect_home, monkeypatch, user_level):
    from fulcra_collect.daemon import Daemon, Config
    from fulcra_collect.registry import RegistryResult
    d = Daemon(registry=RegistryResult(plugins={'lastfm': _plugin(user_level=user_level)}), config=Config())
    reads = []
    monkeypatch.setattr(credentials, 'authorize_secret', lambda p, k: reads.append((p, k)) or True)
    monkeypatch.setattr(credentials, 'authorize_user_secret', lambda k: reads.append(('user', k)) or True)
    assert d.handle_request({'cmd': 'authorize_credential', 'plugin': 'lastfm', 'key': 'wrong'})['ok'] is False
    assert not reads
    result = d.handle_request({'cmd': 'authorize_credential', 'plugin': 'lastfm', 'key': 'api-key'})
    assert result == {'ok': True, 'status': 'set'}
    assert reads == [('user' if user_level else 'lastfm', 'api-key')]


def test_status_distinguishes_required_access_from_missing(collect_home, monkeypatch):
    from fulcra_collect.daemon import Daemon, Config
    from fulcra_collect.registry import RegistryResult
    d = Daemon(registry=RegistryResult(plugins={'lastfm': _plugin()}), config=Config())

    def denied(*_):
        raise credentials.KeychainAccessRequired('PRIVATE must never appear')

    monkeypatch.setattr(credentials, 'has_secret', denied)
    result = d.handle_request({'cmd': 'credential_status', 'plugin': 'lastfm'})
    assert result['ok'] is False
    assert result['credentials'] == {'api-key': 'access_required'}
    assert result['code'] == 'keychain_access_required'
    assert 'PRIVATE' not in json.dumps(result)


def test_worker_emits_access_error_before_source_io(collect_home, monkeypatch):
    from fulcra_collect.worker import run_plugin

    def denied(*_):
        raise credentials.KeychainAccessRequired('PRIVATE')

    monkeypatch.setattr(credentials, 'get_secret', denied)
    out = io.StringIO()
    assert run_plugin(_plugin(), out=out) == 'error'
    result = json.loads(out.getvalue())
    assert result['code'] == 'keychain_access_required'
    assert 'PRIVATE' not in result['error']


def test_one_time_allow_does_not_masquerade_as_background_access(monkeypatch):
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: True)
    from fulcra_collect import macos_keychain
    def read(*_, allow_ui=False):
        if allow_ui:
            return 'secret'
        raise credentials.KeychainAccessRequired('quiet access still denied')
    monkeypatch.setattr(macos_keychain, 'get_password', read)
    with pytest.raises(credentials.KeychainAccessRequired):
        credentials.authorize_secret('lastfm', 'api-key')


def test_unused_optional_credential_still_allows_local_mode(collect_home, monkeypatch):
    from fulcra_collect.plugin import Credential, Plugin
    from fulcra_collect.worker import run_plugin
    ran = []
    plugin = Plugin(id='local-mode', name='Local mode', kind='manual', collect_mode='historical',
                    run=lambda ctx: ran.append(ctx.credentials['api-key']),
                    required_credentials=(Credential(key='api-key',label='API', help='', required=False),))
    def denied(*_):
        raise credentials.KeychainAccessRequired('PRIVATE')
    monkeypatch.setattr(credentials, 'get_secret', denied)
    out=io.StringIO()
    assert run_plugin(plugin,out=out)=='done'
    assert ran==[None]


def test_mutation_overlapping_raw_fresh_read_rejects_completion(monkeypatch):
    started, release=threading.Event(), threading.Event()
    def read(*_):
        started.set()
        release.wait(2)
        return 'old-account'
    monkeypatch.setattr(keyring,'get_password',read)
    monkeypatch.setattr(keyring,'set_password',lambda *_:None)
    with ThreadPoolExecutor(1) as pool:
        pending=pool.submit(credentials.get_user_secret_fresh,'bearer-token')
        assert started.wait(1)
        credentials.set_user_secret('bearer-token','new-account')
        release.set()
        with pytest.raises(credentials.KeychainAccessRequired):
            pending.result()


@pytest.mark.parametrize('status', [-25308, -25293, -128, -25300, -50])
def test_native_query_fails_without_ui_and_only_notfound_is_absent(monkeypatch,status):
    from contextlib import contextmanager
    from fulcra_collect import macos_keychain
    fields=[]
    class Api:
        def constant(self, name): return name
        @contextmanager
        def query(self, value):
            fields.append(value)
            yield 'query'
        def copy(self,*_): return status
    monkeypatch.setattr(macos_keychain,'_api',lambda:Api())
    if status == -25300:
        assert macos_keychain._read_password('service','account') is None
    elif status in {-25308,-25293,-128}:
        with pytest.raises(credentials.KeychainAccessRequired):
            macos_keychain._read_password('service','account')
    else:
        with pytest.raises(keyring.errors.KeyringError):
            macos_keychain._read_password('service','account')
    assert fields[0]['kSecUseAuthenticationUI']=='kSecUseAuthenticationUIFail'


def test_core_auth_access_is_unknown_and_has_explicit_scoped_recovery(collect_home, monkeypatch):
    from fastapi.testclient import TestClient
    from fulcra_collect.daemon import Daemon, Config
    from fulcra_collect.registry import RegistryResult
    from fulcra_collect.web import build_app, _ensure_token
    client=TestClient(build_app(Daemon(registry=RegistryResult(plugins={}),config=Config())))
    def denied(*_):
        raise credentials.KeychainAccessRequired('PRIVATE')
    monkeypatch.setattr(credentials,'get_user_secret_fresh',denied)
    assert client.post('/api/fulcra/auth/keychain_access').status_code==401
    client.headers['Authorization']='Bearer '+_ensure_token()
    result=client.get('/api/fulcra/auth/status').json()
    assert result['authenticated'] is None
    assert result['code']=='keychain_access_required'
    assert 'PRIVATE' not in json.dumps(result)
    calls=[]
    monkeypatch.setattr(credentials,'authorize_user_secret',lambda key:calls.append(key) or True)
    assert client.post('/api/fulcra/auth/keychain_access').json()['ok'] is True
    assert calls==['bearer-token','oauth-credentials']


def test_pending_global_cap_and_late_result_not_reused(monkeypatch):
    import time
    release=threading.Event()
    calls=[]
    def read(service,*_):
        calls.append(service)
        release.wait(2)
        return 'late-old'
    monkeypatch.setattr(keyring,'get_password',read)
    monkeypatch.setattr(credentials,'_MAX_PENDING_READS',2)
    try:
        for service in ['cap-1','cap-2']:
            with pytest.raises(TimeoutError):
                credentials._keyring_get(service,'item',timeout=.01)
        with pytest.raises(credentials.KeychainAccessRequired):
            credentials._keyring_get('cap-3','item',timeout=.01)
        assert calls==['cap-1','cap-2']
    finally:
        release.set()
        deadline=time.monotonic()+1
        while credentials._pending_reads and time.monotonic()<deadline:
            time.sleep(.001)
    monkeypatch.setattr(keyring,'get_password',lambda *_:'current-new')
    assert credentials._keyring_get('cap-1','item')=='current-new'


def test_health_access_error_never_calls_provider(collect_home,monkeypatch):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from fulcra_collect.daemon import Daemon,Config
    from fulcra_collect.registry import RegistryResult
    from fulcra_collect.web import build_app,_ensure_token
    plugin=replace(_plugin(),health_check=lambda _:pytest.fail('provider IO'))
    client=TestClient(build_app(Daemon(registry=RegistryResult(plugins={'lastfm':plugin}),config=Config())))
    client.headers['Authorization']='Bearer '+_ensure_token()
    def denied(*_):raise credentials.KeychainAccessRequired('PRIVATE')
    monkeypatch.setattr(credentials,'get_secret',denied)
    result=client.post('/api/plugin/lastfm/health_check').json()
    assert result['ok'] is False
    assert result['code']=='keychain_access_required'
    assert 'PRIVATE' not in json.dumps(result)


def test_generic_keyring_failure_is_structured_and_private(collect_home,monkeypatch):
    from dataclasses import replace
    from fastapi.testclient import TestClient
    from fulcra_collect.worker import run_plugin
    from fulcra_collect.daemon import Daemon,Config
    from fulcra_collect.registry import RegistryResult
    from fulcra_collect.web import build_app,_ensure_token
    plugin=replace(_plugin(),health_check=lambda _:pytest.fail('provider IO'))
    def unavailable(*_):raise keyring.errors.KeyringError('PRIVATE')
    monkeypatch.setattr(credentials,'get_secret',unavailable)
    out=io.StringIO()
    assert run_plugin(plugin,out=out)=='error'
    worker=json.loads(out.getvalue())
    assert worker['code']=='keychain_unavailable'
    assert 'PRIVATE' not in json.dumps(worker)
    client=TestClient(build_app(Daemon(registry=RegistryResult(plugins={'lastfm':plugin}),config=Config())))
    client.headers['Authorization']='Bearer '+_ensure_token()
    result=client.post('/api/plugin/lastfm/health_check').json()
    assert result['code']=='keychain_unavailable'
    assert 'PRIVATE' not in json.dumps(result)


def test_isolated_reader_uses_identical_bundle_executable(monkeypatch):
    from fulcra_collect import macos_keychain
    monkeypatch.setattr(macos_keychain.sys,'executable','/private/Test.app/Contents/MacOS/Fulcra Collect')
    assert macos_keychain.isolated_command()==['/private/Test.app/Contents/MacOS/Fulcra Collect','--keychain-read']


def test_child_disables_legacy_ui_before_reading(monkeypatch):
    from fulcra_collect import macos_keychain
    order=[]
    class Api:
        def disable_interaction(self):order.append('disable')
    monkeypatch.setattr(macos_keychain,'_api',lambda:Api())
    monkeypatch.setattr(macos_keychain,'_read_password',lambda *_,**kw:order.append('read') or 'synthetic')
    monkeypatch.setattr(macos_keychain.sys,'stdin',io.StringIO(json.dumps({'service':'fulcra-collect:test','account':'key'})))
    output=io.StringIO()
    monkeypatch.setattr(macos_keychain.sys,'stdout',output)
    assert macos_keychain.child_main()==0
    assert order==['disable','read']
    assert json.loads(output.getvalue())=={'status':'ok','value':'synthetic'}


def test_child_refuses_unrelated_item_without_reading(monkeypatch):
    from fulcra_collect import macos_keychain
    monkeypatch.setattr(macos_keychain.sys,'stdin',io.StringIO(json.dumps({'service':'unrelated-app','account':'key'})))
    output=io.StringIO()
    monkeypatch.setattr(macos_keychain.sys,'stdout',output)
    monkeypatch.setattr(macos_keychain,'_api',lambda:pytest.fail('invalid request must not reach OS'))
    assert macos_keychain.child_main()==1
    assert json.loads(output.getvalue())=={'status':'unavailable'}


@pytest.mark.parametrize('packet', [
    '{"service":"fulcra-collect:test","service":"fulcra-collect:other","account":"k"}',
    '{"service":"fulcra-collect:test","account":"k","extra":true}',
    '{"service":"fulcra-collect:test","account":"k\\u0000x"}',
])
def test_invalid_child_packets_never_reach_os(monkeypatch,packet):
    from fulcra_collect import macos_keychain
    monkeypatch.setattr(macos_keychain.sys,'stdin',io.StringIO(packet))
    out=io.StringIO()
    monkeypatch.setattr(macos_keychain.sys,'stdout',out)
    monkeypatch.setattr(macos_keychain,'_api',lambda:pytest.fail('OS access'))
    assert macos_keychain.child_main()==1
    assert json.loads(out.getvalue())=={'status':'unavailable'}


def test_child_timeout_kills_owned_group_reaps_and_never_leaks_output(monkeypatch):
    from types import SimpleNamespace
    from fulcra_collect import macos_keychain
    actions=[]
    proc=SimpleNamespace(pid=12345,returncode=None,stdin=io.BytesIO(),stdout=io.BytesIO(),stderr=io.BytesIO(),
                         poll=lambda:None,wait=lambda **_:actions.append('wait'))
    monkeypatch.setattr(macos_keychain.subprocess,'Popen',lambda *_,**kw:proc)
    monkeypatch.setattr(macos_keychain.os,'killpg',lambda pid,sig:actions.append(('kill',pid,sig)))
    def timeout(*_,**kw):
        raise macos_keychain.subprocess.TimeoutExpired('child',2,output=b'PRIVATE',stderr=b'PRIVATE')
    monkeypatch.setattr(macos_keychain,'_communicate',timeout)
    with pytest.raises(credentials.KeychainAccessRequired) as exc:
        macos_keychain.get_password('fulcra-collect:test','key')
    assert 'PRIVATE' not in str(exc.value)
    assert actions[0][0:2]==('kill',12345)
    assert actions[1]=='wait'
    assert all(stream.closed for stream in (proc.stdin,proc.stdout,proc.stderr))


def test_child_pipe_holding_descendant_cannot_outlive_read_deadline(monkeypatch):
    import sys
    import time
    from fulcra_collect import macos_keychain
    # Pure synthetic process: no native Keychain imports or reads.
    code=('import sys,subprocess; '
          'subprocess.Popen([sys.executable,"-c","import time; time.sleep(5)"]); '
          'print(\'{"status":"ok","value":"synthetic"}\',flush=True)')
    monkeypatch.setattr(macos_keychain,'isolated_command',lambda:[sys.executable,'-c',code])
    monkeypatch.setattr(macos_keychain,'_CHILD_TIMEOUT_S',.08)
    start=time.monotonic()
    with pytest.raises(credentials.KeychainAccessRequired):
        macos_keychain.get_password('fulcra-collect:test','key')
    assert time.monotonic()-start < .8


@pytest.mark.parametrize('operation,status,expected', [
    ('set', 0, 'ok'), ('set', -25300, 'add'), ('set', -25308, 'access'),
    ('delete', 0, 'ok'), ('delete', -25300, 'ok'), ('delete', -25293, 'access'),
    ('set', -50, 'unknown'),
])
def test_trakt_quiet_mutation_native_contract(monkeypatch, operation, status, expected):
    from contextlib import contextmanager
    from fulcra_collect import macos_keychain
    fields, calls = [], []
    class Api:
        def constant(self, name):
            return name
        @contextmanager
        def query(self, values):
            fields.append(values)
            yield values
        def update(self, query, changes):
            calls.append('update')
            return status
        def add(self, query, result):
            calls.append('add')
            return 0
        def delete(self, query):
            calls.append('delete')
            return status
    monkeypatch.setattr(macos_keychain, '_api', lambda: Api())
    request = {'op': operation, 'service': 'fulcra-collect:trakt', 'account': 'source-receipt-attempt'}
    if operation == 'set':
        request['value'] = 'SYNTHETIC_SECRET'
    if expected == 'access':
        with pytest.raises(credentials.KeychainAccessRequired):
            macos_keychain._mutate_password(request)
    elif expected == 'unknown':
        with pytest.raises(keyring.errors.KeyringError):
            macos_keychain._mutate_password(request)
    else:
        macos_keychain._mutate_password(request)
    assert fields[0]['kSecUseAuthenticationUI'] == 'kSecUseAuthenticationUIFail'
    assert calls == (['update', 'add'] if expected == 'add' else ['update' if operation == 'set' else 'delete'])
    if operation == 'set':
        assert fields[1] == {'kSecValueData': b'SYNTHETIC_SECRET'}


@pytest.mark.parametrize('packet', [
    {'op': 'set', 'service': 'fulcra-collect:other', 'account': 'key', 'value': 'secret'},
    {'op': 'set', 'service': 'fulcra-collect:trakt', 'account': 'key', 'value': 'x' * 65537},
    {'op': 'delete', 'service': 'fulcra-collect:trakt', 'account': 'key', 'value': 'unexpected'},
    {'op': 'set', 'service': 'fulcra-collect:trakt', 'account': 'key', 'value': 3},
])
def test_invalid_trakt_mutation_child_packet_never_reaches_os(monkeypatch, packet):
    from fulcra_collect import macos_keychain
    monkeypatch.setattr(macos_keychain.sys, 'stdin', io.StringIO(json.dumps(packet)))
    out = io.StringIO()
    monkeypatch.setattr(macos_keychain.sys, 'stdout', out)
    monkeypatch.setattr(macos_keychain, '_api', lambda: pytest.fail('OS access'))
    assert macos_keychain.child_main() == 1
    assert json.loads(out.getvalue()) == {'status': 'unavailable'}


def test_trakt_mutation_child_disables_ui_and_ack_has_no_secret(monkeypatch):
    from fulcra_collect import macos_keychain
    from types import SimpleNamespace
    order = []
    monkeypatch.setattr(macos_keychain, '_api', lambda: SimpleNamespace(disable_interaction=lambda: order.append('disable')))
    monkeypatch.setattr(macos_keychain, '_mutate_password', lambda _: order.append('write'))
    monkeypatch.setattr(macos_keychain.sys, 'stdin', io.StringIO(json.dumps({
        'op': 'set', 'service': 'fulcra-collect:trakt', 'account': 'receipt', 'value': 'PRIVATE'})))
    out = io.StringIO()
    monkeypatch.setattr(macos_keychain.sys, 'stdout', out)
    assert macos_keychain.child_main() == 0
    assert order == ['disable', 'write']
    assert json.loads(out.getvalue()) == {'status': 'ok'}
    assert 'PRIVATE' not in out.getvalue()


@pytest.mark.parametrize('ack', [{'status': 'ok', 'value': 'PRIVATE'}, {'status': 'unavailable'}, None])
def test_trakt_mutation_ambiguous_child_ack_is_unknown_without_fallback(monkeypatch, ack):
    from fulcra_collect import macos_keychain
    from types import SimpleNamespace
    proc = SimpleNamespace(pid=12345, returncode=0, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(), wait=lambda **_: None)
    calls = []
    monkeypatch.setattr(macos_keychain.subprocess, 'Popen', lambda *a, **kw: calls.append((a, kw)) or proc)
    monkeypatch.setattr(macos_keychain.os, 'killpg', lambda *a: None)
    monkeypatch.setattr(macos_keychain, '_communicate', lambda p, packet, deadline: json.dumps(ack).encode())
    monkeypatch.setattr(keyring, 'set_password', lambda *a: pytest.fail('interactive fallback'))
    with pytest.raises(keyring.errors.KeyringError) as exc:
        macos_keychain.mutate_trakt_password('receipt', 'PRIVATE')
    assert 'PRIVATE' not in str(exc.value)
    assert len(calls) == 1 and 'PRIVATE' not in repr(calls)


@pytest.mark.parametrize('raises', [False, True])
def test_native_cfdata_and_dictionary_are_released_without_os_calls(monkeypatch, raises):
    from fulcra_collect import macos_keychain
    from types import SimpleNamespace
    api = macos_keychain._Security.__new__(macos_keychain._Security)
    released, created = [], []
    api.cf = object()
    api.constant = lambda _: 10
    api.string = lambda *args: 110
    api.data = lambda _allocator, value, length: created.append((value, length)) or 120
    api.dictionary = lambda *args: 130
    api.release = released.append
    original_byte = macos_keychain.C.c_byte
    monkeypatch.setattr(macos_keychain.C, 'c_byte', SimpleNamespace(in_dll=lambda *args: original_byte()))
    def run():
        with api.query({'kSecAttrService': 'synthetic', 'kSecValueData': b'PRIVATE'}) as query:
            assert query == 130
            if raises:
                raise RuntimeError('synthetic interrupted native operation')
    if raises:
        with pytest.raises(RuntimeError):
            run()
    else:
        run()
    assert created == [(b'PRIVATE', 7)]
    assert released == [130, 110, 120]


def test_trakt_quiet_write_timeout_kills_owned_group_without_retry(monkeypatch):
    from fulcra_collect import macos_keychain
    from types import SimpleNamespace
    actions = []
    proc = SimpleNamespace(pid=12345, returncode=None, stdin=io.BytesIO(), stdout=io.BytesIO(), stderr=io.BytesIO(),
        wait=lambda **kw: actions.append('wait'))
    monkeypatch.setattr(macos_keychain.subprocess, 'Popen', lambda *a, **kw: actions.append('start') or proc)
    monkeypatch.setattr(macos_keychain.os, 'killpg', lambda pid, sig: actions.append(('kill', pid)))
    def timeout(*args):
        raise macos_keychain.subprocess.TimeoutExpired('synthetic', 2, output=b'PRIVATE')
    monkeypatch.setattr(macos_keychain, '_communicate', timeout)
    with pytest.raises(credentials.KeychainAccessRequired) as error:
        macos_keychain.mutate_trakt_password('receipt', 'PRIVATE')
    assert actions == ['start', ('kill', 12345), 'wait']
    assert 'PRIVATE' not in str(error.value)


def test_trakt_active_setup_mutation_is_quiet_and_authorization_is_separate(monkeypatch):
    from fulcra_collect.trakt_source import KeychainStore
    calls = []
    monkeypatch.setattr(credentials, 'authorize_secret', lambda p, k: calls.append(('authorize', p, k)) or True)
    monkeypatch.setattr(credentials, 'set_secret', lambda *a: pytest.fail('interactive active-field write'))
    store = KeychainStore(_quiet_write=lambda k, v: calls.append(('quiet', k, v)))
    assert store.authorize('access_token') is True
    assert calls == [('authorize', 'trakt', 'access_token')]
    store.set('access_token', 'synthetic-value')
    assert calls[-1] == ('quiet', 'access_token', 'synthetic-value')


def test_trakt_explicit_mutation_has_no_unsupported_quiet_backend_fallback(monkeypatch):
    from fulcra_collect.trakt_source import KeychainStore
    monkeypatch.setattr(credentials, '_uses_macos_keychain', lambda: False)
    monkeypatch.setattr(keyring, 'set_password', lambda *a: pytest.fail('interactive fallback'))
    store = KeychainStore()
    store._quiet_write = None  # exercise production unsupported-backend refusal
    with pytest.raises(credentials.KeychainAccessRequired):
        store.set('access_token', 'synthetic-value')
