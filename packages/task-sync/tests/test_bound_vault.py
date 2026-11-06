"""Collect injects a captured Files transport; the standalone engine stays portable."""
import base64
import json
import logging

import httpx
import pytest

from fulcra_collect import config
from fulcra_collect.files_io import CollectFiles, FilesIOError
from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_task_sync.vault import FulcraVault, VaultError

ACCOUNT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'
VERSION = '11111111-2222-4333-8444-555555555555'
REMOTE = '/vault/tasks/synthetic/one.md'


def token(account=ACCOUNT):
    raw = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': account, 'exp': 4102444800}).encode()).decode().rstrip('=')
    return 'synthetic.' + raw + '.signature'


def context(tmp_path, monkeypatch):
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path))
    cfg = config.load()
    cfg.enable('synthetic-task')
    config.save(cfg)
    cfg = config.load()
    ctx = RunContext(plugin_id='synthetic-task', config={}, config_epoch=cfg.plugin_epochs['synthetic-task'],
        credentials={}, state=PluginState('synthetic-task'), log=logging.getLogger('synthetic'), _emit=lambda _: None)
    auth = [token()]
    monkeypatch.setattr(ctx, 'collect_token', lambda: auth[0])
    return ctx, auth


def history(**over):
    return {'id': VERSION, 'path': '/vault/tasks/synthetic', 'name': 'one.md',
        'state': 'uploaded', 'uploaded_at': '2026-10-23T12:00:00Z', **over}


def test_bound_vault_uses_fresh_collect_account_without_a_separate_token(tmp_path, monkeypatch):
    ctx, auth = context(tmp_path, monkeypatch)
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['authorization'] == 'Bearer ' + auth[0]
        assert request.url.params['fulcra_userid'] == ACCOUNT
        return httpx.Response(200, json={'files': [history()]})
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session, CollectFiles(session) as files, FulcraVault(bound_files=files) as vault:
        assert vault.namespace == ACCOUNT
        auth[0] = token() + '-synthetic-renewed-signature'
        assert vault.versions(REMOTE) == [VERSION]
        auth[0] = token(OTHER)
        with pytest.raises(RuntimeError, match='changed'):
            vault.versions(REMOTE)
    assert len(calls) == 1


@pytest.mark.parametrize('row', [history(path='/different'), history(name='different.md'), history(fulcra_userid=OTHER), history(id='not-a-uuid')])
def test_bound_history_rejects_wrong_identity_before_returning_rows(tmp_path, monkeypatch, row):
    ctx, _ = context(tmp_path, monkeypatch)
    with CollectRecordSession(ctx, transport=httpx.MockTransport(lambda _:httpx.Response(200, json={'files': [row]}))) as session, CollectFiles(session) as files, FulcraVault(bound_files=files) as vault:
        with pytest.raises(VaultError):
            vault.versions(REMOTE)


@pytest.mark.parametrize('body', [
    {'files': [history(), history(id='11111111-2222-4333-8444-666666666666')]},
    {'files': [history(uploaded_at='not-a-timestamp')]},
    {'files': [], 'continuation_token': 'synthetic-next'},
])
def test_bound_history_ambiguity_is_unknown(tmp_path, monkeypatch, body):
    ctx, _ = context(tmp_path, monkeypatch)
    with CollectRecordSession(ctx, transport=httpx.MockTransport(lambda _:httpx.Response(200, json=body))) as session, CollectFiles(session) as files, FulcraVault(bound_files=files) as vault:
        with pytest.raises(VaultError):
            vault.versions(REMOTE)


def test_bound_upload_validates_version_before_storage(tmp_path, monkeypatch):
    ctx, _ = context(tmp_path, monkeypatch)
    api, storage = [], []
    def handler(request):
        api.append(request)
        return httpx.Response(200, json={'file': {'id': 'not-a-uuid'}, 'url': 'https://storage.example/upload'})
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session, CollectFiles(session, storage_transport=httpx.MockTransport(lambda r:storage.append(r) or httpx.Response(200))) as files, FulcraVault(bound_files=files) as vault:
        with pytest.raises(VaultError):
            vault.write(REMOTE, 'Synthetic task')
    assert len(api) == 1 and not storage


def test_large_history_does_not_reread_keychain_for_each_metadata_row(tmp_path, monkeypatch):
    from uuid import UUID
    from unittest.mock import Mock
    ctx, _ = context(tmp_path, monkeypatch)
    ctx.collect_token = Mock(wraps=ctx.collect_token)
    rows = [history(id=str(UUID(int=i + 1000)), state='uploaded' if i == 0 else 'archived') for i in range(500)]
    with CollectRecordSession(ctx, transport=httpx.MockTransport(lambda _:httpx.Response(200, json={'files': rows}))) as session, CollectFiles(session) as files, FulcraVault(bound_files=files) as vault:
        before = ctx.collect_token.call_count
        assert len(vault.versions(REMOTE)) == 500
        assert ctx.collect_token.call_count - before <= 20


@pytest.mark.parametrize("status,headers", [(206, {}), (200, {"Content-Range": "items 0-0/2"}),
                                            (200, {"X-Total-Count": "1"})])
