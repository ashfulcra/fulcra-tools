"""Synthetic history conversion must preserve content or retain a quarantine."""
from copy import deepcopy
import json
from uuid import UUID

import pytest

from fulcra_collect.migration_plan import MigrationPlan
from fulcra_common.v1 import build_event_record


def uid(number):
    return str(UUID(int=number))


def plan(**changes):
    from fulcra_media.v1 import media_history_spec
    fields = json.loads(media_history_spec('watched').create_body()['record_spec']['schema'])['properties']
    return MigrationPlan({'version': 1, 'plan_id': uid(1), 'account_id': uid(2),
        'source_api_version': 'v1alpha1', 'source_data_type': 'DurationAnnotation/' + uid(3),
        'destination_api_version': 'v1', 'destination_data_type': 'Event/' + uid(4),
        'converter': 'collect.media.watched.v1', 'identity_version': 1,
        'source_prefixes': ['com.fulcra.media.netflix.'],
        'start_time': '2020-01-01T00:00:00Z', 'end_time': '2021-01-01T00:00:00Z',
        'destination_schema': {'type': 'object', 'additionalProperties': False,
            'properties': {**{key: {} for key in ('id', 'start_time', 'end_time', 'sources', 'tags')}, **fields}},
        **changes})


def raw(**changes):
    return {'id': uid(5), 'recorded_at': {'start_time': '2020-06-01T12:00:00Z',
        'end_time': '2020-06-01T12:01:00Z'}, 'sources': ['com.fulcra.media.netflix.synthetic',
        'com.fulcra.content.watched.v1.synthetic', 'com.fulcradynamics.annotation.' + uid(3)],
        'tags': [uid(6)], 'note': 'Synthetic café 🐈', **changes}


def convert(original=None, **changes):
    from fulcra_collect.migration_convert import MediaHistoryConverter
    return MediaHistoryConverter(plan(**changes)).convert(raw() if original is None else original)


def test_flat_typed_history_retains_null_absence_provenance_tags_and_shared_uuid():
    original = raw(title=None, external_ids={'nested': {'opaque': '值'}})
    result = convert(original)
    assert result.status == 'converted' and result.reason is None
    assert result.legacy_id == uid(5) and result.source_id == original['sources'][0]
    from fulcra_collect.migration_plan import _time
    expected = build_event_record(data_type=plan().destination_data_type,
        source_id=original['sources'][0], extra_sources=original['sources'][1:], tags=original['tags'],
        start_time=_time(original['recorded_at']['start_time']), end_time=_time(original['recorded_at']['end_time']),
        fields={'note': original['note'], 'title': None, 'external_ids': original['external_ids'],
                'category': 'watched', 'duration_seconds': 60.0})
    assert result.payload == expected
    assert 'service' not in result.payload and 'importer' not in result.payload
    original['note'] = 'mutated input'
    exposed = result.payload
    exposed['external_ids']['nested']['opaque'] = 'mutated output'
    assert result.payload == expected


def test_fully_enriched_history_matches_live_media_uuid_and_all_content():
    from fulcra_media.importers.base import NormalizedEvent
    from fulcra_media.v1 import build_media_record
    from fulcra_collect.migration_plan import _time
    original = raw(title='Synthetic title', service='Synthetic service', importer='netflix',
                   timestamp_confidence='high', external_ids={'opaque': 'synthetic'}, category='watched')
    live = NormalizedEvent(start_time=_time(original['recorded_at']['start_time']),
        end_time=_time(original['recorded_at']['end_time']), deterministic_id=original['sources'][0],
        extra_source_ids=tuple(original['sources'][1:]), **{key: original[key] for key in (
            'note', 'title', 'service', 'importer', 'timestamp_confidence', 'external_ids', 'category')})
    assert convert(original).payload == build_media_record(live, plan().destination_data_type, tags=original['tags'])


