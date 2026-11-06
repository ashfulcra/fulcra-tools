"""Pure observed MomentAnnotation reader conversion; never upload authority.

The plan proves a typed ID and the enumerated metadata bindings below, not a
whole-definition snapshot. Unknown JSON remains inside complete raw provenance.
"""
import json
import re

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from fulcra_common.v1 import _canonical_uuid, build_event_record, _json_equal
from fulcra_collect.migration_convert import ConvertedEntry
from fulcra_collect.migration_plan import MigrationPlan, _time
from fulcra_collect.record_actions import _hash, _json
from .v1 import UNIFIED_JOURNAL_SPEC

_CONVERTER = 'collect.journal.reader.v1'
_DEFINITION_PREFIX = 'com.fulcradynamics.annotation.'
_PRIMARY = re.compile(r'com\.fulcra\.dayone\.[0-9a-f]{16}')


def _has_reference(value):
    if isinstance(value, dict):
        return bool({'$ref', '$dynamicRef', '$recursiveRef'} & value.keys()) or any(
            _has_reference(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_reference(child) for child in value)
    return False


class JournalHistoryConverter:
    """Versioned, plan-bound API reader codec for the observed Day One shape."""
    def __init__(self, plan: MigrationPlan):
        self.plan = MigrationPlan(plan.document)
        document = self.plan.document
        if document['converter'] != _CONVERTER:
            raise ValueError('unsupported historical converter version')
        if not self.plan.source_data_type.startswith('MomentAnnotation/'):
            raise ValueError('Journal converter requires a typed MomentAnnotation source')
        if not self.plan.destination_data_type.startswith('Event/'):
            raise ValueError('Journal converter requires an Event destination')
        self._definition = self.plan.source_data_type.split('/')[1]
        self._binding = _DEFINITION_PREFIX + self._definition
        self._prefixes = tuple(document['source_prefixes'])
        self._start, self._end = _time(document['start_time']), _time(document['end_time'])
        schema = document['destination_schema']
        if _has_reference(schema):
            raise ValueError('Journal destination schema references are unsupported')
        contract = json.loads(UNIFIED_JOURNAL_SPEC.create_body()['record_spec']['schema'])
        if (not set(contract['required']) <= set(schema.get('required', []))
                or any(not _json_equal(fragment, schema['properties'].get(key))
                    for key, fragment in contract['properties'].items())):
            raise ValueError('Journal destination schema must declare the unified contract fragments')
        self._contract = Draft202012Validator(contract)
        self._validator = Draft202012Validator(schema, format_checker=FormatChecker(), registry=Registry())

    def convert(self, raw):
        # Unretainable originals are static errors, never a pretend quarantine.
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
        if 'legacy_record' in original:
            return quarantine('reserved-migration-field')
        if {'data', 'specversion', 'start_time', 'end_time'} & original.keys():
            return quarantine('unsupported-record-envelope')
        metadata = original.get('metadata')
        if (not isinstance(metadata, dict) or 'source_name' not in original
                or not isinstance(original['source_name'], (str, type(None)))):
            return quarantine('unsupported-record-envelope')
        if metadata.get('id') != self._definition:
            return quarantine('mismatched-definition')
        if metadata.get('fulcra_userid') != self.plan.account_id:
            return quarantine('mismatched-account')
        if metadata.get('fulcra_source_id') != self._binding or original.get('source_id') != self._binding:
            return quarantine('mismatched-definition')
        if metadata.get('annotation_type') not in ('moment', 'MomentAnnotation'):
            return quarantine('incompatible-annotation-kind')
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
        if (self._binding not in sources or any(source.startswith(_DEFINITION_PREFIX)
                and source != self._binding for source in sources)):
            return quarantine('mismatched-definition')
        candidates = [source for source in sources if source.startswith('com.fulcra.dayone')]
        if any(_PRIMARY.fullmatch(source) is None for source in candidates):
            return quarantine('malformed-primary-identity')
        if not candidates:
            return quarantine('outside-source-namespace')
        if len(candidates) != 1:
            return quarantine('ambiguous-primary-identity')
        primary = candidates[0]
        if not primary.startswith(self._prefixes):
            return quarantine('outside-source-namespace')
        try:
            instant = _time(original['recorded_at'])
        except (ValueError, TypeError, KeyError):
            return quarantine('invalid-time')
        if not self._start <= instant < self._end:
            return quarantine('outside-plan-range')
        fields = {key: original.get(key) for key in ('note', 'title', 'external_ids', 'dayone_tag_names')}
        fields['legacy_record'] = dict(version=1, converter=_CONVERTER, source_api_version='v1alpha1',
            source_data_type=self.plan.source_data_type, raw=original)
        payload = build_event_record(data_type=self.plan.destination_data_type, source_id=primary,
            start_time=instant, tags=tags, extra_sources=[source for source in sources if source != primary], fields=fields)
        if not self._contract.is_valid(payload) or not self._validator.is_valid(payload):
            return quarantine('invalid-payload')
        try:
            prepared = _json(payload)
        except ValueError:
            return quarantine('payload-too-large')
        return ConvertedEntry(_hash(encoded), identity, 'converted', None, primary, prepared)
