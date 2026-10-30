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
