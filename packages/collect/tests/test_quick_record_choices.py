"""Legacy track metadata is migration input, never a record destination."""
import base64
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from fulcra_collect.plugin import RunContext
from fulcra_collect.quick_record_v1 import QuickRecordSession, quick_record_context
from fulcra_collect.record_actions import ActionJournal
from fulcra_common.v1 import record_id_for

ACCOUNT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
OTHER = 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'
TRACK = str(UUID(int=21))
TAG = str(UUID(int=22))
DESTINATION = 'Event/11111111-2222-4333-8444-555555555555'
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def runtime(collect_home, monkeypatch):
    claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': ACCOUNT}).encode()).decode().rstrip('=')
    monkeypatch.setattr(RunContext, 'collect_token', lambda _: 'synthetic.' + claims + '.signature')
    ctx, account = quick_record_context()
    state = {'definitions': [{'id': TRACK, 'name': 'Synthetic track 🐈', 'annotation_type': 'duration',
                             'tags': [TAG], 'fulcra_userid': ACCOUNT}],
             'catalog': [], 'schemas': {}, 'creates': [], 'lose_create': False}

    def handler(request):
        if request.url.path == '/user/v1alpha1/annotation':
            return httpx.Response(200, json=state['definitions'])
        if request.url.path == '/input/v1/data_type':
            return httpx.Response(200, json=state['catalog'])
        if request.url.path.endswith('/schema'):
            return httpx.Response(200, json=state['schemas'][DESTINATION])
        assert request.method == 'POST' and request.url.path == '/input/v1/data_type/Event'
        body = json.loads(request.content)
        state['creates'].append(body)
        fragment = json.loads(body['record_spec']['schema'])
        state['schemas'][DESTINATION] = fragment | {'properties': {
            'id': {}, 'sources': {}, 'tags': {}, 'start_time': {}, 'end_time': {}, **fragment['properties']}}
        state['catalog'] = [{'id': DESTINATION, 'fulcra_userid': ACCOUNT, 'api_version': 'v1',
                             'recordable': True, 'deprecated': False, 'name': body['name'], 'record_spec': {'type': 'event'}}]
        if state['lose_create']:
            raise httpx.ReadTimeout('synthetic lost creation response')
        return httpx.Response(201, json={'id': DESTINATION})

    with QuickRecordSession(ctx, expected_account=account, expected_epoch=ctx.config_epoch,
                            transport=httpx.MockTransport(handler)) as session:
        yield SimpleNamespace(session=session, journal=ActionJournal(collect_home / 'actions.sqlite'), state=state)


def choices(runtime):
    from fulcra_collect.quick_record_choices import QuickRecordChoices
    return QuickRecordChoices(runtime.session, runtime.journal)


def test_complete_choices_are_read_only_and_preserve_track_kind_tags_and_digest(runtime):
    service = choices(runtime)
    first = service.list()
    assert first[0]['id'] == TRACK and first[0]['kind'] == 'duration'
    assert first[0]['name'] == 'Synthetic track 🐈' and first[0]['tags'] == [TAG]
    assert len(first[0]['digest']) == 64 and first == service.list()
    assert runtime.state['creates'] == []
    runtime.state['definitions'][0]['name'] = 'Changed track'
    assert service.list()[0]['digest'] != first[0]['digest']


def test_choice_digest_change_is_rejected_before_any_destination_creation(runtime):
    service = choices(runtime)
    first = service.list()[0]
    runtime.state['definitions'][0]['tags'] = []
    with pytest.raises(ValueError, match='changed'):
        service.prepare(TRACK, expected_digest=first['digest'])
    assert runtime.state['creates'] == []


@pytest.mark.parametrize('change', [
    {'id': 'invalid'}, {'tags': ['invalid']}, {'tags': 'invalid'}, {'fulcra_userid': OTHER},
    {'name': ''}, {'deleted_at': True},
])
def test_malformed_or_foreign_track_catalog_cannot_become_empty_or_write_authority(runtime, change):
    service = choices(runtime)
    runtime.state['definitions'][0].update(change)
    with pytest.raises(ValueError):
        service.list()
    assert runtime.state['creates'] == []


def test_duplicate_track_ids_are_unknown_instead_of_first_wins(runtime):
    runtime.state['definitions'].append(dict(runtime.state['definitions'][0]))
    with pytest.raises(ValueError, match='duplicate|ambiguous'):
        choices(runtime).list()


def test_first_preparation_is_owned_v1_and_track_rename_reuses_original_binding(runtime):
    service = choices(runtime)
    selected = service.list()[0]
    prepared = service.prepare(TRACK, expected_digest=selected['digest'])
    assert prepared['data_type'] == DESTINATION and prepared['name'] == selected['name']
    original_creation = runtime.state['creates'][0]
    runtime.state['definitions'][0]['name'] = 'Renamed track'
    current = service.list()[0]
    prepared_again = choices(runtime).prepare(TRACK, expected_digest=current['digest'])
    assert prepared_again['data_type'] == DESTINATION and prepared_again['name'] == 'Renamed track'
    assert runtime.state['creates'] == [original_creation]


def test_lost_type_creation_response_then_rename_never_creates_another_destination(runtime):
    service = choices(runtime)
    selected = service.list()[0]
    runtime.state['lose_create'] = True
    with pytest.raises(httpx.ReadTimeout):
        service.prepare(TRACK, expected_digest=selected['digest'])
    runtime.state['definitions'][0]['name'] = 'Renamed after lost response'
    selected = service.list()[0]
    prepared = choices(runtime).prepare(TRACK, expected_digest=selected['digest'])
    assert prepared['data_type'] == DESTINATION and len(runtime.state['creates']) == 1


def test_prepared_builder_preserves_v1_fields_primary_source_track_and_tags(runtime):
    from fulcra_collect.quick_record_choices import build_quick_record
    service = choices(runtime)
    selected = service.list()[0]
    prepared = service.prepare(TRACK, expected_digest=selected['digest'])
    action_id = str(UUID(int=23))
    payload = build_quick_record(prepared, action_id=action_id, started_at=NOW,
                                ended_at=NOW + timedelta(seconds=90), comment='Synthetic note 🐈')
    record = payload['record']
    source = 'com.fulcradynamics.fulcra-collect.quick-record.' + action_id
    assert payload['data_type'] == DESTINATION
    assert record['id'] == record_id_for(DESTINATION, source) and record['sources'] == [source]
    assert record['tags'] == [TAG] and record['note'] == 'Synthetic note 🐈'
    assert record['track_id'] == record['legacy_definition_id'] == TRACK
    assert record['track_name'] == selected['name'] and record['kind'] == 'duration'
    assert record['start_time'] == NOW.isoformat().replace('+00:00', 'Z')
    assert record['end_time'] == (NOW + timedelta(seconds=90)).isoformat().replace('+00:00', 'Z')
    assert not {'data', 'recorded_at', 'metadata', 'specversion'} & record.keys()