def test_bound_history_partial_http_is_unknown_before_followon_write(tmp_path, monkeypatch, status, headers):
    ctx, _ = context(tmp_path, monkeypatch)
    calls = []
    def handle(request):
        calls.append(request.method)
        return httpx.Response(status, json={"files": []}, headers=headers)
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handle)) as session, CollectFiles(session) as files, FulcraVault(bound_files=files) as vault:
        with pytest.raises((VaultError, FilesIOError)):
            vault.versions(REMOTE)
    assert calls == ["GET"]


@pytest.mark.parametrize('signed', [False, True])
@pytest.mark.parametrize('short', [False, True])
def test_real_task_history_source_update_preserves_user_suffix_or_holds(tmp_path, monkeypatch, signed, short):
    from copy import deepcopy
    from dataclasses import replace
    from types import SimpleNamespace
    from fulcra_task_sync.documents import render, task_path
    from fulcra_task_sync.models import Collection, Task
    from fulcra_task_sync.engine import run_sync
    task = Task('synthetic-task', 'synthetic-list', 'Updated source title', 'source body', False, revision='r2')
    prefix = render('synthetic', replace(task, title='Original source title', revision='r1'), 'synthetic-generation')
    suffix = '\nUser-authored suffix must survive. café ☃\n'
    complete = prefix + suffix
    body = (prefix if short else complete).encode()
    ctx, _ = context(tmp_path, monkeypatch)
    calls, writes, state = [], [], {}
    def handle(request):
        calls.append(request)
        if request.url.host == 'storage.example' or request.url.path.endswith('/download'):
            if signed and request.url.host != 'storage.example':
                return httpx.Response(302, headers={'location': 'https://storage.example/task'})
            return httpx.Response(200, content=body, headers={'Content-Length': str(len(body))})
        return httpx.Response(200, json={'files': [history(path=request.url.params['path'],
            name=request.url.params['name'], size=len(complete.encode()))]})
    provider = SimpleNamespace(name='synthetic', namespace='synthetic-source',
        collections=lambda: [Collection('synthetic-list', 'Synthetic list')],
        tasks=lambda selected: [task], get_task=lambda _: task,
        complete=lambda *_: pytest.fail('source completion forbidden'))
    transport = httpx.MockTransport(handle)
    with CollectRecordSession(ctx, transport=transport) as session, CollectFiles(session, storage_transport=transport) as files, FulcraVault(bound_files=files) as vault:
        # Capture the real engine's proposed file write; no real upload/source mutation.
        vault.write = lambda path, text: writes.append((path, text)) or '11111111-2222-4333-8444-666666666666'
        result = run_sync(provider, {'synthetic-list'}, vault,
            lambda key: deepcopy(state.get(key)), lambda key, value: state.__setitem__(key, deepcopy(value)))
    if short:
        assert result.errors and writes == []
        assert VERSION not in json.dumps(state)  # failed bytes never become seen version authority
    else:
        assert not result.errors and len(writes) == 1
        assert writes[0][0] == task_path('synthetic', task.id) and suffix in writes[0][1]
    assert calls and all(request.method == 'GET' for request in calls)


@pytest.mark.parametrize('size', [-1, True, False, 3.0, '3', None])
def test_supplied_version_size_must_be_strict_before_history_publication(tmp_path, monkeypatch, size):
    ctx, _ = context(tmp_path, monkeypatch)
    with CollectRecordSession(ctx, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'files': [history(size=size)]}))) as session, CollectFiles(session) as files, FulcraVault(bound_files=files) as vault:
        with pytest.raises(VaultError):
            vault.versions(REMOTE)


def test_failed_history_invalidates_old_exact_version_metadata(tmp_path, monkeypatch):
    ctx, _ = context(tmp_path, monkeypatch)
    payload = [{'files': [history(size=3)]}]
    with CollectRecordSession(ctx, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload[0]))) as session, CollectFiles(session) as files, FulcraVault(bound_files=files) as vault:
        assert vault.versions(REMOTE) == [VERSION]
        payload[0] = {'files': [], 'has_more': True}
        with pytest.raises(VaultError):
            vault.versions(REMOTE)
        with pytest.raises(VaultError, match='version metadata'):
            vault.read_version(VERSION)


@pytest.mark.parametrize('signed', [False, True])
def test_exact_archived_version_keeps_its_own_size_not_current_path_size(tmp_path, monkeypatch, signed):
    archived = '11111111-2222-4333-8444-666666666666'
    bodies = {VERSION: b'current', archived: 'older café body'.encode()}
    ctx, _ = context(tmp_path, monkeypatch)
    def handle(request):
        if request.url.host == 'storage.example':
            return httpx.Response(200, content=bodies[request.url.path.lstrip('/')])
        if request.url.path.endswith('/download'):
            identifier = request.url.path.split('/')[-2]
            return (httpx.Response(302, headers={'location': 'https://storage.example/' + identifier}) if signed
                    else httpx.Response(200, content=bodies[identifier]))
        return httpx.Response(200, json={'files': [history(size=len(bodies[VERSION])),
            history(id=archived, state='archived', size=len(bodies[archived]))]})
    transport = httpx.MockTransport(handle)
    with CollectRecordSession(ctx, transport=transport) as session, CollectFiles(session, storage_transport=transport) as files, FulcraVault(bound_files=files) as vault:
        assert vault.versions(REMOTE) == [VERSION, archived]
        assert vault.read_version(archived) == bodies[archived].decode()
        assert vault.read_version(VERSION) == bodies[VERSION].decode()
