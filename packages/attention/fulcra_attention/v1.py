"""Opt-in Attention Event schema/encoding shared with browser v1 vectors.

This module does not resolve destinations, capture consent, deliver records or
switch the installed browser writers. Preserve their queues/install identities
and wire the authority/delivery transition before enabling v1 capture.
"""
from datetime import datetime
from importlib.resources import files
import json
import re

from jsonschema import Draft202012Validator

from fulcra_common.data_types import DataTypeSpec
from fulcra_common.json_values import snapshot_json
from fulcra_common.v1 import build_event_record, _canonical_uuid, _utc

_CONTRACT = files('fulcra_attention').joinpath('v1_contract.json').read_text()
_DEFINITION = json.loads(_CONTRACT)
if _DEFINITION['version'] != 1 or _DEFINITION['key'] != 'collect.attention.v1':
    raise ValueError('unsupported Attention v1 schema contract')
_VALIDATOR = Draft202012Validator({'type': 'object', 'additionalProperties': False,
    'properties': _DEFINITION['fields'], 'required': _DEFINITION['required_fields']})


def attention_spec():
    """Return an independently snapshotted, owned v1 Attention destination."""
    contract = json.loads(_CONTRACT)
    return DataTypeSpec(key=contract['key'], base_type=contract['base_type'],
        name=contract['name'], description=contract['description'], fields=contract['fields'],
        required_fields=tuple(contract['required_fields']))


def build_attention_record(*, data_type, source_id, start_time, end_time, tags=(),
                           fields, extra_sources=()):
    """Preserve normalized fields and the existing v3 primary source verbatim.

    New browser captures retain milliseconds; history keeps its original stored
    precision. This pure builder does not authorize replay or account selection.
    """
    if not isinstance(source_id, str) or re.fullmatch(r'com\.fulcra\.attention\.v3\.[a-f0-9]{16}', source_id) is None:
        raise ValueError('Attention v1 requires its original v3 primary source')
    if not isinstance(start_time, datetime) or not isinstance(end_time, datetime):
        raise ValueError('Attention v1 requires aware timestamp bounds')
    start, end = _utc(start_time), _utc(end_time)
    if start.microsecond % 1000 or end.microsecond % 1000:
        raise ValueError('Attention v1 capture precision exceeds milliseconds')
    copied = snapshot_json(fields)
    if not isinstance(copied, dict) or not _VALIDATOR.is_valid(copied):
        raise ValueError('Attention v1 fields do not match the declared schema')
    url, category = copied['url'], copied['category']
    if ((url is None) == (category is None) or not (url or category).strip()
            or copied['duration_seconds'] != (end - start).total_seconds()):
        raise ValueError('Attention v1 variant/duration is invalid')
    tags, extra_sources = tuple(tags), tuple(extra_sources)
    for tag in tags:
        _canonical_uuid(tag)
    if any(not isinstance(source, str) or not source.strip() for source in extra_sources):
        raise ValueError('Attention v1 auxiliary sources must be nonempty text')
    return build_event_record(data_type=data_type, source_id=source_id,
        start_time=start, end_time=end, tags=tags, extra_sources=extra_sources, fields=copied)
