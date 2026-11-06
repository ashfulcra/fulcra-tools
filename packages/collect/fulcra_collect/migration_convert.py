"""Pure, versioned conversion of explicitly scoped legacy media records.

No lookup, upload, destination adoption or deletion occurs here. Unknown shapes
retain a quarantine result, never a guessed target. The ledger retains originals.
"""
from dataclasses import dataclass, field
import json

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry
from referencing.exceptions import Unresolvable

from fulcra_common.v1 import _canonical_uuid, build_event_record
from .migration_plan import MigrationPlan, _time
from .record_actions import _hash, _json

_CODECS = {f'collect.media.{category}.v{version}': (category, version)
           for category in ('watched', 'listened', 'read', 'activity') for version in (1, 2)}
_ENVELOPE = frozenset({'id', 'recorded_at', 'sources', 'tags', 'source_id'})
_BASE = frozenset({'id', 'start_time', 'end_time', 'sources', 'tags', 'value', 'unit'})
_DEFINITION_PREFIX = 'com.fulcradynamics.annotation.'


@dataclass(frozen=True)
class ConvertedEntry:
    raw_hash: str
    legacy_id: str | None
    status: str
    reason: str | None
    source_id: str | None = field(repr=False)
    _payload_json: str | None = field(repr=False)

    @property
    def payload(self):
        return None if self._payload_json is None else json.loads(self._payload_json)


class MediaHistoryConverter:
    """Plan-bound codecs for typed DurationAnnotation media history.

    Missing enrichment stays missing; null stays null. Declared extensions are
    retained. Category comes from the explicit codec; duration is derived only
    when absent. Version 1 handles flat typed rows. Version 2 preserves the API
    reader definition snapshot in declared legacy_record provenance. Legacy
    batch envelopes and deletion markers need other codecs.
    """
    def __init__(self, plan: MigrationPlan):
        self.plan = MigrationPlan(plan.document)
        document = self.plan.document
        if document['converter'] not in _CODECS:
            raise ValueError('unsupported historical converter version')
        if not self.plan.source_data_type.startswith('DurationAnnotation/'):
            raise ValueError('media converter requires a typed DurationAnnotation source')
        if not self.plan.destination_data_type.startswith('Event/'):
            raise ValueError('media converter requires an Event destination')
        self.category, self._version = _CODECS[document['converter']]
        self._definition = self.plan.source_data_type.split('/')[1]
        self._prefixes = tuple(document['source_prefixes'])
        self._start, self._end = _time(document['start_time']), _time(document['end_time'])
        schema = document['destination_schema']
        self._declared = frozenset(schema['properties']) - _BASE
        if not {'category', 'duration_seconds'} <= self._declared:
            raise ValueError('media destination must declare category and duration_seconds')
        if self._version == 2 and 'legacy_record' not in self._declared:
            raise ValueError('media destination must declare legacy_record for reader converter v2')
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker(), registry=Registry())

    def _reader_record(self, original):
        if 'legacy_record' in original:
            return None, 'reserved-migration-field'
        metadata = original.get('metadata')
        if (not isinstance(metadata, dict) or 'source_name' not in original
                or not isinstance(original['source_name'], (str, type(None)))):
            return None, 'unsupported-record-envelope'
        binding = _DEFINITION_PREFIX + self._definition
        if metadata.get('id') != self._definition:
            return None, 'mismatched-definition'
        if metadata.get('fulcra_userid') != self.plan.account_id:
            return None, 'mismatched-account'
        if metadata.get('fulcra_source_id') != binding or original.get('source_id') != binding:
            return None, 'mismatched-definition'
        kind = metadata.get('annotation_type')
        if not isinstance(kind, str) or kind not in {'duration', 'DurationAnnotation'}:
            return None, 'incompatible-annotation-kind'
        provenance = {'id': original['id'], 'data_type': self.plan.source_data_type,
            'api_version': 'v1alpha1', 'source_id': original['source_id'],
            'source_name': original['source_name'], 'metadata': metadata}
        return {**{key: value for key, value in original.items() if key not in {'metadata', 'source_name'}},
                'source_id': self._definition, 'legacy_record': provenance}, None

    def convert(self, raw):
        # Refuse unretainable/non-JSON input before classifying it as a raw row.
        encoded = _json(raw)
        original = json.loads(encoded)
        identity = original.get('id')
        if not isinstance(identity, str) or not identity.strip() or len(identity.encode()) > 512:
            identity = None
        def quarantine(reason):
            return ConvertedEntry(_hash(encoded), identity, 'quarantined', reason, None, None)
        if identity is None:
            return quarantine('missing-record-id')
        if {'supersedes_source_id', 'superseded_by'} & original.keys():
            return quarantine('unsupported-deletion-marker')
        if self._version == 2:
            original, reason = self._reader_record(original)
            if reason is not None:
                return quarantine(reason)
        if {'metadata', 'data', 'specversion'} & original.keys():
            return quarantine('unsupported-record-envelope')
        if original.get('source_id', self._definition) != self._definition:
            return quarantine('mismatched-definition')
        sources, tags = original.get('sources'), original.get('tags')
        if (not isinstance(sources, list) or not isinstance(tags, list)
                or any(not isinstance(source, str) or not source.strip() for source in sources)
                or any(not isinstance(tag, str) for tag in tags)
                or len(set(sources)) != len(sources) or len(set(tags)) != len(tags)):
            return quarantine('malformed-provenance')
        try:
            for tag in tags:
                _canonical_uuid(tag)
        except ValueError:
            return quarantine('malformed-provenance')
        if any(source.startswith(_DEFINITION_PREFIX) and source != _DEFINITION_PREFIX + self._definition
               for source in sources):
            return quarantine('mismatched-definition')
        primary = [source for source in sources if source.startswith(self._prefixes)
                   and not source.startswith(('com.fulcra.content.', _DEFINITION_PREFIX))]
        if not primary:
            return quarantine('outside-source-namespace')
        if len(primary) != 1:
            return quarantine('ambiguous-primary-identity')
        try:
            times = original['recorded_at']
            if not isinstance(times, dict) or set(times) != {'start_time', 'end_time'}:
                raise ValueError
            start, end = _time(times['start_time']), _time(times['end_time'])
            if end < start:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            return quarantine('invalid-time')
        if not self._start <= start < self._end:
            return quarantine('outside-plan-range')
        if original.get('category', self.category) != self.category:
            return quarantine('incompatible-category')
        if original.keys() - _ENVELOPE - self._declared:
            return quarantine('undeclared-fields')
        fields = {key: value for key, value in original.items() if key in self._declared}
        fields['category'] = self.category
        fields.setdefault('duration_seconds', (end - start).total_seconds())
        payload = build_event_record(data_type=self.plan.destination_data_type, source_id=primary[0],
            start_time=start, end_time=end, extra_sources=[source for source in sources if source != primary[0]],
            tags=tags, fields=fields)
        try:
            invalid = next(self._validator.iter_errors(payload), None) is not None
        except Unresolvable:
            raise ValueError('migration destination schema has an unavailable reference') from None
        if invalid:
            return quarantine('invalid-payload')
        try:
            prepared = _json(payload)
        except ValueError:
            return quarantine('payload-too-large')
        return ConvertedEntry(_hash(encoded), identity, 'converted', None, primary[0], prepared)
