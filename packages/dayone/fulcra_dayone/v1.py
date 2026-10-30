"""Pure Journal Event contract shared by live and historical adapters."""
from copy import deepcopy

from jsonschema import Draft202012Validator

from fulcra_common.data_types import DataTypeSpec, tag_create_name
from fulcra_common.v1 import build_event_record

from .convert import to_event
from .entry import DayOneEntry

_FIELDS = {
    "note": {"type": "string"},
    "title": {"type": ["string", "null"]},
    # Null is reserved for historical rows whose original tag labels were not
    # retained; live entries always provide their original ordered label array.
    "dayone_tag_names": {"type": ["array", "null"], "items": {"type": "string"}},
    "external_ids": {"type": "object", "properties": {
        "dayone_uuid": {"type": "string", "minLength": 1}, "journal": {"type": "string"},
        "starred": {"type": "boolean"}, "word_count": {"type": "integer", "minimum": 0},
        "photo_count": {"type": "integer", "minimum": 0}, "location": {"type": "string"}},
        "required": ["dayone_uuid", "journal", "starred", "word_count", "photo_count"]},
}
JOURNAL_SPEC = DataTypeSpec(key="dayone.journal.v1", base_type="Event", name="Journal",
    description="Day One journal entries, imported once with their original source identity.",
    fields=deepcopy(_FIELDS), required_fields=tuple(_FIELDS))
_VALIDATOR = Draft202012Validator({"type": "object", "properties": deepcopy(_FIELDS), "required": list(_FIELDS)})


def build_journal_record(entry: DayOneEntry, destination: str, *, tags=()) -> dict:
    """Keep the existing text normalization, source hash and nullable title.

    Original tag labels remain enrichment even when a new Fulcra tag needs a
    shortened machine name. Photos/attachments are not uploaded.
    """
    # The legacy normalizer intentionally omits falsey locations and iterates
    # tags; reject wrong raw shapes before those operations can hide a value
    # or turn a tag string into individual character labels.
    if (entry.location is not None and not isinstance(entry.location, str)) or not isinstance(entry.tags, (tuple, list)):
        raise ValueError("Day One location/tag values have an invalid source shape")
    for name in entry.tags:
        tag_create_name(name)
    event = to_event(entry)
    record = build_event_record(data_type=destination, source_id=event.source_id,
        start_time=event.start_time, tags=tags,
        fields={"note": event.note, "title": event.title, "external_ids": event.external_ids,
                "dayone_tag_names": list(entry.tags)})
    if next(_VALIDATOR.iter_errors(record), None) is not None:
        raise ValueError("Day One entry violates the declared Journal v1 schema")
    return record


# A separate, opt-in local contract revision. Existing writer bindings and
# creation reservations continue to use JOURNAL_SPEC above.
_UNIFIED_FIELDS = deepcopy(_FIELDS)
_UNIFIED_FIELDS['external_ids']['type'] = ['object', 'null']
_UNIFIED_FIELDS['legacy_record'] = {
    'type': ['object', 'null'],
    'properties': {
        'version': {'type': 'integer', 'const': 1},
        'converter': {'type': 'string', 'const': 'collect.journal.reader.v1'},
        'source_api_version': {'type': 'string', 'const': 'v1alpha1'},
        'source_data_type': {'type': 'string',
            'pattern': r'^MomentAnnotation/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'},
        'raw': {'type': 'object'},
    },
    'required': ['version', 'converter', 'source_api_version', 'source_data_type', 'raw'],
    'additionalProperties': False,
}
UNIFIED_JOURNAL_SPEC = DataTypeSpec(key='dayone.journal.v2', base_type='Event', name='Day One Journal',
    description='Day One journal entries with nullable historical enrichment and complete reader provenance.',
    fields=deepcopy(_UNIFIED_FIELDS), required_fields=tuple(_UNIFIED_FIELDS))
_UNIFIED_VALIDATOR = Draft202012Validator({'type': 'object', 'properties': deepcopy(_UNIFIED_FIELDS),
    'required': list(_UNIFIED_FIELDS)})


def build_unified_journal_record(entry: DayOneEntry, destination: str, *, tags=()) -> dict:
    """Opt in to the new contract without relaxing strict live-source checks."""
    record = build_journal_record(entry, destination, tags=tags)
    record['legacy_record'] = None
    if next(_UNIFIED_VALIDATOR.iter_errors(record), None) is not None:
        raise ValueError('Day One entry violates the unified Journal schema')
    return record
