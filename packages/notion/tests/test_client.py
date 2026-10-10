import json
import traceback
from dataclasses import replace

import httpx
import pytest

from fulcra_notion.client import Limits, NotionClient, NotionError, normalize_page_ids
from fulcra_notion.render import render_snapshot

PAGE = '00000000-0000-4000-8000-000000000001'
BLOCK = '00000000-0000-4000-8000-000000000002'
CHILD = '00000000-0000-4000-8000-000000000003'


def text(value):
    return [{'type': 'text', 'plain_text': value, 'text': {'content': value}}]


def block(identifier, parent, kind='paragraph', children=False):
    return {'object': 'block', 'id': identifier, 'type': kind, 'has_children': children,
            'parent': {'type': 'page_id' if parent == PAGE else 'block_id',
                       'page_id' if parent == PAGE else 'block_id': parent},
            kind: {'rich_text': text('Synthetic content')}}


def page():
    return {'object': 'page', 'id': PAGE, 'properties': {
        'Name': {'type': 'title', 'title': text('Synthetic page')}}}


def listing(items, more=False, cursor=None):
    return {'object': 'list', 'results': items, 'has_more': more, 'next_cursor': cursor}


def client(handler, **kwargs):
    return NotionClient('synthetic-token', transport=httpx.MockTransport(handler),
                        sleep=lambda seconds: None, **kwargs)


@pytest.mark.parametrize('value', [PAGE, PAGE.replace('-', ''),
    f'https://www.notion.so/workspace/Synthetic-{PAGE.replace("-", "")}?pvs=4',
    f'https://app.notion.com/p/{PAGE}', f'https://demo.notion.site/{PAGE}'])
def test_only_supported_page_identifiers_are_selected(value):
    assert normalize_page_ids(value + '\n' + PAGE) == (PAGE,)


@pytest.mark.parametrize('value', ['', [], 'arbitrary', 'https://evil.example/' + PAGE,
    'https://www.notion.so.evil.example/' + PAGE, 'http://www.notion.so/' + PAGE,
    'https://' + 'user:secret' + '@' + 'www.notion.so/' + PAGE])
def test_invalid_selection_fails_without_echoing_input(value):
    with pytest.raises(NotionError) as error:
        normalize_page_ids(value)
    assert str(value) not in str(error.value) or value == ''


def test_reads_selected_page_nested_blocks_and_pagination_only():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.method == 'GET'
        assert request.url.host == 'api.notion.com'
        assert request.headers['Notion-Version'] == '2026-03-11'
        assert request.headers['Authorization'] == 'Bearer synthetic-token'
        if request.url.path == '/v1/pages/' + PAGE:
            return httpx.Response(200, json=page())
        if request.url.path == '/v1/blocks/' + BLOCK + '/children':
            return httpx.Response(200, json=listing([block(CHILD, BLOCK)]))
        assert request.url.path == '/v1/blocks/' + PAGE + '/children'
        if 'start_cursor' in request.url.params:
            assert request.url.params['start_cursor'] == 'opaque / cursor?'
            return httpx.Response(200, json=listing([]))
        return httpx.Response(200, json=listing([block(BLOCK, PAGE, children=True)],
                                                True, 'opaque / cursor?'))
    with client(handler) as api:
        snap = api.snapshot(PAGE)
    assert snap.page_id == PAGE and snap.title == 'Synthetic page'
    assert snap.blocks[0]['_children'][0]['id'] == CHILD
    assert len(requests) == 4
    assert render_snapshot(snap).count('Synthetic content') == 2


@pytest.mark.parametrize('kind', ['child_page', 'child_database', 'link_to_page',
                                  'synced_block', 'image', 'file', 'embed'])
def test_external_references_are_marked_and_never_followed(kind):
    requests = []
    def handler(request):
        requests.append(request.url.path)
        if request.url.path.startswith('/v1/pages/'):
            return httpx.Response(200, json=page())
        item = block(BLOCK, PAGE, kind, children=True)
        item[kind]['url'] = 'https://files.example/private?secret=synthetic'
        return httpx.Response(200, json=listing([item]))
    with client(handler) as api:
        content = render_snapshot(api.snapshot(PAGE))
    assert len(requests) == 2 and 'omitted' in content.lower()
    assert 'secret=synthetic' not in content and 'files.example' not in content


@pytest.mark.parametrize('fault', ['cursor', 'duplicate', 'parent', 'page-id', 'child-id'])
def test_malformed_or_looping_responses_fail_closed(fault):
    def handler(request):
        if request.url.path.startswith('/v1/pages/'):
            result = page()
            if fault == 'page-id':
                result['id'] = BLOCK
            return httpx.Response(200, json=result)
        item = block(BLOCK, PAGE)
        if fault == 'parent':
            item['parent'] = {'type': 'page_id', 'page_id': CHILD}
        if fault == 'child-id':
            item['id'] = '../../unselected'
        return httpx.Response(200, json=listing([item, item] if fault == 'duplicate' else [item],
                                                fault == 'cursor', 'repeated'))
    with client(handler) as api, pytest.raises(NotionError):
        api.snapshot(PAGE)


@pytest.mark.parametrize('limit', ['max_response_bytes', 'max_total_bytes', 'max_requests',
                                   'max_page_blocks', 'max_run_blocks', 'max_depth'])
def test_limits_abort_without_a_partial_snapshot(limit):
    limits = replace(Limits(), **{limit: 0})
    def handler(request):
        return httpx.Response(200, json=page() if request.url.path.startswith('/v1/pages/')
                              else listing([block(BLOCK, PAGE, children=True)]))
    with client(handler, limits=limits) as api, pytest.raises(NotionError):
        api.snapshot(PAGE)


@pytest.mark.parametrize('status', [302, 401, 403, 404, 429, 500])
def test_provider_errors_and_redirects_do_not_echo_secrets(status):
    with client(lambda req: httpx.Response(status, text='private-provider-response',
                 headers={'Location': 'https://evil.example'})) as api:
        with pytest.raises(NotionError) as error:
            api.snapshot(PAGE)
    assert 'private-provider-response' not in str(error.value)


def test_transport_exception_traceback_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout('secret and private path', request=request)
    with client(handler) as api:
        try:
            api.snapshot(PAGE)
        except NotionError:
            trace = traceback.format_exc()
        else:
            pytest.fail('transport failure was swallowed')
    assert 'secret and private path' not in trace


def test_selection_limit_and_missing_pages_are_not_silently_skipped():
    ids = '\n'.join(f'00000000-0000-4000-8000-{n:012d}' for n in range(51))
    with pytest.raises(NotionError):
        normalize_page_ids(ids)
    with client(lambda req: httpx.Response(200, content=json.dumps([]))) as api:
        with pytest.raises(NotionError):
            api.snapshot(PAGE)


def test_invalid_url_port_and_host_are_sanitized():
    for value in [f'https://www.notion.so:private-value/{PAGE}',
                  f'https://[malformed-host/{PAGE}']:
        with pytest.raises(NotionError) as error:
            normalize_page_ids(value)
        assert 'private-value' not in str(error.value)


def test_archived_selected_page_never_reads_its_blocks():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        result = page()
        result['in_trash'] = True
        return httpx.Response(200, json=result)
    with client(handler) as api, pytest.raises(NotionError):
        api.snapshot(PAGE)
    assert calls == ['/v1/pages/' + PAGE]


def test_invalid_credentials_are_rejected_without_echoing_them():
    for token in ['', 'private-token\nheader', 'private-token-π']:
        with pytest.raises(NotionError) as error:
            NotionClient(token)
        assert 'private-token' not in str(error.value)
