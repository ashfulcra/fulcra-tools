"""Synthetic raw HTTP export retention; never a provider-coverage assertion."""
import hashlib
import gzip
import json

import httpx
import pytest

from test_migration_ledger import ACCOUNT, START, MID, END, plan, raw, uid


def response(status, *, content, headers=None):
    return httpx.Response(status, stream=Chunks([content]), headers=headers)


class Client:
    def __init__(self, handler, *, base_url='https://api.fulcradynamics.com'):
        self.http = httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))
        self.headers_calls = 0

    def _client(self):
        return self.http

    def _authed_headers(self):
        self.headers_calls += 1
        return {'Authorization': 'Bearer synthetic-token'}


def reader(tmp_path, handler, *, guard=lambda: ACCOUNT, **kwargs):
    from fulcra_collect.migration_export import HistoricalReader
    client = Client(handler)
    return HistoricalReader(plan(), client=client, guard=guard, artifacts=tmp_path / 'artifacts',
                            pass_id=uid(20), **kwargs), client


def fetch(value, **kwargs):
    return value.read(uid(10), start_time=START, end_time=END, order='asc', **kwargs)


def test_raw_body_and_scope_are_private_durable_before_parsing(tmp_path):
    body = json.dumps([raw()]).encode()
    requests = []
    def handle(request):
        attempts = list((tmp_path / 'artifacts' / uid(20)).glob('*/request.json'))
        assert len(attempts) == 1  # durable intent precedes GET
        request_doc = json.loads(attempts[0].read_bytes())
        assert request_doc['window_id'] == uid(10)
        assert 'synthetic-token' not in attempts[0].read_text()
        requests.append(request)
        return response(200, content=body)
    export, client = reader(tmp_path, handle)
    result = fetch(export)
    assert result.records == [raw()] and result.coverage_complete is False
    result.records[0]['note'] = 'changed returned value'
    assert result.records == [raw()]
    assert result.body_path.read_bytes() == body
    receipt = json.loads(result.receipt_path.read_bytes())
    assert receipt['body_sha256'] == hashlib.sha256(body).hexdigest()
    assert receipt['bytes'] == len(body) and receipt['transport_eof'] is True
    assert receipt['coverage_complete'] is False
    for path in (tmp_path / 'artifacts').rglob('*'):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    assert client.headers_calls == 1
    assert requests[0].url.path == '/data/v1alpha1/event/DurationAnnotation/' + uid(2)
    assert dict(requests[0].url.params) == {'start_time': START, 'end_time': END,
        'fulcra_userid': ACCOUNT, 'format': 'json', 'sort': 'asc'}


@pytest.mark.parametrize('body', [b'{', b'{"data":[]}', b'{"records":[],"next_cursor":"more"}',
    b'[null]', b'[{"id":"one","id":"two"}]', b'[{"value":NaN}]',
    b'[{"value":1.00000000000000001}]', b'[{"note":"\\ud800"}]'])
def test_malformed_ambiguous_or_lossy_response_retained_but_never_clean_empty(tmp_path, body):
    export, _ = reader(tmp_path, lambda request: response(200, content=body))
    with pytest.raises(RuntimeError, match='unreadable|unsupported|lossless'):
        fetch(export)
    assert next((tmp_path / 'artifacts').rglob('body.json')).read_bytes() == body
    assert len(list((tmp_path / 'artifacts').rglob('receipt.json'))) == 1


@pytest.mark.parametrize('status', [206, 301, 401, 429, 500])
def test_unexpected_status_preserves_response_without_following_redirects(tmp_path, status):
    calls = []
    export, _ = reader(tmp_path, lambda request: calls.append(request) or response(
        status, content=b'[]', headers={'Location': 'https://example.com/private'}))
    with pytest.raises(RuntimeError, match='HTTP|status'):
        fetch(export)
    assert len(calls) == 1
    assert next((tmp_path / 'artifacts').rglob('body.json')).read_bytes() == b'[]'


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks

    def __iter__(self):
        yield from self.chunks


def test_oversized_body_retains_bounded_prefix_without_complete_receipt(tmp_path):
    export, _ = reader(tmp_path, lambda request: httpx.Response(200, stream=Chunks([b'1234', b'56789'])),
                       max_bytes=8)
    with pytest.raises(RuntimeError, match='bound'):
        fetch(export)
    assert next((tmp_path / 'artifacts').rglob('body.json')).read_bytes() == b'12345678'
    assert not list((tmp_path / 'artifacts').rglob('receipt.json'))


