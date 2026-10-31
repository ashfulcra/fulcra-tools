"""Owned Attention schema and cross-language flat Event contract."""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

FIXTURES = json.loads((Path(__file__).parent / 'fixtures' / 'v1_wire_cases.json').read_text())['cases']
BASE = {'id', 'start_time', 'end_time', 'sources', 'tags'}


def test_attention_spec_owns_and_freezes_exact_declared_enrichment():
    from fulcra_attention.v1 import attention_spec
    spec = attention_spec()
    assert spec.key == 'collect.attention.v1' and spec.base_type == 'Event' and spec.name == 'Attention'
    assert spec.fingerprint == '005994ac66e5f4548050c843c0a261891d59d358c8f67578f1cb6dd93b8ba941'
    fragment = json.loads(spec.create_body()['record_spec']['schema'])
    assert set(fragment['properties']) == set(FIXTURES[0]['record']) - BASE
    assert set(fragment['required']) == set(fragment['properties'])
    changed = deepcopy(fragment)
    changed['properties']['note']['type'] = 'number'
    assert json.loads(attention_spec().create_body()['record_spec']['schema']) == fragment
    for case in FIXTURES:
        assert Draft202012Validator(fragment).is_valid(case['record'])


@pytest.mark.parametrize('case', FIXTURES, ids=[case['name'] for case in FIXTURES])
def test_shared_attention_source_uuid_fields_tags_nulls_and_millisecond_bounds(case):
    from fulcra_attention.v1 import build_attention_record
    original = case['record']
    fields = {key: value for key, value in original.items() if key not in BASE}
    record = build_attention_record(data_type=case['context']['dataType'], source_id=case['source_id'],
        start_time=datetime.fromisoformat(original['start_time'].replace('Z', '+00:00')),
        end_time=datetime.fromisoformat(original['end_time'].replace('Z', '+00:00')),
        tags=case['context']['tagIds'], fields=fields)
    assert record['id'] == original['id'] and record['sources'] == [case['source_id']]
    from fulcra_common.v1 import record_matches
    assert record_matches(record, original)
    assert not {'data', 'metadata', 'specversion'} & set(record)


@pytest.mark.parametrize('change', [{'unknown': 'unretainable'}, {'service': 'other'},
    {'duration_seconds': float('nan')}, {'external_ids': {}}, {'note': '\ud800'}, {'url': None}])
def test_bad_or_lossy_enrichment_refuses_before_a_flat_record_is_returned(change):
    from fulcra_attention.v1 import build_attention_record
    original = deepcopy(FIXTURES[0]['record'])
    fields = {key: value for key, value in original.items() if key not in BASE}
    with pytest.raises(ValueError):
        build_attention_record(data_type=FIXTURES[0]['context']['dataType'], source_id=FIXTURES[0]['source_id'],
            start_time=datetime.fromisoformat(original['start_time'].replace('Z', '+00:00')),
            end_time=datetime.fromisoformat(original['end_time'].replace('Z', '+00:00')),
            tags=original['tags'], fields=fields | change)


def test_iterable_tag_and_auxiliary_source_snapshots_are_not_consumed_by_validation():
    from fulcra_attention.v1 import build_attention_record
    original = FIXTURES[0]['record']
    fields = {key: value for key, value in original.items() if key not in BASE}
    record = build_attention_record(data_type=FIXTURES[0]['context']['dataType'], source_id=FIXTURES[0]['source_id'],
        start_time=datetime.fromisoformat(original['start_time'].replace('Z', '+00:00')),
        end_time=datetime.fromisoformat(original['end_time'].replace('Z', '+00:00')),
        tags=iter(original['tags']), fields=fields, extra_sources=iter(['com.fulcra.synthetic.provenance']))
    assert record['tags'] == original['tags']
    assert record['sources'] == [FIXTURES[0]['source_id'], 'com.fulcra.synthetic.provenance']


def test_schema_preserves_historical_unknown_device_without_inventing_a_current_identity():
    from fulcra_attention.v1 import attention_spec
    original = deepcopy(FIXTURES[0]['record'])
    original['external_ids']['device'] = None
    fragment = json.loads(attention_spec().create_body()['record_spec']['schema'])
    assert Draft202012Validator(fragment).is_valid(original)
