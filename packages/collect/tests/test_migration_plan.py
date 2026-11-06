"""Offline history plans freeze account, scope, codec, range and served schema."""
from copy import deepcopy
from uuid import UUID

import pytest

ACCOUNT = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
DESTINATION = 'Event/11111111-2222-4333-8444-555555555555'


def document(**changes):
    return {'version': 1, 'plan_id': str(UUID(int=1)), 'account_id': ACCOUNT,
        'source_api_version': 'v1alpha1', 'source_data_type': 'DurationAnnotation/' + str(UUID(int=2)),
        'destination_api_version': 'v1', 'destination_data_type': DESTINATION,
        'converter': 'collect.media.watched.v1', 'identity_version': 1,
        'source_prefixes': ['com.fulcra.netflix.'],
        'start_time': '2020-01-01T00:00:00Z', 'end_time': '2026-01-01T00:00:00Z',
        'destination_schema': {'type': 'object', 'properties': {'id': {'type': 'string'},
            'start_time': {'type': 'string'}, 'end_time': {'type': 'string'},
            'sources': {'type': 'array'}, 'tags': {'type': 'array'}, 'note': {'type': ['string', 'null']}}},
        **changes}


def plan(**changes):
    from fulcra_collect.migration_plan import MigrationPlan
    return MigrationPlan.from_document(document(**changes))


def test_plan_is_private_data_only_and_returns_independent_snapshots():
    value = document()
    from fulcra_collect.migration_plan import MigrationPlan
    frozen = MigrationPlan.from_document(value)
    original_hash = frozen.fingerprint
    value['destination_schema']['properties']['note'] = {'type': 'integer'}
    exposed = frozen.document
    exposed['source_prefixes'].append('other.')
    assert frozen.document == document()
    assert frozen.fingerprint == original_hash and len(original_hash) == 64
    assert frozen.account_id == ACCOUNT and frozen.destination_data_type == DESTINATION


@pytest.mark.parametrize('changes', [
    {'account_id': 'not-a-UUID'}, {'plan_id': 'not-a-UUID'}, {'version': True}, {'version': 2},
    {'source_api_version': 'v1'}, {'destination_api_version': 'v1alpha1'},
    {'source_data_type': 'DurationAnnotation'}, {'destination_data_type': 'DurationAnnotation/' + str(UUID(int=2))},
    {'identity_version': True}, {'identity_version': 2}, {'converter': ''},
    {'source_prefixes': []}, {'source_prefixes': ['com.fulcra.', 'com.fulcra.']},
    {'source_prefixes': ['']}, {'source_prefixes': ['com.fulcra.', 'com.fulcra.netflix.']},
    {'start_time': '2020-01-01'}, {'end_time': '2020-01-01T00:00:00Z'},
    {'destination_schema': {'type': 123}}, {'destination_schema': {'type': 'object'}},
    {'unknown_override': True},
])
def test_unsupported_or_ambiguous_plan_is_never_accepted(changes):
    with pytest.raises(ValueError):
        plan(**changes)


def test_semantic_time_zone_and_object_order_normalize_but_scope_changes_do_not():
    first = plan()
    same = plan(start_time='2019-12-31T19:00:00-05:00', end_time='2025-12-31T19:00:00-05:00')
    assert same.fingerprint == first.fingerprint
    for changes in [{'account_id': 'aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff'},
                    {'destination_data_type': 'Event/11111111-2222-4333-8444-666666666666'},
                    {'source_prefixes': ['com.fulcra.youtube.']}, {'converter': 'collect.media.read.v1'}]:
        assert plan(**changes).fingerprint != first.fingerprint
    schema = deepcopy(document()['destination_schema'])
    schema['properties']['note'] = {'type': ['string', 'null'], 'maxLength': 100}
    assert plan(destination_schema=schema).fingerprint != first.fingerprint


def test_lossy_json_schema_cannot_enter_an_immutable_plan():
    schema = document()['destination_schema']
    schema['properties']['note']['examples'] = [{1: 'numeric key', '1': 'text key'}]
    with pytest.raises(ValueError):
        plan(destination_schema=schema)


def test_window_bound_cannot_silently_round_away_a_submicrosecond_boundary():
    with pytest.raises(ValueError):
        plan(start_time='2020-01-01T00:00:00.000000001Z')
    assert plan(start_time='2020-01-01T00:00:00.000000000Z').fingerprint == plan().fingerprint


@pytest.mark.parametrize('offset', ['+00:60', '+01:99', '-00:60', '+24:00'])
def test_plan_rejects_malformed_timezone_offsets(offset):
    with pytest.raises(ValueError):
        plan(start_time='2020-01-01T00:00:00' + offset)