@pytest.mark.parametrize('changes,reason', [
    ({'id': None}, 'missing-record-id'),
    ({'sources': ['other.namespace.synthetic']}, 'outside-source-namespace'),
    ({'sources': ['com.fulcra.media.netflix.one', 'com.fulcra.media.netflix.two']}, 'ambiguous-primary-identity'),
    ({'sources': ['com.fulcra.media.netflix.synthetic', 'com.fulcradynamics.annotation.' + uid(9)]}, 'mismatched-definition'),
    ({'source_id': uid(9)}, 'mismatched-definition'),
    ({'tags': ['not-a-tag-uuid']}, 'malformed-provenance'),
    ({'recorded_at': {'start_time': '2020-06-01', 'end_time': '2020-06-02'}}, 'invalid-time'),
    ({'recorded_at': {'start_time': '2020-06-01T12:00:00.000000001Z', 'end_time': '2020-06-01T12:01:00Z'}}, 'invalid-time'),
    ({'recorded_at': {'start_time': '2020-06-01T12:01:00Z', 'end_time': '2020-06-01T12:00:00Z'}}, 'invalid-time'),
    ({'category': 'listened'}, 'incompatible-category'),
    ({'note': {'unexpected': 'object'}}, 'invalid-payload'),
    ({'unknown_private_field': 'must not disappear'}, 'undeclared-fields'),
    ({'supersedes_source_id': 'synthetic target'}, 'unsupported-deletion-marker'),
    ({'data': '{"note":"unparsed envelope"}'}, 'unsupported-record-envelope'),
])
def test_ambiguous_or_lossy_conversion_is_quarantined_without_a_target(changes, reason):
    result = convert(raw(**changes))
    assert result.status == 'quarantined' and result.reason == reason
    assert result.payload is None and result.source_id is None


def test_boundary_rows_outside_the_plan_are_retained_for_explicit_disposition():
    original = raw(recorded_at={'start_time': plan().document['end_time'], 'end_time': '2021-01-01T00:00:00Z'})
    assert convert(original).reason == 'outside-plan-range'


@pytest.mark.parametrize('changes', [
    {'converter': 'collect.unknown.v1'}, {'converter': 'collect.media.watched.v3'},
    {'source_data_type': 'NumericAnnotation/' + uid(3)}, {'destination_data_type': 'Metric/' + uid(4),
        'destination_schema': {'properties': {key: {} for key in ('id', 'start_time', 'end_time', 'sources', 'tags', 'value')}}},
])
def test_unknown_codec_or_wrong_family_is_a_plan_error_before_any_row(changes):
    from fulcra_collect.migration_convert import MediaHistoryConverter
    with pytest.raises(ValueError, match='converter|destination|source'):
        MediaHistoryConverter(plan(**changes))


def test_declared_extension_field_is_preserved_without_becoming_implicit_schema_authority():
    document = plan().document
    document['destination_schema']['properties']['custom_enrichment'] = {'type': ['object', 'null']}
    original = raw(custom_enrichment={'opaque': ['synthetic', None]})
    result = convert(original, destination_schema=document['destination_schema'])
    assert result.payload['custom_enrichment'] == original['custom_enrichment']
    assert convert(original).reason == 'undeclared-fields'


def test_explicit_duration_is_not_silently_recomputed_and_duplicate_provenance_is_refused():
    assert convert(raw(duration_seconds=59.5)).payload['duration_seconds'] == 59.5
    original = raw()
    original['sources'].append(original['sources'][0])
    assert convert(original).reason == 'malformed-provenance'
    malformed = deepcopy(raw())
    malformed['tags'].append(malformed['tags'][0])
    assert convert(malformed).reason == 'malformed-provenance'


def test_schema_references_cannot_fetch_unfrozen_remote_content(monkeypatch):
    import io
    import urllib.request
    calls = []
    def fetch(request):
        calls.append(request)
        return io.BytesIO(b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"string"}')
    monkeypatch.setattr(urllib.request, 'urlopen', fetch)
    schema = plan().document['destination_schema']
    schema['properties']['note'] = {'$ref': 'https://synthetic.invalid/unfrozen-note-schema'}
    with pytest.raises(ValueError, match='reference'):
        convert(destination_schema=schema)
    assert calls == []


def test_local_schema_definitions_resolve_from_the_frozen_plan():
    schema = plan().document['destination_schema']
    schema['$defs'] = {'note': {'type': 'string'}}
    schema['properties']['note'] = {'$ref': '#/$defs/note'}
    assert convert(destination_schema=schema).payload['note'] == raw()['note']


def test_retainable_raw_with_oversized_output_has_an_explicit_quarantine():
    from fulcra_collect.record_actions import _json
    original = raw(note='')
    original['note'] = 'x' * (1024 * 1024 - len(_json(original).encode()) - 1)
    assert len(_json(original).encode()) == 1024 * 1024 - 1
    result = convert(original)
    assert result.status == 'quarantined' and result.reason == 'payload-too-large'
    assert result.payload is None


