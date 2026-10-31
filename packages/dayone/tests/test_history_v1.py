"""Offline unified contract and reader codec; no client or credentials."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import pytest
from jsonschema import Draft202012Validator

from dayone_history_test_helpers import plan, raw, uid
from fulcra_dayone import v1
from fulcra_dayone.entry import DayOneEntry


def convert(original=None, **changes):
    from fulcra_dayone.history_v1 import JournalHistoryConverter
    return JournalHistoryConverter(plan(**changes)).convert(raw() if original is None else original)


def test_unified_contract_is_opt_in_and_legacy_fingerprint_and_body_are_frozen():
    assert getattr(v1, 'UNIFIED_JOURNAL_SPEC', None) is not None
    assert v1.JOURNAL_SPEC.fingerprint == 'bba5361f61e844695fd2458c20ece7ef2d8956d56adb71128f52d8f843da9b64'
    encoded = json.dumps(v1.JOURNAL_SPEC.create_body(), sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    assert hashlib.sha256(encoded.encode()).hexdigest() == 'c0430e41388ef93e18be37deec7a9efc9de20f6266472aba415e5e86684e18f5'
    body = v1.UNIFIED_JOURNAL_SPEC.create_body()
    assert body['name'] == 'Day One Journal'
    assert body['metadata'] == {'fulcra_tools': {'key': 'dayone.journal.v2', 'schema_version': 1}}
    import fulcra_dayone.collect_v1 as writer
    assert writer.JOURNAL_SPEC is v1.JOURNAL_SPEC


def live_entry(**changes):
    return DayOneEntry(**(dict(uuid='ABC123', creation_date=datetime(2020, 6, 1, tzinfo=timezone.utc),
        text='Synthetic note', tags=('label',), starred=False, journal='Synthetic', location=None,
        photo_count=0, word_count=2) | changes))


def test_live_and_history_use_exact_uuid_spelling_primary_and_destination_identity():
    for spelling in ('ABC123', 'abc123'):
        live = v1.build_unified_journal_record(live_entry(uuid=spelling), plan().destination_data_type, tags=[uid(6)])
        old = v1.build_journal_record(live_entry(uuid=spelling), plan().destination_data_type, tags=[uid(6)])
        assert live == old | {'legacy_record': None}
        original = raw(sources=[raw()['sources'][0], live['sources'][0]], recorded_at=live['start_time'], note=live['note'])
        historical = convert(original).payload
        assert historical['id'] == live['id']
        assert historical['start_time'] == historical['end_time'] == live['start_time']
    assert v1.build_unified_journal_record(live_entry(uuid='ABC123'), plan().destination_data_type)['id'] != live['id']


@pytest.mark.parametrize('change', [{'location': False}, {'location': 0}, {'location': {}},
    {'tags': 'label'}, {'tags': ['']}, {'starred': 0}, {'word_count': True}, {'photo_count': -1}, {'journal': None}])
def test_new_live_builder_keeps_strict_source_preflight(change):
    with pytest.raises((ValueError, TypeError)):
        v1.build_unified_journal_record(live_entry(**change), plan().destination_data_type)


def test_historical_nullable_acceptance_complete_raw_roundtrip_and_snapshot_isolation():
    original = raw(unknown={'null': None, 'false': False, 'zero': 0, 'array': ['b', 'a']})
    result = convert(original)
    assert result.status == 'converted'
    payload = result.payload
    assert payload['note'] == original['note']
    assert payload['title'] is payload['external_ids'] is payload['dayone_tag_names'] is None
    assert payload['legacy_record'] == dict(version=1, converter='collect.journal.reader.v1',
        source_api_version='v1alpha1', source_data_type=plan().source_data_type, raw=original)
    assert payload['legacy_record']['raw']['sources'] == original['sources']
    assert payload['legacy_record']['raw']['recorded_at'] == '2020-11-01T01:30:00-04:00'
    assert payload['start_time'] == payload['end_time'] == '2020-11-01T05:30:00Z'
    assert not Draft202012Validator(json.loads(v1.JOURNAL_SPEC.create_body()['record_spec']['schema'])).is_valid(payload)
    assert Draft202012Validator(plan().document['destination_schema']).is_valid(payload)
    original['unknown']['zero'] = 10
    payload['legacy_record']['raw']['note'] = 'changed'
    assert result.payload['legacy_record']['raw']['unknown']['zero'] == 0
    assert result.payload['legacy_record']['raw']['note'] == raw()['note']


@pytest.mark.parametrize('field,value', [('title', ''), ('external_ids', None), ('dayone_tag_names', []),
    ('external_ids', {'dayone_uuid': 'X', 'journal': '', 'starred': False, 'word_count': 0, 'photo_count': 0, 'extra': [None]})])
def test_valid_known_enrichment_is_preserved(field, value):
    assert convert(raw(**{field: value})).payload[field] == value


@pytest.mark.parametrize('changes,reason', [
    ({'id': None}, 'missing-record-id'), ({'note': None}, 'invalid-payload'),
    ({'note': 0}, 'invalid-payload'), ({'title': False}, 'invalid-payload'),
    ({'external_ids': {}}, 'invalid-payload'), ({'external_ids': {'starred': 0}}, 'invalid-payload'),
    ({'dayone_tag_names': ['ok', 0]}, 'invalid-payload'),
    ({'source_id': uid(3)}, 'mismatched-definition'),
    ({'source_name': False}, 'unsupported-record-envelope'),
    ({'legacy_record': None}, 'reserved-migration-field'),
    ({'data': '{}'}, 'unsupported-record-envelope'), ({'specversion': '1.0'}, 'unsupported-record-envelope'),
    ({'start_time': '2020-06-01T00:00:00Z'}, 'unsupported-record-envelope'),
    ({'supersedes_source_id': 'old'}, 'unsupported-deletion-marker'),
    ({'superseded_by': None}, 'unsupported-deletion-marker'),
    ({'sources': ['com.fulcra.dayone.0123456789abcdef']}, 'mismatched-definition'),
    ({'sources': [raw()['sources'][0], 'com.fulcra.dayone.0123456789abcdeF']}, 'malformed-primary-identity'),
    ({'sources': [raw()['sources'][0], 'com.fulcra.dayone.0123456789abcdef.extra']}, 'malformed-primary-identity'),
    ({'sources': [raw()['sources'][0], 'other.primary']}, 'outside-source-namespace'),
    ({'sources': raw()['sources'] + ['com.fulcra.dayone.ffffffffffffffff']}, 'ambiguous-primary-identity'),
    ({'sources': raw()['sources'] + [raw()['sources'][0]]}, 'malformed-provenance'),
    ({'tags': [uid(6), uid(6)]}, 'malformed-provenance'), ({'tags': ['BAD']}, 'malformed-provenance'),
    ({'recorded_at': '2020-11-01T01:30:00'}, 'invalid-time'),
    ({'recorded_at': {'start_time': '2020-11-01T01:30:00Z'}}, 'invalid-time'),
    ({'recorded_at': '2020-11-01T01:30:00.0000001Z'}, 'invalid-time'),
    ({'recorded_at': '2021-01-01T00:00:00Z'}, 'outside-plan-range'),
])
def test_malformed_or_ambiguous_rows_are_retained_as_quarantines(changes, reason):
    result = convert(raw(**changes))
    assert (result.status, result.reason, result.payload, result.source_id) == ('quarantined', reason, None, None)


@pytest.mark.parametrize('field,value,reason', [('id', uid(9), 'mismatched-definition'),
    ('fulcra_userid', uid(9), 'mismatched-account'), ('fulcra_source_id', 'wrong', 'mismatched-definition'),
    ('annotation_type', 'duration', 'incompatible-annotation-kind'), ('annotation_type', [], 'incompatible-annotation-kind')])
def test_metadata_is_bound_to_plan_account_exact_typed_definition_and_kind(field, value, reason):
    original = raw()
    original['metadata'][field] = value
    assert convert(original).reason == reason


@pytest.mark.parametrize('change', [{'source_data_type': 'InstantAnnotation/' + uid(3)},
    {'destination_data_type': 'Metric/' + uid(4), 'destination_schema': {'properties': {k: {} for k in ('id', 'start_time', 'end_time', 'sources', 'tags', 'value')}}}])
def test_wrong_families_fail_before_row_work(change):
    from fulcra_dayone.history_v1 import JournalHistoryConverter
    with pytest.raises(ValueError, match='source|destination'):
        JournalHistoryConverter(plan(**change))


@pytest.mark.parametrize('keyword', ['$ref', '$dynamicRef', '$recursiveRef'])
def test_schema_references_in_untraversed_branches_are_static_errors(keyword):
    from fulcra_dayone.history_v1 import JournalHistoryConverter
    schema = plan().document['destination_schema']
    schema['if'] = {'properties': {'note': {'const': 'never matches'}}}
    schema['then'] = {keyword: 'https://synthetic.invalid/schema'}
    with pytest.raises(ValueError, match='reference'):
        JournalHistoryConverter(plan(destination_schema=schema))


def test_incompatible_destination_fragments_fail_before_row_work():
    from fulcra_dayone.history_v1 import JournalHistoryConverter
    schema = plan().document['destination_schema']
    schema['properties']['external_ids'] = deepcopy(v1.JOURNAL_SPEC.fields['external_ids'])
    with pytest.raises(ValueError, match='schema'):
        JournalHistoryConverter(plan(destination_schema=schema))


def test_payload_doubling_uses_encoded_utf8_bound_and_never_truncates():
    from fulcra_collect.record_actions import _json
    original = raw(note='é' * 100000)
    assert len(_json(original).encode()) < 1024 * 1024
    assert convert(original).reason == 'payload-too-large'
    with pytest.raises(ValueError, match='size limit'):
        convert(raw(note='é' * 180000))


def test_plan_prefix_is_independent_and_dst_offsets_retain_distinct_instants():
    assert convert(source_prefixes=['com.fulcra.dayone.f']).reason == 'outside-source-namespace'
    first = convert().payload
    second = convert(raw(recorded_at='2020-11-01T01:30:00-05:00')).payload
    assert first['start_time'] != second['start_time']
    assert convert(raw(recorded_at='2020-01-01T00:00:00Z')).status == 'converted'


def test_full_destination_schema_constraints_are_enforced_after_local_contract():
    schema = plan().document['destination_schema']
    schema['allOf'] = [{'properties': {'tags': {'maxItems': 0}}}]
    assert convert(destination_schema=schema).reason == 'invalid-payload'
    assert convert(raw(tags=[]), destination_schema=schema).status == 'converted'


def test_raw_nonobjects_nonjson_and_nonfinite_values_cannot_be_classified_as_retained_rows():
    from fulcra_dayone.history_v1 import JournalHistoryConverter
    codec = JournalHistoryConverter(plan())
    for value in ([], None, {'unknown': float('nan')}, {'unknown': object()}):
        with pytest.raises(ValueError):
            codec.convert(value)


def test_provenance_outer_envelope_is_closed_and_raw_extensions_cannot_override_it():
    original = raw(version=99, converter='opaque original', source_data_type='original extension')
    payload = convert(original).payload
    provenance = payload['legacy_record']
    assert provenance['version'] == 1 and provenance['converter'] == 'collect.journal.reader.v1'
    assert provenance['raw'] == original
    validator = Draft202012Validator(plan().document['destination_schema'])
    for change in ({'extra': None}, {'version': True}, {'source_data_type': 'MomentAnnotation/ABC'}):
        changed = deepcopy(payload)
        changed['legacy_record'].update(change)
        assert not validator.is_valid(changed)
    changed = deepcopy(payload)
    del changed['legacy_record']['raw']
    assert not validator.is_valid(changed)