def test_interrupted_stream_keeps_partial_artifact_and_retry_gets_new_attempt(tmp_path):
    def chunks():
        yield b'['
        raise httpx.ReadError('private transport detail must not escape')
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, stream=Chunks(chunks())) if len(calls) == 1 else response(200, content=b'[]')
    export, _ = reader(tmp_path, handle)
    with pytest.raises(RuntimeError, match='transport.*unknown') as error:
        fetch(export)
    assert 'private transport detail' not in str(error.value)
    result = fetch(export)
    assert result.records == [] and result.coverage_complete is False
    bodies = list((tmp_path / 'artifacts').rglob('body.json'))
    assert len(bodies) == 2 and sorted(path.read_bytes() for path in bodies) == [b'[', b'[]']
    assert len(list((tmp_path / 'artifacts').rglob('receipt.json'))) == 1


def test_account_change_during_stream_retains_prefix_but_cannot_complete(tmp_path):
    account = [ACCOUNT]
    def chunks():
        yield b'['
        account[0] = uid(99)
        yield b']'
    export, _ = reader(tmp_path, lambda request: httpx.Response(200, stream=Chunks(chunks())), guard=lambda: account[0])
    with pytest.raises(ValueError, match='account|scope'):
        fetch(export)
    assert not list((tmp_path / 'artifacts').rglob('receipt.json'))


def test_scope_rechecked_after_headers_before_dispatch(tmp_path):
    account, calls = [ACCOUNT], []
    export, client = reader(tmp_path, lambda request: calls.append(request) or response(200, content=b'[]'),
                            guard=lambda: account[0])
    def headers():
        account[0] = uid(99)
        return {'Authorization': 'Bearer synthetic-token'}
    client._authed_headers = headers
    with pytest.raises(ValueError, match='account|scope'):
        fetch(export)
    assert not calls


@pytest.mark.parametrize('change', [{'start_time': MID, 'end_time': START},
    {'start_time': '2019-01-01T00:00:00Z'}, {'end_time': '2021-01-01T00:00:00Z'},
    {'order': 'other'}, {'start_time': '2020-01-01T00:00:00.000000001Z'}])
def test_invalid_window_refuses_before_any_credentials_or_http(tmp_path, change):
    calls = []
    export, client = reader(tmp_path, lambda request: calls.append(request))
    options = {'start_time': START, 'end_time': END, 'order': 'asc', **change}
    with pytest.raises(ValueError):
        export.read(uid(10), **options)
    assert not calls and client.headers_calls == 0


def test_export_pass_cannot_be_rebound_to_changed_plan(tmp_path):
    from fulcra_collect.migration_export import HistoricalReader
    export, client = reader(tmp_path, lambda request: response(200, content=b'[]'))
    fetch(export)
    with pytest.raises(ValueError, match='pass|scope'):
        HistoricalReader(plan(destination_data_type='Event/' + uid(77)), client=client,
            guard=lambda: ACCOUNT, artifacts=tmp_path / 'artifacts', pass_id=uid(20))


