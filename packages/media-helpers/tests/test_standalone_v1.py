"""Dormant adapter authority/recovery tests, with synthetic HTTP only."""
from contextlib import contextmanager

import httpx
import pytest

from fulcra_common.client import BaseFulcraClient

ACCOUNT = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'profile'))
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request',
                        lambda *_: pytest.fail('external HTTP forbidden'))
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={'userid': ACCOUNT}))
    return BaseFulcraClient(base_url='https://example.test', transport=transport)


def test_resume_precedes_yield_and_source_callback(client, monkeypatch):
    from fulcra_media import standalone_v1 as adapter
    log = []
    real_runtime = adapter.standalone_runtime
    @contextmanager
    def runtime(*args, **kwargs):
        with real_runtime(*args, **kwargs) as pair:
            log.append('identity')
            yield pair
    monkeypatch.setattr(adapter, 'standalone_runtime', runtime)
    monkeypatch.setattr(adapter.MediaRecordWriter, 'resume', lambda self: log.append('recovery'))
    with adapter.open_media({}, client=client) as run:
        log.append('source')
        assert run.import_events([]).total == 0
    assert log[:3] == ['identity', 'recovery', 'source']


def test_namespace_lease_serializes_and_releases_on_error(client, monkeypatch):
    from fulcra_media import standalone_v1 as adapter
    calls = []
    def fail(self):
        calls.append(self.session)
        raise RuntimeError('recovery failed')
    monkeypatch.setattr(adapter.MediaRecordWriter, 'resume', fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match='recovery failed'):
            with adapter.open_media({}, client=client):
                pytest.fail('source must not run')
    assert len(calls) == 2


def test_check_only_never_drains_or_stages(client, monkeypatch):
    from fulcra_media.standalone_v1 import open_media
    from fulcra_media.collect_v1 import MediaRecordWriter
    settings = {'v1_destination_ids': {}}
    with open_media(settings, client=client) as run:
        run.ctx.outbox_set('v1_media_pending', {'malformed': 'retain'})
    monkeypatch.setattr(MediaRecordWriter, 'resume', lambda self: pytest.fail('preview drained'))
    with open_media(settings, client=client, check_only=True) as run:
        assert run.ctx.config['preview_only'] is True
        assert run.import_events([]).total == 0
        assert run.ctx.outbox_get('v1_media_pending') == {'malformed': 'retain'}
    assert settings == {'v1_destination_ids': {}}


@pytest.mark.parametrize('settings,flag', [([], False), ({}, 1), ({'dry_run': 'yes'}, True),
    ({'preview_only': True}, False), ({'preview_only': False}, True)])
def test_malformed_or_conflicting_preview_rejected(client, settings, flag):
    from fulcra_media.standalone_v1 import open_media
    with pytest.raises(ValueError):
        with open_media(settings, client=client, check_only=flag):
            pytest.fail('invalid authority admitted')


