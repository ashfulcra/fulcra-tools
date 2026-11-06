"""Media v1 encoding must retain enrichment dropped by the old typed path."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from fulcra_media.importers.base import NormalizedEvent, BULK_IMPORT_NOTE_MARKER

DESTINATION = "Event/11111111-2222-4333-8444-555555555555"


def event(**changes):
    start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    args = dict(importer="synthetic", service="synthetic", category="watched", note="Synthetic film",
                title="Synthetic film", start_time=start, end_time=start + timedelta(minutes=12),
                deterministic_id="com.fulcra.media.synthetic.v1.example", timestamp_confidence="high",
                external_ids={"example_id": "one", "details": {"version": 1}},
                extra_source_ids=("com.fulcra.content.watched.v1.synthetic",))
    return NormalizedEvent(**{**args, **changes})


def test_media_record_preserves_every_normalized_field_without_mutating_source():
    from fulcra_media.v1 import build_media_record
    source = event()
    original = deepcopy(source)
    record = build_media_record(source, DESTINATION, tags=("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",))
    assert record == {"id": record["id"], "start_time": "2026-01-02T00:00:00Z", "end_time": "2026-01-02T00:12:00Z",
                      "sources": [source.deterministic_id, *source.extra_source_ids],
                      "tags": ["aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"],
                      "note": source.note, "title": source.title, "service": source.service,
                      "importer": source.importer, "category": source.category,
                      "timestamp_confidence": source.timestamp_confidence,
                      "external_ids": source.external_ids, "duration_seconds": 720.0}
    source.external_ids["details"]["version"] = 2
    assert record["external_ids"]["details"]["version"] == 1
    assert original == event()
    assert record["id"] == build_media_record(event(), DESTINATION)["id"]


def test_low_confidence_marker_retains_provenance_and_is_idempotent():
    from fulcra_media.v1 import build_media_record
    source = event(timestamp_confidence="low", external_ids={"timestamp_cluster_size": 20})
    record = build_media_record(source, DESTINATION)
    assert record["note"].endswith(BULK_IMPORT_NOTE_MARKER)
    assert record["timestamp_confidence"] == "low"
    assert record["external_ids"] == {"timestamp_cluster_size": 20}
    assert build_media_record(event(note=record["note"], timestamp_confidence="low"), DESTINATION)["note"] == record["note"]


def test_same_source_replays_keep_separate_ids_even_with_shared_fingerprints():
    from fulcra_media.v1 import build_media_record
    first = event()
    second = event(deterministic_id="com.fulcra.media.synthetic.v1.replay", start_time=first.start_time + timedelta(minutes=1))
    a, b = build_media_record(first, DESTINATION), build_media_record(second, DESTINATION)
    assert a["id"] != b["id"]
    assert a["sources"][1:] == b["sources"][1:]


def test_specs_route_semantic_categories_and_declare_all_live_fields():
    import json
    from fulcra_media.v1 import media_spec, build_media_record
    names = {"watched": "Watched", "listened": "Listened", "read": "Read", "activity": "Activity"}
    for category, name in names.items():
        spec = media_spec(category)
        assert spec.base_type == "Event"
        assert spec.key == f"collect.media.{category}.v1"
        assert spec.name == name
        schema = json.loads(spec.create_body()["record_spec"]["schema"])
        fields = build_media_record(event(category=category), DESTINATION)
        assert set(fields) - {"id", "sources", "tags", "start_time", "end_time"} == set(schema["properties"])
        assert "legacy_record" not in fields  # source collection invents no historical provenance
    with pytest.raises(ValueError):
        media_spec("unknown")


def test_historical_audit_schema_is_opt_in_and_preserves_default_creation_intents(tmp_path):
    from dataclasses import replace
    from fulcra_common.data_types import CreationJournal
    from fulcra_media.v1 import media_spec, media_history_spec
    account = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    for category in ('watched', 'listened', 'read', 'activity'):
        default = media_spec(category)
        assert 'legacy_record' not in default.fields
        old = replace(default, fields={key: value for key, value in default.fields.items() if key != 'legacy_record'})
        journal = CreationJournal(tmp_path / (category + '.sqlite'))
        journal.begin(account, old)
        journal.resolved(account, old, DESTINATION)
        historical = media_history_spec(category)
        assert historical.key != default.key and historical.name != default.name
        assert 'legacy_record' in historical.fields
        assert historical.fields.keys() == default.fields.keys() | {'legacy_record'}
        assert media_spec(category).fingerprint == old.fingerprint
        assert journal.read(account, media_spec(category))[1] == 'resolved'
        assert journal.read(account, historical) is None


def test_live_default_spec_can_use_an_explicitly_pinned_richer_history_destination(tmp_path):
    import base64
    import json
    import httpx
    from fulcra_common.client import BaseFulcraClient
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    from fulcra_media.v1 import media_spec, media_history_spec
    account = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
    history_destination = 'Event/11111111-2222-4333-8444-666666666666'
    default, history = media_spec('watched'), media_history_spec('watched')
    schema = {'properties': {**{key: {} for key in ('id', 'sources', 'tags', 'start_time', 'end_time')}, **history.fields}}
    requests = []
    def handler(request):
        requests.append(request.method)
        if request.url.path == '/input/v1/data_type':
            return httpx.Response(200, json=[{'id': history_destination, 'name': history.name,
                'api_version': 'v1', 'recordable': True, 'deprecated': False, 'fulcra_userid': account, 'record_spec': {'type': 'event'}}])
        return httpx.Response(200, json=schema)
    class Client(BaseFulcraClient):
        def get_token(self):
            payload = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': account}).encode()).decode().rstrip('=')
            return 'synthetic.' + payload + '.signature'
    client = Client(base_url='https://api.example', transport=httpx.MockTransport(handler))
    journal = CreationJournal(tmp_path / 'types.sqlite')
    journal.begin(account, default)
    journal.resolved(account, default, DESTINATION)
    resolved = resolve_data_type(client=client, spec=default, account_id=account,
        pinned_id=history_destination)  # live read-only setup does not retarget creation intents
    assert resolved.data_type == history_destination
    assert resolved.schema['properties']['legacy_record'] == history.fields['legacy_record']
    assert requests == ['GET', 'GET']
    assert journal.read(account, default)[2] == DESTINATION
    client._client().close()


def test_invalid_time_or_enrichment_is_rejected_before_a_partial_batch():
    from fulcra_media.v1 import build_media_record
    source = event()
    with pytest.raises(ValueError):
        build_media_record(event(end_time=source.start_time - timedelta(seconds=1)), DESTINATION)
    with pytest.raises(ValueError):
        build_media_record(event(external_ids={"bad": float("nan")}), DESTINATION)


@pytest.mark.parametrize("start,end,seconds", [
    ((2025, 11, 2, 1, 30, 0), (2025, 11, 2, 1, 30, 1), 3600),
    ((2025, 11, 2, 1, 55, 0), (2025, 11, 2, 1, 5, 1), 600),
    ((2025, 3, 9, 1, 30, 0), (2025, 3, 9, 3, 30, 0), 3600),
])
def test_duration_uses_elapsed_utc_time_across_dst_folds_and_gaps(start, end, seconds):
    from zoneinfo import ZoneInfo
    from fulcra_media.v1 import build_media_record
    zone = ZoneInfo("America/New_York")
    def local(parts):
        return datetime(*parts[:5], tzinfo=zone, fold=parts[5])
    row = build_media_record(event(start_time=local(start), end_time=local(end)), DESTINATION)
    assert row["duration_seconds"] == seconds


@pytest.mark.parametrize("change", [
    {"title": {"bad": "must not be stringified"}},
    {"note": {"bad": "must not be stringified"}, "timestamp_confidence": "low"},
    {"service": False},
    {"external_ids": ["bad"]},
])
def test_codec_rejects_schema_invalid_enrichment_before_any_metadata(change):
    from fulcra_media.v1 import build_media_record
    with pytest.raises(ValueError, match="schema"):
        build_media_record(event(**change), DESTINATION)