def test_private_root_symlink_and_broad_permissions_refused(tmp_path):
    real = tmp_path / 'elsewhere'
    real.mkdir(mode=0o700)
    (tmp_path / 'artifacts').symlink_to(real, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        reader(tmp_path, lambda request: response(200, content=b'[]'))
    (tmp_path / 'artifacts').unlink()
    (tmp_path / 'artifacts').mkdir(mode=0o755)
    with pytest.raises(ValueError, match='private'):
        reader(tmp_path, lambda request: response(200, content=b'[]'))
    assert not list((tmp_path / 'artifacts').iterdir())


def test_untrusted_api_origin_refused_before_credentials(tmp_path):
    export, client = reader(tmp_path, lambda request: response(200, content=b'[]'))
    client.http.base_url = httpx.URL('https://example.com')
    with pytest.raises(ValueError, match='origin'):
        fetch(export)
    assert client.headers_calls == 0


def test_origin_change_during_header_refresh_refused_before_dispatch(tmp_path):
    calls = []
    export, client = reader(tmp_path, lambda request: calls.append(request) or response(200, content=b'[]'))
    def headers():
        client.http.base_url = httpx.URL('https://example.com')
        return {'Authorization': 'Bearer synthetic-token'}
    client._authed_headers = headers
    with pytest.raises(ValueError, match='origin'):
        fetch(export)
    assert not calls


def test_deadline_is_bounded_across_whole_body(tmp_path):
    clock = [0.0]
    def chunks():
        yield b'['
        clock[0] = 31.0
        yield b']'
    export, _ = reader(tmp_path, lambda request: httpx.Response(200, stream=Chunks(chunks())),
                       clock=lambda: clock[0], timeout=30)
    with pytest.raises(RuntimeError, match='deadline|bound'):
        fetch(export)
    assert not list((tmp_path / 'artifacts').rglob('receipt.json'))


def test_compressed_body_retained_without_inflating_past_read_bound(tmp_path, monkeypatch):
    import httpx._decoders
    body = gzip.compress(b' ' * (8 * 1024 * 1024))
    def forbidden(*args, **kwargs):
        pytest.fail('compressed HTTP bytes must not be decoded before the bound')
    monkeypatch.setattr(httpx._decoders.GZipDecoder, 'decode', forbidden)
    export, _ = reader(tmp_path, lambda request: response(200, content=body,
        headers={'Content-Encoding': 'gzip'}), max_bytes=1024 * 1024)
    with pytest.raises(RuntimeError, match='encoding|compressed'):
        fetch(export)
    assert next((tmp_path / 'artifacts').rglob('body.json')).read_bytes() == body
    receipt = json.loads(next((tmp_path / 'artifacts').rglob('receipt.json')).read_bytes())
    assert receipt['decoded_body'] is False and receipt['body_encoding'] == 'gzip'
    assert receipt['coverage_complete'] is False


def test_zero_output_compressed_chunks_cannot_bypass_deadline(tmp_path):
    clock, visited = [0.0], []
    def chunks():
        for now in (0, 31, 62):
            clock[0] = now
            visited.append(now)
            yield gzip.compress(b'')
    export, _ = reader(tmp_path, lambda request: httpx.Response(200, stream=Chunks(chunks()),
        headers={'Content-Encoding': 'gzip'}), clock=lambda: clock[0], timeout=30)
    with pytest.raises(RuntimeError, match='deadline'):
        fetch(export)
    assert visited == [0, 31]
    assert not list((tmp_path / 'artifacts').rglob('receipt.json'))


def test_saved_body_must_match_eof_receipt_before_parse(tmp_path, monkeypatch):
    import fulcra_collect.migration_export as module
    write = module._write
    def corrupt(parent, name, value):
        result = write(parent, name, value)
        if name == 'receipt.json':
            next((tmp_path / 'artifacts').rglob('body.json')).write_bytes(b'[{"note":"substituted"}]')
        return result
    monkeypatch.setattr(module, '_write', corrupt)
    export, _ = reader(tmp_path, lambda request: response(200, content=b'[]'))
    with pytest.raises(RuntimeError, match='integrity|hash|changed'):
        fetch(export)


@pytest.mark.parametrize('body', [b'[{},]', b'[] false', b'[{} {}]', b'[' + b'{},' * 4000 + b'{}]',
    b'[{"note":"' + b'x' * (1024 * 1024) + b'"}]'],
    ids=['trailing-comma', 'trailing-data', 'missing-comma', 'too-many-rows', 'oversized-row'])
def test_json_row_parser_limits_before_materializing_unbounded_objects(tmp_path, body):
    export, _ = reader(tmp_path, lambda request: response(200, content=body))
    with pytest.raises(RuntimeError, match='JSON|bound'):
        fetch(export)
    assert next((tmp_path / 'artifacts').rglob('body.json')).read_bytes() == body


def test_json_array_whitespace_empty_and_record_boundaries_are_preserved(tmp_path):
    body = b' \r\n[\t{"note":"a } , [ \\"quote\\"", "value":1.5},\n{"value":2e2}] \n'
    export, _ = reader(tmp_path, lambda request: response(200, content=body))
    assert fetch(export).records == [{'note': 'a } , [ "quote"', 'value': 1.5}, {'value': 200.0}]


def test_json_total_canonical_snapshot_bound_and_nesting_are_loud(tmp_path, monkeypatch):
    import fulcra_collect.migration_export as module
    monkeypatch.setattr(module, '_MAX_SNAPSHOT', 64)
    body = json.dumps([{'note': 'x' * 30}, {'note': 'y' * 30}]).encode()
    export, _ = reader(tmp_path, lambda request: response(200, content=body))
    with pytest.raises(RuntimeError, match='JSON'):
        fetch(export)
    assert next((tmp_path / 'artifacts').rglob('body.json')).read_bytes() == body
    body = b'[' + b'{"nested":' * 65 + b'null' + b'}' * 65 + b']'
    export.client.http = httpx.Client(base_url='https://api.fulcradynamics.com',
        transport=httpx.MockTransport(lambda request: response(200, content=body)))
    with pytest.raises(RuntimeError, match='JSON'):
        fetch(export)


def test_row_scanner_preserves_arbitrary_nested_json_and_multiplicity():
    from fulcra_collect.migration_export import _records
    rows = [{'text': '🦉 \\" } ] { [ , null', 'nested': {'items': [None, False, 0, 1.5, {'n': i}]},
             'unicode': '\u2028\u2029', 'empty': {}} for i in range(50)]
    rows += rows[:2]
    assert json.loads(_records(json.dumps(rows, ensure_ascii=False).encode())) == rows


@pytest.mark.parametrize("headers", [
    {"X-Next-Cursor": "synthetic-secret-cursor"}, {"Content-Range": "items 0-0/2"},
    {"Link": "https://example.test/private?token=synthetic"}, {"X-Has-More": "false"},
    {"X-Total-Count": "1"}, {"X-Total": "-1"}, {"X-Total-Count": "unknown"},
    {"X-Total-Count": "0, 0"}, {"Content-Length": "3"},
])
def test_http_incompleteness_cannot_become_a_raw_export(tmp_path, headers):
    export, _ = reader(tmp_path, lambda request: response(200, content=b"[]", headers=headers))
    with pytest.raises(RuntimeError, match="complete|metadata|total"):
        fetch(export)
    assert next((tmp_path / "artifacts").rglob("body.json")).read_bytes() == b"[]"
    receipt = next((tmp_path / "artifacts").rglob("receipt.json")).read_bytes()
    assert len(receipt) <= 4096 and b"synthetic-secret" not in receipt and b"example.test" not in receipt


def test_response_facts_are_bounded_nonsecret_and_bound_to_verification(tmp_path):
    headers = {"X-Total-Count": "0", "X-Total": "0", "Content-Length": "2",
               "Set-Cookie": "synthetic-secret", "X-Private": "synthetic-secret"}
    export, _ = reader(tmp_path, lambda request: response(200, content=b"[]", headers=headers))
    result = fetch(export)
    receipt_bytes = result.receipt_path.read_bytes()
    receipt = json.loads(receipt_bytes)
    assert receipt["response_metadata"] == {"partial_headers": [], "total_counts": [0, 0], "content_length": 2, "response_account_id": None}
    proof = export.verify_result(result, uid(10), start_time=START, end_time=END, order="asc")
    assert proof["receipt_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
    assert len(receipt_bytes) <= 4096 and b"synthetic-secret" not in receipt_bytes


@pytest.mark.parametrize("mutation", ["strip", "replace", "legacy"])
def test_response_metadata_cannot_be_removed_or_replaced_before_consumption(tmp_path, mutation):
    export, _ = reader(tmp_path, lambda request: response(200, content=b"[]", headers={"X-Total-Count": "0"}))
    result = fetch(export)
    receipt = json.loads(result.receipt_path.read_bytes())
    if mutation in {"strip", "legacy"}:
        receipt.pop("response_metadata", None)
    else:
        receipt["response_metadata"] = {"partial_headers": [], "total_counts": [None, None], "content_length": None}
    if mutation == "legacy":
        receipt["version"] = 1
    result.receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(RuntimeError, match="integrity|scope"):
        export.verify_result(result, uid(10), start_time=START, end_time=END, order="asc")


@pytest.mark.parametrize("metadata", [
    {"partial_headers": ["x-next-page"], "total_counts": [None, None], "content_length": None},
    {"partial_headers": [], "total_counts": [1, None], "content_length": None},
    {"partial_headers": [], "total_counts": [None, None], "content_length": 3},
])
def test_artifact_verifier_rechecks_completeness_even_with_matching_receipt_hash(tmp_path, metadata):
    from dataclasses import replace
    export, _ = reader(tmp_path, lambda request: response(200, content=b"[]"))
    result = fetch(export)
    receipt = json.loads(result.receipt_path.read_bytes())
    receipt["response_metadata"] = metadata
    result.receipt_path.write_text(json.dumps(receipt))
    rebound = replace(result, _receipt_sha256=hashlib.sha256(result.receipt_path.read_bytes()).hexdigest())
    with pytest.raises(RuntimeError, match="integrity|scope"):
        export.verify_result(rebound, uid(10), start_time=START, end_time=END, order="asc")
