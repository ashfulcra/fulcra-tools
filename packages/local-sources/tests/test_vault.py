"""Synthetic Files transport; no CLI credentials or real account access."""
import traceback

import httpx
import pytest

from fulcra_local_sources.vault import VaultWriter, VaultError


def test_upload_is_bound_to_collect_token_and_storage_gets_no_bearer(tmp_path):
    source = tmp_path / 'private-name.md'
    source.write_text('synthetic chosen data')
    calls = []

    def respond(request):
        calls.append(request)
        if request.url.host == 'api.fulcradynamics.com':
            assert request.headers['Authorization'] == 'Bearer synthetic-collect-account-a'
            if request.method == 'GET':
                return httpx.Response(200, json={'userid': 'synthetic-account-a'})
            import json
            metadata = json.loads(request.content)
            assert metadata['name'] == 'private-name.md'
            assert metadata['path'] == '/vault/chosen'
            assert str(tmp_path) not in repr(metadata)
            return httpx.Response(200, json={'file': {'id': 'synthetic-version'},
                                            'url': 'https://storage.example.test/upload'})
        assert request.url.host == 'storage.example.test'
        assert 'Authorization' not in request.headers
        assert request.content == b'synthetic chosen data'
        return httpx.Response(200)

    with VaultWriter('synthetic-collect-account-a', transport=httpx.MockTransport(respond)) as writer:
        assert writer.namespace == 'synthetic-account-a'
        writer.upload_file(source, '/vault/chosen/private-name.md')
    assert [r.method for r in calls] == ['GET', 'POST', 'POST']


@pytest.mark.parametrize('failure', ['timeout', 'status', 'redirect'])
def test_full_error_trace_never_exposes_local_path_token_or_response(tmp_path, failure):
    source = tmp_path / 'private-name.md'
    source.write_text('synthetic')

    def respond(request):
        if failure == 'timeout':
            raise httpx.ReadTimeout(f'secret token and local {source}')
        if failure == 'redirect':
            return httpx.Response(302, headers={'Location': 'https://other.example.test'})
        return httpx.Response(400, text=f'secret token and local {source}')

    with VaultWriter('synthetic-token', transport=httpx.MockTransport(respond)) as writer:
        with pytest.raises(VaultError):
            try:
                writer.upload_file(source, '/vault/chosen/private-name.md')
            except VaultError:
                detail = traceback.format_exc()
                assert str(source) not in detail
                assert 'secret token and local' not in detail
                assert 'synthetic-token' not in detail
                raise


def test_storage_redirect_and_invalid_urls_are_not_followed(tmp_path):
    source = tmp_path / 'brief.md'
    source.write_text('synthetic')
    calls = []

    def respond(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(200, json={'file': {'id': 'synthetic-version'},
                                            'url': 'http://storage.example.test/upload'})
        pytest.fail('must not send to an insecure storage URL')

    with VaultWriter('synthetic', transport=httpx.MockTransport(respond)) as writer:
        with pytest.raises(VaultError, match='secure URL'):
            writer.upload_file(source, '/vault/chosen/brief.md')
    assert len(calls) == 1