@pytest.mark.parametrize('mode', ['visible', 'unknown', 'settings', 'account', 'malformed', 'legacy', 'preview', 'rejected', 'excluded'])
def test_actual_writer_path_retains_shared_delivery_guards(tmp_path, monkeypatch, mode):
    import json
    import base64
    import re
    from datetime import datetime, timedelta, timezone
    from fulcra_collect import db
    from fulcra_common.data_types import CreationJournal
    from fulcra_common.record_exclusions import EXCLUSION_SPEC
    from fulcra_media.importers.base import NormalizedEvent
    from fulcra_media.v1 import media_spec
    from fulcra_media.standalone_v1 import open_media

    profile = tmp_path / 'profile'
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT, 'exp': 4102444800}).encode()).decode().rstrip('=')
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(profile))
    monkeypatch.setenv('FULCRA_ACCESS_TOKEN', 'synthetic.' + claims + '.signature')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_: pytest.fail('external HTTP'))
    destination = 'Event/11111111-2222-4333-8444-555555555555'
    control = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
    tag = '11111111-1111-4111-8111-111111111111'
    profile.mkdir()
    CreationJournal(profile / 'v1-type-creations.sqlite').resolved(ACCOUNT, EXCLUSION_SPEC, control)
    rows, posts, sources = {}, [], []
    account = [ACCOUNT]
    reading = [True]

    def handle(request):
        path = request.url.path
        if path == '/user/v1alpha1/info':
            return httpx.Response(200, json={'userid': account[0]})
        if path == '/input/v1/data_type':
            return httpx.Response(200, json=[{'id': full, 'fulcra_userid': ACCOUNT,
                'name': name, 'api_version': 'v1', 'recordable': True,
                'deprecated': False, 'record_spec': {'type': 'event'}}
                for full, name in ((destination, 'Watched'), (control, EXCLUSION_SPEC.name))])
        if path.endswith('/schema'):
            spec = EXCLUSION_SPEC if control in path else media_spec('watched')
            return httpx.Response(200, json={'type': 'object', 'properties': {
                **{k: {} for k in ('id', 'sources', 'tags', 'start_time', 'end_time')},
                **spec.fields}, 'required': list(spec.required_fields)})
        if '/tag/name/' in path or '/tag/id/' in path:
            return httpx.Response(200, json={'id': tag, 'name': 'synthetic', 'fulcra_userid': ACCOUNT})
        if path == '/data/v1/records':
            if not reading[0]:
                return httpx.Response(503)
            query = request.url.params['q']
            full = re.search('__name__="([^"]+)"', query).group(1)
            ids = re.findall('id="([^"]+)"', query)
            return httpx.Response(200, text='\n'.join(json.dumps(rows[full, i])
                for i in ids if (full, i) in rows))
        if path == '/ingest/v1/record/' + destination:
            batch = [json.loads(line) for line in request.content.splitlines()]
            posts.extend(batch)
            if mode == 'rejected':
                return httpx.Response(400, json={'error': 'synthetic rejection'})
            if mode != 'unknown':
                rows.update({(destination, row['id']): row for row in batch})
            raise httpx.ReadError('synthetic lost receipt')
        pytest.fail('unexpected path ' + path)

    transport = httpx.MockTransport(handle)
    client = BaseFulcraClient(base_url='https://example.test', transport=transport)
    settings = {'v1_destination_ids': {'watched': destination}}
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    sample = NormalizedEvent(importer='synthetic', service='synthetic', category='watched',
        note='Synthetic', title='Synthetic', start_time=start, end_time=start + timedelta(minutes=1),
        deterministic_id='com.fulcra.media.synthetic.v1.one', timestamp_confidence='high',
        external_ids={})
    if mode == 'excluded':
        from fulcra_common.v1 import build_event_record
        from fulcra_media.v1 import build_media_record
        record = build_media_record(sample, destination)
        exclusion = build_event_record(data_type=control,
            source_id=f"collect:v1:excluded:{destination}:{record['id']}", start_time=start,
            fields={'schema_version': 1, 'target_data_type': destination,
                'target_record_id': record['id'], 'action_id': ACCOUNT,
                'reason': 'undo', 'evidence': []})
        rows[control, exclusion['id']] = exclusion
        with open_media(settings, client=client) as run:
            result = run.import_events([sample])
            assert result.posted == result.verified == 0
            assert result.skipped_existing == 1
        assert posts == []
        return
    with pytest.raises(httpx.HTTPError):
        with open_media(settings, client=client) as run:
            sources.append('first')
            run.import_events([sample])
    assert len(posts) == 1
    conn = db.open(profile / 'state.db')
    before = db.get_record_outbox(conn, 'cli-media', 'v1_media_pending')
    assert before['groups'] and before['dispatch_version'] == 1
    if mode == 'settings':
        settings = {**settings, 'twin_policy': 'keep'}
    if mode == 'account':
        account[0] = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    if mode == 'unknown':
        reading[0] = False
    if mode == 'malformed':
        before['groups'] = 'malformed'
        db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', before)
    if mode == 'legacy':
        before.pop('dispatch_version')
        before.pop('dispatch')
        rows.clear()
        db.set_record_outbox(conn, 'cli-media', 'v1_media_pending', before)
    if mode == 'preview':
        with open_media(settings, client=client, check_only=True) as run:
            assert run.import_events([sample]).verified == 0
            with pytest.raises(RuntimeError):
                run.writer.run_import([sample])
        assert db.get_record_outbox(conn, 'cli-media', 'v1_media_pending') == before
        assert len(posts) == 1
        return
    if mode == 'visible':
        with open_media(settings, client=client) as run:
            sources.append('second')
            assert run.import_events([]).total == 0
        assert db.get_record_outbox(conn, 'cli-media', 'v1_media_pending') == {'groups': []}
        assert sources == ['first', 'second']
    else:
        with pytest.raises((RuntimeError, ValueError, httpx.HTTPError)):
            with open_media(settings, client=client):
                sources.append('must not acquire even empty source')
        assert db.get_record_outbox(conn, 'cli-media', 'v1_media_pending') == before
        assert sources == ['first']
    assert len(posts) == 1


def test_media_namespace_lease_serializes(client, monkeypatch):
    from fulcra_media import standalone_v1 as adapter
    real_runtime = adapter.standalone_runtime
    def bounded(*args, **kwargs):
        return real_runtime(*args, **kwargs, lease_timeout_s=0)
    monkeypatch.setattr(adapter, 'standalone_runtime', bounded)
    with adapter.open_media({}, client=client):
        with pytest.raises(TimeoutError):
            with adapter.open_media({}, client=client):
                pytest.fail('competing media writer yielded')
    with adapter.open_media({}, client=client):
        pass


@pytest.mark.parametrize('change', ['home', 'settings', 'cross-origin', 'account'])
def test_profile_account_origin_and_token_fences(client, monkeypatch, tmp_path, change):
    from fulcra_media.standalone_v1 import open_media
    with open_media({}, client=client) as run:
        if change == 'home':
            monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'wrong'))
        if change == 'settings':
            run.ctx.config['changed'] = True
        if change == 'account':
            run.session._identity_http.close()
            run.session._identity_http = httpx.Client(base_url=run.session.origin,
                transport=httpx.MockTransport(lambda r: httpx.Response(200,
                    json={'userid': 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'})))
        path = 'https://other.test/request' if change == 'cross-origin' else '/request'
        with pytest.raises(RuntimeError):
            run.session.client._client().get(path)
        assert not (tmp_path / 'wrong').exists()