@pytest.mark.parametrize('category', ['watched', 'listened', 'read', 'activity'])
def test_all_supported_media_codecs_bind_the_explicit_category(category):
    result = convert(converter=f'collect.media.{category}.v1')
    assert result.status == 'converted' and result.payload['category'] == category
    assert convert(raw(category='unknown'), converter=f'collect.media.{category}.v1').reason == 'incompatible-category'


def reader_record(**changes):
    definition = uid(3)
    binding = 'com.fulcradynamics.annotation.' + definition
    return raw(source_id=binding, source_name='Original display name', metadata={
        'id': definition, 'fulcra_userid': uid(2), 'fulcra_source_id': binding,
        'annotation_type': 'duration', 'name': 'Renamed definition',
        'description': 'Synthetic definition', 'deleted_at': None,
        'created_at': '2019-01-01T00:00:00Z', 'updated_at': '2020-06-02T00:00:00Z',
        'measurement_spec': {'unit': 's'}, 'spec': {'opaque': [None, False, 0]},
        'tags': [uid(6)], 'mesh_identifier': 'synthetic mesh provenance'}, **changes)


def test_reader_codec_preserves_definition_snapshot_and_distinct_display_names():
    original = reader_record()
    result = convert(original, converter='collect.media.watched.v2')
    assert result.status == 'converted'
    assert result.payload['legacy_record'] == {'id': uid(5), 'data_type': plan().source_data_type,
        'api_version': 'v1alpha1', 'source_id': original['source_id'],
        'source_name': original['source_name'], 'metadata': original['metadata']}
    assert result.payload['note'] == original['note']
    assert result.payload['sources'] == original['sources'] and result.payload['tags'] == original['tags']
    # Version 1's frozen behavior must not change when another codec is added.
    assert convert(original, converter='collect.media.watched.v1').reason == 'unsupported-record-envelope'


@pytest.mark.parametrize('field,value,reason', [
    ('id', uid(9), 'mismatched-definition'), ('fulcra_userid', uid(9), 'mismatched-account'),
    ('fulcra_source_id', 'other.binding', 'mismatched-definition'),
    ('annotation_type', 'moment', 'incompatible-annotation-kind'),
    ('annotation_type', {}, 'incompatible-annotation-kind'),
])
def test_reader_codec_requires_explicit_owner_definition_and_duration_kind(field, value, reason):
    original = reader_record()
    original['metadata'][field] = value
    assert convert(original, converter='collect.media.watched.v2').reason == reason


def test_reader_codec_refuses_partial_or_forged_provenance_and_undeclared_audit():
    from fulcra_collect.migration_convert import MediaHistoryConverter
    original = reader_record()
    original['source_id'] = uid(3)  # reader binding is the prefixed source, not the bare definition UUID
    assert convert(original, converter='collect.media.watched.v2').reason == 'mismatched-definition'
    assert convert(raw(), converter='collect.media.watched.v2').reason == 'unsupported-record-envelope'
    original = reader_record(legacy_record={'forged': 'must not be overwritten'})
    assert convert(original, converter='collect.media.watched.v2').reason == 'reserved-migration-field'
    schema = plan().document['destination_schema']
    schema['properties'].pop('legacy_record')
    with pytest.raises(ValueError, match='declare.*legacy_record'):
        MediaHistoryConverter(plan(converter='collect.media.watched.v2', destination_schema=schema))


@pytest.mark.parametrize('category', ['watched', 'listened', 'read', 'activity'])
def test_reader_codec_covers_all_media_categories_without_reinterpreting_metadata(category):
    original = reader_record()
    result = convert(original, converter=f'collect.media.{category}.v2')
    assert result.status == 'converted' and result.payload['category'] == category
    assert result.payload['legacy_record']['metadata'] == original['metadata']


def test_explicitly_mapped_soft_deleted_definition_is_provenance_not_record_deletion():
    original = reader_record()
    original['metadata']['deleted_at'] = '2020-06-03T00:00:00Z'
    result = convert(original, converter='collect.media.watched.v2')
    assert result.status == 'converted'
    assert result.payload['legacy_record']['metadata']['deleted_at'] == original['metadata']['deleted_at']
