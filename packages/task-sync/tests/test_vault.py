import json
import httpx
import pytest


@pytest.mark.parametrize('path', ['vault/tasks/todoist/a.md', '/vault/tasks/todoist/a.md'])
def test_history_contract_and_signed_upload_never_forward_auth(path):
    from fulcra_task_sync.vault import FulcraVault
    calls = []
    def handle(request):
        calls.append(request)
        if request.url.host == 'storage.example':
            assert 'authorization' not in request.headers
            return httpx.Response(200, text='body' if request.method == 'GET' else '')
        assert request.headers['authorization'] == 'Bearer synthetic-token'
        if request.url.path.endswith('/info'):
            return httpx.Response(200, json={'userid': 'synthetic-user'})
        if request.method == 'POST':
            assert json.loads(request.content)['path'] == '/vault/tasks/todoist'
            return httpx.Response(200, json={'url': 'https://storage.example/upload', 'file': {'id': 'v2'}})
        if request.url.path.endswith('/download'):
            return httpx.Response(302, headers={'location': 'https://storage.example/download'})
        assert request.url.params['state'] == 'uploaded,archived'
        assert request.url.params['path'] == '/vault/tasks/todoist'
        assert request.url.params['name'] == 'a.md'
        return httpx.Response(200, json={'files': [
            {'id': 'v1', 'state': 'uploaded', 'uploaded_at': '2026-01-01T00:00:00Z'}]})
    with FulcraVault('synthetic-token', transport=httpx.MockTransport(handle)) as vault:
        assert vault.namespace == 'synthetic-user'
        assert vault.versions(path) == ['v1']
        assert vault.read_version('v1') == 'body'
        assert vault.write(path, 'synthetic body') == 'v2'
    assert len(calls) == 6


def test_version_cap_and_malformed_listing_fail_closed():
    from fulcra_task_sync.vault import FulcraVault, VaultError
    for payload in ({}, {'files': [{'id': 'v1'}] * 3}, {'files': [], 'next_cursor': 'more'}):
        with FulcraVault('synthetic-token', namespace='synthetic-user', max_versions=2,
                         transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as vault:
            with pytest.raises(VaultError):
                vault.versions('vault/tasks/todoist/a.md')


def test_download_and_upload_reject_insecure_signed_urls():
    from fulcra_task_sync.vault import FulcraVault, VaultError
    with FulcraVault('synthetic-token', namespace='synthetic-user',
                     transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={'location': 'http://storage.example/x'}))) as vault:
        with pytest.raises(VaultError):
            vault.read_version('v1')
