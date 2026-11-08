"""Immutable offline scope for one legacy Collect stream and v1 destination.

A plan is data, never runtime authority. The exporter/apply actor must still
prove the current account, ownership/schema, coverage and cutover conditions.
No HTTP, credential lookup, datatype creation or legacy mutation happens here.
"""
from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
import json
import re

from jsonschema import Draft202012Validator, SchemaError
from fulcra_common.json_values import snapshot_json
from fulcra_common.v1 import _canonical_uuid, _destination, _instant_key

_FIELDS = frozenset({'version', 'plan_id', 'account_id', 'source_api_version', 'source_data_type',
    'destination_api_version', 'destination_data_type', 'converter', 'identity_version',
    'source_prefixes', 'start_time', 'end_time', 'destination_schema'})
_BASE_FIELDS = frozenset({'id', 'start_time', 'end_time', 'sources', 'tags'})
_LEGACY = frozenset({'DurationAnnotation', 'MomentAnnotation', 'NumericAnnotation', 'InstantAnnotation'})


def _time(value):
    try:
        second, fraction = _instant_key(value)
        if len(fraction) > 6:
            raise ValueError
        return second + timedelta(microseconds=int(fraction.ljust(6, '0')))
    except (TypeError, ValueError, OverflowError):
        raise ValueError('migration plan time must include an explicit timezone') from None


def _canonical(document):
    try:
        value = snapshot_json(document)
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError
        if (type(value['version']) is not int or value['version'] != 1
                or type(value['identity_version']) is not int or value['identity_version'] != 1
                or value['source_api_version'] != 'v1alpha1' or value['destination_api_version'] != 'v1'):
            raise ValueError
        _canonical_uuid(value['plan_id'])
        _canonical_uuid(value['account_id'])
        source = value['source_data_type']
        if not isinstance(source, str):
            raise ValueError
        base, separator, identity = source.partition('/')
        if base not in _LEGACY or not separator:
            raise ValueError
        _canonical_uuid(identity)
        value['destination_data_type'] = _destination(value['destination_data_type'])
        converter = value['converter']
        if (not isinstance(converter, str) or len(converter) > 128
                or not re.fullmatch(r'collect\.[a-z0-9]+(?:[.-][a-z0-9]+)*\.v[1-9][0-9]*', converter)):
            raise ValueError
        prefixes = value['source_prefixes']
        if (not isinstance(prefixes, list) or not 1 <= len(prefixes) <= 64
                or any(not isinstance(prefix, str) or not 1 <= len(prefix.encode()) <= 256
                       or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9:._/-]*', prefix) for prefix in prefixes)):
            raise ValueError
        prefixes = sorted(prefixes)
        if any(later.startswith(earlier) for earlier, later in zip(prefixes, prefixes[1:])):
            raise ValueError
        value['source_prefixes'] = prefixes
        start, end = _time(value['start_time']), _time(value['end_time'])
        if end <= start:
            raise ValueError
        value['start_time'], value['end_time'] = [instant.isoformat().replace('+00:00', 'Z') for instant in (start, end)]
        schema = value['destination_schema']
        required = _BASE_FIELDS | ({'value'} if value['destination_data_type'].startswith('Metric/') else set())
        if (not isinstance(schema, dict) or schema.get('type', 'object') != 'object'
                or not isinstance(schema.get('properties'), dict) or not required <= schema['properties'].keys()):
            raise ValueError
        Draft202012Validator.check_schema(schema)
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(encoded.encode()) > 1024 * 1024:
            raise ValueError
        return encoded
    except (ValueError, TypeError, KeyError, SchemaError, UnicodeError):
        raise ValueError('migration plan has unsupported, ambiguous or malformed scope/schema') from None


@dataclass(frozen=True, init=False)
class MigrationPlan:
    """Canonical account/mapping/schema/range fingerprint, safe to persist privately.

    Creation cannot prove remote ownership or export completeness. A future
    executor must reject unknown converter versions before processing any row.
    """
    _encoded: str = field(repr=False)
    plan_id: str
    account_id: str = field(repr=False)
    source_data_type: str = field(repr=False)
    destination_data_type: str = field(repr=False)
    fingerprint: str

    def __init__(self, document):
        encoded = _canonical(document)
        frozen = json.loads(encoded)
        object.__setattr__(self, '_encoded', encoded)
        for key in ('plan_id', 'account_id', 'source_data_type', 'destination_data_type'):
            object.__setattr__(self, key, frozen[key])
        object.__setattr__(self, 'fingerprint', hashlib.sha256(encoded.encode()).hexdigest())

    @classmethod
    def from_document(cls, document):
        return cls(document)

    @property
    def document(self):
        """Independent JSON snapshot; callers cannot mutate the saved plan."""
        return json.loads(self._encoded)
