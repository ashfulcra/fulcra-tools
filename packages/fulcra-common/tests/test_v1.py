"""V1 datatype contract, independent of live accounts and source content."""
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import pytest

from fulcra_common.client import BaseFulcraClient

EVENT = "Event/11111111-2222-4333-8444-555555555555"
OTHER_EVENT = "Event/11111111-2222-4333-8444-666666666666"
METRIC = "Metric/11111111-2222-4333-8444-555555555555"
NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)


def event(**kwargs):
    from fulcra_common.v1 import build_event_record
    args = dict(data_type=EVENT, source_id="example.source.1", start_time=NOW)
    args.update(kwargs)
    return build_event_record(**args)


def test_event_preserves_declared_fields_and_uses_explicit_v1_times():
    fields = {"note": "Synthetic note", "external_ids": {"example": "one"}}
    record = event(fields=fields, extra_sources=("fingerprint", "fingerprint", ""))
    assert record["start_time"] == record["end_time"] == "2026-01-02T12:00:00Z"
    assert record["note"] == fields["note"]
    assert record["external_ids"] == fields["external_ids"]
    fields["external_ids"]["example"] = "changed"
    assert record["external_ids"] == {"example": "one"}
    assert record["sources"] == ["example.source.1", "fingerprint"]
    assert not {"recorded_at", "metadata", "specversion", "data"} & record.keys()


def test_record_identity_is_repeatable_and_scoped_to_destination():
    from fulcra_common.v1 import build_event_record
    first = event()
    assert str(UUID(first["id"])) == first["id"]
    assert event()["id"] == first["id"]
    other = build_event_record(data_type=OTHER_EVENT, source_id="example.source.1",
                               start_time=NOW)
    assert other["id"] != first["id"]


def test_explicit_uuid_and_utc_range_are_preserved():
    original_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    record = event(record_id=original_id, end_time=NOW + timedelta(seconds=3))
    assert record["id"] == original_id
    assert record["end_time"] == "2026-01-02T12:00:03Z"


@pytest.mark.parametrize("kwargs", [
    {"start_time": NOW.replace(tzinfo=None)},
    {"end_time": NOW - timedelta(seconds=1)},
    {"source_id": ""},
    {"fields": {"id": "substituted"}},
    {"fields": {"sources": ["substituted"]}},
    {"fields": {"value": 3}},
    {"fields": {"value": float("nan")}},
    {"record_id": "not-a-uuid"},
])
def test_invalid_event_is_refused_before_io(kwargs):
    from fulcra_common.v1 import build_event_record
    args = dict(data_type=EVENT, source_id="example.source.1", start_time=NOW)
    args.update(kwargs)
    with pytest.raises(ValueError):
        build_event_record(**args)


@pytest.mark.parametrize("fields", [{"external_ids": {1: "numeric", "1": "text"}},
    {"external_ids": [{False: "boolean"}]}, {"note": "\ud800"}])
def test_nested_non_json_enrichment_is_rejected_before_it_can_be_coerced(fields):
    with pytest.raises(ValueError, match="JSON"):
        event(fields=fields)


@pytest.mark.parametrize("change", [{"sources": ["\ud800"]}, {"external_ids": {1: "numeric"}}])
def test_raw_record_json_is_proved_before_any_schema_or_transport_request(change, monkeypatch):
    from fulcra_common.v1 import V1DataClient
    def no_io(_):
        pytest.fail("invalid JSON reached HTTP")
    monkeypatch.setattr(BaseFulcraClient, "get_token", lambda _: "synthetic")
    row = event() | change
    with pytest.raises(ValueError, match="JSON"):
        V1DataClient(BaseFulcraClient(transport=httpx.MockTransport(no_io))).ingest(EVENT, [row])


@pytest.mark.parametrize("data_type", ["DurationAnnotation", "Event", "Metric/x",
                                        "Event/../token", METRIC])
def test_event_destination_requires_custom_v1_event(data_type):
    from fulcra_common.v1 import build_event_record
    with pytest.raises(ValueError):
        build_event_record(data_type=data_type, source_id="example", start_time=NOW)


def test_metric_value_and_unit_are_first_class_fields():
    from fulcra_common.v1 import build_metric_record
    record = build_metric_record(data_type=METRIC, source_id="example.metric.1",
                                 start_time=NOW, value=0, unit="percent",
                                 fields={"sensor": "synthetic"})
    assert record["value"] == 0
    assert record["unit"] == "percent"
    assert record["sensor"] == "synthetic"
    assert record["start_time"] == record["end_time"]


@pytest.mark.parametrize("value", [True, False, float("nan"), float("inf"),
                                    -float("inf"), "3", None])
def test_metric_refuses_non_numeric_or_non_finite_value(value):
    from fulcra_common.v1 import build_metric_record
    with pytest.raises(ValueError):
        build_metric_record(data_type=METRIC, source_id="example", start_time=NOW,
                            value=value)


def make_client(handler):
    from fulcra_common.v1 import V1DataClient

    class Client(BaseFulcraClient):
        def get_token(self):
            return "synthetic-token"

    return V1DataClient(Client(base_url="https://api.example", transport=httpx.MockTransport(handler)))


SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "format": "uuid"},
        "start_time": {"type": "string", "format": "date-time"},
        "end_time": {"type": "string", "format": "date-time"},
        "sources": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string", "format": "uuid"}},
        "note": {"type": "string"},
    },
    "required": ["id", "start_time", "end_time"],
}


def test_before_submit_runs_after_validation_and_can_prevent_the_post():
    effects = []

    def handler(request):
        effects.append(request.method)
        return httpx.Response(200, json=SCHEMA)

    def persist_dispatch_intent():
        effects.append("intent")
        raise RuntimeError("synthetic journal unavailable")

    with pytest.raises(RuntimeError, match="journal unavailable"):
        make_client(handler).ingest(EVENT, [event()], before_submit=persist_dispatch_intent)
    assert effects == ["GET", "intent"]


def test_invalid_payload_never_calls_before_submit():
    effects = []
    client = make_client(lambda _: httpx.Response(200, json=SCHEMA))
    with pytest.raises(ValueError, match="undeclared"):
        client.ingest(EVENT, [event(fields={"undeclared": "synthetic"})],
                      before_submit=lambda: effects.append("intent"))
    assert effects == []


def test_v1_post_redirect_cannot_repeat_a_single_dispatch_attempt():
    effects = []

    def handler(request):
        effects.append(request.url.path)
        if request.method == 'GET':
            return httpx.Response(200, json=SCHEMA)
        if request.url.path == f'/ingest/v1/record/{EVENT}':
            return httpx.Response(307, headers={'location': '/synthetic-redirect-target'})
        return httpx.Response(201, json={'upload_id': 'synthetic-receipt'})

    with pytest.raises(httpx.HTTPStatusError):
        make_client(handler).ingest(EVENT, [event()])
    assert effects == [f'/data/v1/catalog/{EVENT}/v1/schema', f'/ingest/v1/record/{EVENT}']


def test_ingest_validates_then_posts_to_full_destination_with_explicit_v1():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            assert request.url.path == f"/data/v1/catalog/{EVENT}/v1/schema"
            return httpx.Response(200, json=SCHEMA)
        assert request.url.path == f"/ingest/v1/record/{EVENT}"
        assert request.url.params["api_version"] == "v1"
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.headers["content-type"] == "application/x-jsonl"
        lines = request.content.splitlines()
        assert len(lines) == 2
        return httpx.Response(201, json={"upload_id": "synthetic-receipt"})

    result = make_client(handler).ingest(EVENT, [event(fields={"note": "one"}),
                                               event(source_id="example.source.2", fields={"note": "two"})])
    assert result == {"upload_id": "synthetic-receipt"}
    assert [r.method for r in requests] == ["GET", "POST"]


@pytest.mark.parametrize("change", [
    {"undeclared": "private-value-must-not-appear-in-errors"},
    {"start_time": "not-a-time"},
    {"note": {"private": "private-value-must-not-appear-in-errors"}},
    {"tags": ["not-a-uuid"]},
])
def test_invalid_batch_never_partially_uploads_or_leaks_payload(change):
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=SCHEMA)

    second = event()
    second.update(change)
    with pytest.raises(ValueError) as error:
        make_client(handler).ingest(EVENT, [event(), second])
    assert calls == ["GET"]
    assert "record 1" in str(error.value)
    assert "private-value" not in str(error.value)


def test_unreadable_schema_cannot_fall_back_to_legacy_or_upload():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        make_client(handler).ingest(EVENT, [event()])
    assert calls == ["GET"]


def test_uncertain_upload_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=SCHEMA)
        raise httpx.ReadTimeout("synthetic lost response", request=request)

    with pytest.raises(httpx.ReadTimeout):
        make_client(handler).ingest(EVENT, [event()])
    assert calls == ["GET", "POST"]


def test_conflicting_same_id_payloads_in_one_batch_are_refused():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=SCHEMA)

    with pytest.raises(ValueError, match="conflicting"):
        make_client(handler).ingest(EVENT, [event(fields={"note": "one"}),
                                           event(fields={"note": "two"})])
    assert calls == ["GET"]


def test_same_id_boolean_and_numeric_payloads_are_conflicting_json_types():
    schema = {**SCHEMA, "properties": {**SCHEMA["properties"], "note": {"type": ["boolean", "number"]}}}
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=schema) if request.method == "GET" else httpx.Response(201, json={"upload_id": "synthetic"})

    with pytest.raises(ValueError, match="conflicting"):
        make_client(handler).ingest(EVENT, [event(fields={"note": 0}), event(fields={"note": False})])
    assert calls == ["GET"]


def test_missing_receipt_is_uncertain_not_verified_success():
    def handler(request):
        return httpx.Response(200, json=SCHEMA) if request.method == "GET" else httpx.Response(201, json={})

    with pytest.raises(RuntimeError, match="reconcile"):
        make_client(handler).ingest(EVENT, [event()])


def test_empty_batch_does_no_io():
    def handler(request):
        raise AssertionError("empty input must not contact API")

    assert make_client(handler).ingest(EVENT, []) is None


def test_readback_queries_exact_destination_and_id_in_captured_account():
    import json
    expected = event()

    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/data/v1/records"
        assert request.url.params["fulcra_userid"] == "synthetic-account"
        assert request.url.params["q"] == '{__name__="' + EVENT + '",id="' + expected["id"] + '"}'
        return httpx.Response(200, text=json.dumps(expected) + "\n")

    assert make_client(handler).get_record(EVENT, expected["id"], account_id="synthetic-account") == expected


@pytest.mark.parametrize("body", ["not JSON", "[]", "{}", "{}\n{}", '{"id":"wrong"}'])
def test_unreadable_or_ambiguous_readback_is_never_clean_missing(body):
    def handler(request):
        return httpx.Response(200, text=body)

    with pytest.raises(ValueError, match="readback"):
        make_client(handler).get_record(EVENT, event()["id"], account_id="synthetic-account")


def test_empty_successful_id_read_is_missing_not_verified():
    def handler(request):
        return httpx.Response(200, text="\n")

    assert make_client(handler).get_record(EVENT, event()["id"], account_id="synthetic-account") is None


def test_readback_field_comparison_normalizes_time_but_preserves_enrichment():
    from fulcra_common.v1 import record_matches
    expected = event(fields={"note": "synthetic", "external_ids": {"x": "one"}})
    actual = {**expected, "start_time": "2026-01-02T07:00:00-05:00", "server_field": 1}
    assert record_matches(actual, expected)
    assert not record_matches(None, expected)
    assert not record_matches({**actual, "external_ids": {"x": "two"}}, expected)
    assert not record_matches({**actual, "end_time": "bad"}, expected)
    assert not record_matches({**actual, "start_time": "2026-01-02T12:00:00"}, expected)


def test_readback_cannot_confuse_boolean_with_number_in_nested_fields():
    from fulcra_common.v1 import record_matches
    expected = event(fields={"payload": {"values": [0]}})
    assert not record_matches({**expected, "payload": {"values": [False]}}, expected)
    assert record_matches({**expected, "payload": {"values": [0.0]}}, expected)


def test_batch_id_readback_uses_bounded_or_groups_and_preserves_missing_ids():
    import json
    from uuid import UUID
    records = [event(record_id=str(UUID(int=i + 1))) for i in range(70)]
    calls = []

    def handler(request):
        query = request.url.params["q"]
        calls.append(query)
        # Live API rejects a PromQL binary OR of vectors. Its supported
        # grouping is inside ONE matcher selector's braces.
        assert query.count("{") == query.count("}") == 1
        selectors = query[1:-1].split(" or ")
        assert len(selectors) <= 32
        selected = []
        for selector in selectors:
            assert selector.startswith('__name__="' + EVENT + '",id="')
            identifier = selector.split('id="')[1].split('"')[0]
            # The last requested record is genuinely absent.
            selected.extend(r for r in records[:-1] if r["id"] == identifier)
        return httpx.Response(200, text="\n".join(json.dumps(r) for r in selected))

    result = make_client(handler).get_records(EVENT, [r["id"] for r in records], account_id="synthetic-account")
    assert len(calls) == 3
    assert result == {r["id"]: r for r in records[:-1]}


def test_batch_id_readback_rejects_duplicates_and_unrequested_records():
    import json
    first, other = event(), event(source_id="other")
    for rows in [[first, first], [other]]:
        c = make_client(lambda _: httpx.Response(200, text="\n".join(json.dumps(r) for r in rows)))
        with pytest.raises(ValueError, match="readback"):
            c.get_records(EVENT, [first["id"]], account_id="synthetic-account")


def test_batch_invalid_identity_is_refused_before_any_io():
    def handler(request):
        raise AssertionError("invalid batch must not contact API")
    with pytest.raises(ValueError):
        make_client(handler).get_records(EVENT, [event()["id"], 'bad"selector'], account_id="synthetic-account")
    assert make_client(handler).get_records(EVENT, [], account_id="synthetic-account") == {}


def test_source_lookup_is_destination_account_and_window_scoped():
    import json
    expected = event(extra_sources=("synthetic.fingerprint",))

    def handler(request):
        query = request.url.params["q"]
        assert query == '{__name__="' + EVENT + '",source="synthetic.fingerprint"}[1200s] @ 1767355800.0'
        assert request.url.params["fulcra_userid"] == "synthetic-account"
        return httpx.Response(200, text=json.dumps(expected))

    found = make_client(handler).get_source_records(EVENT, ["synthetic.fingerprint"],
        start=NOW - timedelta(minutes=10), end=NOW + timedelta(minutes=10), account_id="synthetic-account")
    assert found == [expected]


def test_saturated_source_lookup_is_unknown_not_complete():
    import json
    record = event(extra_sources=("fingerprint",))
    def handler(request):
        return httpx.Response(200, text="\n".join(json.dumps(record) for _ in range(4000)))
    with pytest.raises(RuntimeError, match="coverage"):
        make_client(handler).get_source_records(EVENT, ["fingerprint"], start=NOW,
            end=NOW + timedelta(hours=1), account_id="synthetic-account")


def test_source_lookup_refuses_unrelated_or_malformed_rows():
    import json
    def handler(request):
        return httpx.Response(200, text=json.dumps(event()))
    with pytest.raises(ValueError, match="readback"):
        make_client(handler).get_source_records(EVENT, ["absent-fingerprint"], start=NOW,
            end=NOW + timedelta(hours=1), account_id="synthetic-account")


def test_latest_preview_uses_native_bounded_query_in_captured_account():
    import json
    expected = event()
    def handler(request):
        assert request.url.params["q"] == 'last_over_time({__name__="' + EVENT + '"}[30d])'
        assert request.url.params["fulcra_userid"] == "synthetic-account"
        return httpx.Response(200, text=json.dumps(expected))
    assert make_client(handler).get_latest(EVENT, account_id="synthetic-account") == expected


@pytest.mark.parametrize("body", ["not JSON", "{}", "[]", "{}\n{}"])
def test_latest_preview_unknown_response_is_not_clean_empty(body):
    def handler(request):
        return httpx.Response(200, text=body)
    with pytest.raises(ValueError, match="preview"):
        make_client(handler).get_latest(EVENT, account_id="synthetic-account")


def test_malformed_destination_schema_is_sanitized_before_upload():
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"properties": {"note": {"type": "private-schema-value"}}})

    with pytest.raises(ValueError) as error:
        make_client(handler).ingest(EVENT, [event()])
    assert "private-schema-value" not in str(error.value)
    assert calls == ["GET"]


def test_shared_identity_lookup_matches_the_existing_builder_mapping():
    from fulcra_common.v1 import record_id_for
    assert record_id_for(EVENT, "example.source.1") == event()["id"]
    assert record_id_for(METRIC, "example.source.1") != event()["id"]
    for destination, source in [("Event", "example"), (EVENT, ""), (EVENT, None)]:
        with pytest.raises(ValueError):
            record_id_for(destination, source)


@pytest.mark.parametrize("identifier", [None, "bad", 7, True, "AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE"])
def test_ingest_requires_explicit_canonical_identity_even_when_schema_does_not(identifier):
    # Native schemas allow the server to generate IDs. Recovery cannot reconcile
    # such writes, so our shared transport must impose its stronger contract.
    schema = {**SCHEMA, "required": ["start_time", "end_time"],
              "properties": {**SCHEMA["properties"], "id": {}}}
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=schema)
    record = event()
    if identifier is None:
        record.pop("id")
    else:
        record["id"] = identifier
    with pytest.raises(ValueError, match="explicit canonical UUID"):
        make_client(handler).ingest(EVENT, [record])
    assert "POST" not in calls


TOMBSTONE_SCHEMA = {
    "type": "object", "required": ["record_id"],
    "properties": {"id": {"type": "string", "format": "uuid"},
                   "record_id": {"type": "string", "format": "uuid"},
                   "data_type": {"type": "string"}},
}
ACTION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.mark.parametrize("destination", [EVENT, METRIC])
def test_native_delete_uses_v1_schema_base_type_and_stable_action_scoped_ids(destination):
    import json
    from uuid import uuid5, NAMESPACE_URL
    calls, batches = [], []
    identifier = event()["id"]
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            assert request.url.path == "/data/v1/catalog/DeletedRecord/v1/schema"
            return httpx.Response(200, json=TOMBSTONE_SCHEMA)
        assert request.url.path == "/ingest/v1/record/DeletedRecord"
        assert request.url.params["api_version"] == "v1"
        assert request.headers["content-type"] == "application/x-jsonl"
        batch = [json.loads(line) for line in request.content.splitlines()]
        assert batch == [{"id": str(uuid5(NAMESPACE_URL,
            f"fulcra-tools:v1:delete:{ACTION}:{destination}:{identifier}")),
            "record_id": identifier, "data_type": destination.split("/")[0]}]
        batches.append(batch)
        return httpx.Response(201, json={"upload_id": "synthetic-submit-only"})
    client = make_client(handler)
    for _ in range(2):
        assert client.delete_records(destination, [identifier, identifier], action_id=ACTION) == {
            "upload_id": "synthetic-submit-only"}
    assert batches[0] == batches[1]
    assert calls == ["GET", "POST", "GET", "POST"]


@pytest.mark.parametrize("destination, identifiers, action", [
    ("DurationAnnotation", ["bad"], ACTION),
    (EVENT, [event()["id"], "bad"], ACTION),
    (EVENT, [event()["id"]], "bad"),
    (EVENT, [event()["id"]], ACTION.upper()),
])
def test_native_delete_invalid_input_never_submits_any_target(destination, identifiers, action):
    def handler(request):
        raise AssertionError("invalid deletion intent must not contact the API")
    with pytest.raises(ValueError):
        make_client(handler).delete_records(destination, identifiers, action_id=action)


def test_native_delete_unknown_schema_and_lost_post_never_fall_back_or_retry():
    identifier = event()["id"]
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=TOMBSTONE_SCHEMA)
        raise httpx.ReadTimeout("synthetic uncertain delete", request=request)
    with pytest.raises(httpx.ReadTimeout):
        make_client(handler).delete_records(EVENT, [identifier], action_id=ACTION)
    assert calls == ["GET", "POST"]
    calls.clear()
    def unavailable(request):
        calls.append(request.method)
        return httpx.Response(503)
    with pytest.raises(httpx.HTTPStatusError):
        make_client(unavailable).delete_records(EVENT, [identifier], action_id=ACTION)
    assert calls == ["GET"]


def test_empty_native_delete_does_no_io_and_does_not_open_generic_native_ingest():
    def handler(request):
        raise AssertionError("empty deletion must not contact the API")
    client = make_client(handler)
    assert client.delete_records(EVENT, [], action_id=ACTION) is None
    with pytest.raises(ValueError):
        client.ingest("DeletedRecord", [])


def test_preflight_validation_snapshots_the_batch_without_submitting():
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=SCHEMA)
    original = event(fields={"note": "synthetic-before-mutation"})
    batch = make_client(handler).validate_records(EVENT, [original])
    original["note"] = "changed"
    assert batch[0]["note"] == "synthetic-before-mutation"
    assert calls == ["GET"]


@pytest.mark.parametrize('expected,actual,match', [
    ('2026-01-02T12:00:00.123456789Z', '2026-01-02T12:00:00.123456788Z', False),
    ('2026-01-02T12:00:00.000000001Z', '2026-01-02T12:00:00Z', False),
    ('2026-01-02T12:00:00.123456789Z', '2026-01-02T07:00:00.123456789-05:00', True),
    ('2026-01-02T12:00:00.123456Z', '2026-01-02T12:00:00.123456000Z', True),
    ('2026-01-02T12:00:00.10000000000000000001Z', '2026-01-02T12:00:00.1Z', False),
])
def test_exact_readback_never_truncates_fractional_time_precision(expected, actual, match):
    from fulcra_common.v1 import record_matches
    assert record_matches({'start_time': actual}, {'start_time': expected}) is match


@pytest.mark.parametrize('malformed,normalized', [
    ('2026-01-02T12:00:00+00:60', '2026-01-02T11:00:00Z'),
    ('2026-01-02T12:00:00+01:99', '2026-01-02T09:21:00Z'),
    ('2026-01-02T12:00:00-00:60', '2026-01-02T13:00:00Z'),
])
def test_malformed_offset_cannot_satisfy_exact_readback(malformed, normalized):
    from fulcra_common.v1 import record_matches
    assert not record_matches({'start_time': malformed}, {'start_time': normalized})
    assert not record_matches({'start_time': normalized}, {'start_time': malformed})


def test_validation_never_fetches_schema_references_outside_the_served_snapshot(monkeypatch):
    import io
    import urllib.request
    calls, remote = [], []
    def fetch(request):
        remote.append(request)
        return io.BytesIO(b'{"$schema":"https://json-schema.org/draft/2020-12/schema","type":"string"}')
    monkeypatch.setattr(urllib.request, 'urlopen', fetch)
    schema = {**SCHEMA, 'properties': {**SCHEMA['properties'],
        'note': {'$ref': 'https://synthetic.invalid/unfrozen-note-schema'}}}
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=schema) if request.method == 'GET' else httpx.Response(201, json={'upload_id': 'synthetic'})
    with pytest.raises(ValueError, match='reference'):
        make_client(handler).ingest(EVENT, [event(fields={'note': 'Synthetic note'})])
    assert calls == ['GET'] and remote == []

@pytest.mark.parametrize("reader", ["exact", "source", "latest"])
@pytest.mark.parametrize("mode", ["partial_status", "content_range", "link", "cursor", "total", "other_owner"])
def test_record_readers_cannot_report_clean_empty_for_partial_success(reader, mode):
    headers = {"Content-Type": "application/x-jsonl"}
    if mode == "content_range":
        headers["Content-Range"] = "records 0-0/2"
    if mode == "link":
        headers["Link"] = '<https://example.com/next>; rel="next"'
    if mode == "cursor":
        headers["X-Next-Cursor"] = "synthetic"
    if mode == "total":
        headers["X-Total-Count"] = "1"
    if mode == "other_owner":
        headers["X-Fulcra-UserID"] = "other-account"
    client = make_client(lambda _: httpx.Response(206 if mode == "partial_status" else 200,
        content=b"", headers=headers))
    with pytest.raises(ValueError):
        if reader == "exact":
            client.get_record(EVENT, event()["id"], account_id="synthetic-account")
        elif reader == "source":
            client.get_source_records(EVENT, ["synthetic"], start=NOW,
                end=NOW + timedelta(hours=1), account_id="synthetic-account")
        else:
            client.get_latest(EVENT, account_id="synthetic-account")

@pytest.mark.parametrize("reader", ["exact", "source", "latest"])
@pytest.mark.parametrize("body", [b"\x0b", b"\x0c", b"\xc2\xa0", b"\xef\xbb\xbf", b"\xff",
    b'{"id":"synthetic","x":NaN}', b'{"id":"synthetic","x":"\\ud800"}',
    b'{"id":"synthetic","x":1,"\\u0078":1}', b'{}\nmalformed-private-tail'])
def test_record_readers_reject_malformed_framing_and_lossy_json_statically(reader, body):
    import json
    if body.startswith(b'{"id":"synthetic",'):
        # Keep ID/source/time valid so malformed JSON metadata cannot be
        # masked by unrelated identity or preview-validation failures.
        baseline = json.dumps(event(extra_sources=("synthetic",))).encode()
        body = baseline[:-1] + b"," + body.split(b",", 1)[1]
    client = make_client(lambda _: httpx.Response(200, content=body,
        headers={"Content-Type": "application/x-jsonl"}))
    with pytest.raises(ValueError) as error:
        if reader == "exact":
            client.get_record(EVENT, event()["id"], account_id="synthetic-account")
        elif reader == "source":
            client.get_source_records(EVENT, ["synthetic"], start=NOW,
                end=NOW + timedelta(hours=1), account_id="synthetic-account")
        else:
            client.get_latest(EVENT, account_id="synthetic-account")
    assert "malformed-private-tail" not in str(error.value)


@pytest.mark.parametrize("mode", ["redirect", "wrong_origin", "wrong_path", "wrong_query", "history"])
def test_exact_readback_rejects_response_retargeting(mode, monkeypatch):
    client = make_client(lambda _: httpx.Response(302, headers={"Location": "https://example.com/other"})
        if mode == "redirect" else httpx.Response(200, content=b""))
    http = client.client._client()
    original = http.get
    # httpx correctly replaces MockTransport request/history attributes. Inject
    # at the completed client boundary to test unexpected returned responses.
    def get(*args, **kwargs):
        response = original(*args, **kwargs)
        if mode == "redirect":
            return response
        if mode == "history":
            response.history = [httpx.Response(301, request=response.request)]
        else:
            url = response.url
            if mode == "wrong_origin":
                url = url.copy_with(host="other.example.com")
            elif mode == "wrong_path":
                url = url.copy_with(path="/other")
            else:
                url = url.copy_set_param("q", "other-query")
            response.request = httpx.Request("GET", url)
        return response
    monkeypatch.setattr(http, "get", get)
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        client.get_record(EVENT, event()["id"], account_id="synthetic-account")


def test_record_readback_jsonl_ascii_blank_and_consistent_headers_are_valid():
    def handler(request):
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["accept"] == "application/x-jsonl"
        return httpx.Response(200, content=b" \t\r\n\t\n",
            headers={"X-Total": "0", "X-Fulcra-UserID": "synthetic-account"})
    client = make_client(handler)
    assert client.get_record(EVENT, event()["id"], account_id="synthetic-account") is None


def test_record_readback_keeps_existing_client_redirect_policy_unchanged():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(302, headers={"Location": "/other"})
    client = make_client(handler)
    client.client._client().follow_redirects = True
    with pytest.raises(httpx.HTTPStatusError):
        client.get_record(EVENT, event()["id"], account_id="synthetic-account")
    assert calls == ["/data/v1/records"]
    assert client.client._client().follow_redirects is True

@pytest.mark.parametrize("mode", ["bytes", "depth", "nodes", "lines"])
def test_record_readback_buffered_parse_limits_refuse_without_clean_absence(mode):
    import json
    if mode == "bytes":
        body = b" " * (32 * 1024 * 1024 + 1)
    elif mode == "depth":
        body = ('{"id":"' + event()["id"] + '","nested":').encode() + b"[" * 65 + b"0" + b"]" * 65 + b"}"
    elif mode == "nodes":
        body = json.dumps({"id": event()["id"], "nested": [0] * 100001}).encode()
    else:
        body = b"\n" * 4097
    with pytest.raises(ValueError, match="readback"):
        make_client(lambda _: httpx.Response(200, content=body)).get_record(EVENT,
            event()["id"], account_id="synthetic-account")

@pytest.mark.parametrize("reader", ["exact", "source", "latest"])
@pytest.mark.parametrize("cut", [10, -8])
def test_record_readers_never_trust_eager_decoding_of_truncated_gzip(reader, cut):
    import gzip
    import json
    record = event(extra_sources=("synthetic",))
    wire = gzip.compress(json.dumps(record).encode())[:cut]
    client = make_client(lambda _: httpx.Response(200, stream=httpx.ByteStream(wire),
        headers={"Content-Encoding": "gzip", "Content-Length": str(len(wire))}))
    with pytest.raises(ValueError):
        if reader == "exact":
            client.get_record(EVENT, record["id"], account_id="synthetic-account")
        elif reader == "source":
            client.get_source_records(EVENT, ["synthetic"], start=NOW,
                end=NOW + timedelta(hours=1), account_id="synthetic-account")
        else:
            client.get_latest(EVENT, account_id="synthetic-account")


PIN_ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


@pytest.fixture
def exact_schema_account(monkeypatch):
    # Account verification is an SDK dependency; never inspect real credentials.
    from fulcra_common.data_types import FulcraAPI
    monkeypatch.setattr(FulcraAPI, "get_fulcra_userid", lambda _: PIN_ACCOUNT)


@pytest.mark.parametrize("destination", [EVENT, METRIC])
def test_exact_schema_uses_one_fresh_full_schema_for_validation_and_submit(destination, exact_schema_account):
    import copy
    import json
    from fulcra_common.v1 import build_metric_record
    schema = copy.deepcopy(SCHEMA)
    schema["description"] = "Synthetic full schema"
    schema["x-numeric"] = 1
    record = event(fields={"note": "original"})
    if destination == METRIC:
        schema["properties"]["value"] = {"type": "number"}
        record = build_metric_record(data_type=METRIC, source_id="synthetic.metric", start_time=NOW, value=1)
    expected = dict(reversed(list(schema.items())))
    expected["x-numeric"] = 1.0
    calls, bodies = [], []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            assert request.url.path == f"/data/v1/catalog/{destination}/v1/schema"
            assert request.headers["accept-encoding"] == "identity"
            assert request.headers["cache-control"] == "no-store"
            return httpx.Response(200, json=schema)
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={"upload_id": "synthetic-receipt"})
    client = make_client(handler)
    batch = client.validate_records(destination, [record], expected_schema=expected, account_id=PIN_ACCOUNT)
    assert batch == [record] and batch[0] is not record
    assert client.ingest(destination, [record], expected_schema=expected, account_id=PIN_ACCOUNT) == {"upload_id": "synthetic-receipt"}
    assert calls == ["GET", "GET", "POST"] and bodies == [record]


@pytest.mark.parametrize("field,expected,served", [
    ("description", "original", "changed"),
    ("required", ["id", "start_time", "end_time"], ["id"]),
    ("x-nested", {"const": False}, {"const": 0}),
    ("x-nested", {"const": True}, {"const": 1}),
    ("x-nested", {"const": None}, {}),
    ("x-nested", ["one", "two"], ["two", "one"]),
    ("properties", SCHEMA["properties"], {**SCHEMA["properties"], "extra": {"type": "string"}}),
])
def test_exact_schema_full_drift_refuses_before_callback_or_post(field, expected, served, exact_schema_account):
    effects = []
    def handler(request):
        effects.append(request.method)
        return httpx.Response(200, json={**SCHEMA, field: served})
    with pytest.raises(ValueError) as error:
        make_client(handler).ingest(EVENT, [event()], expected_schema={**SCHEMA, field: expected},
            account_id=PIN_ACCOUNT, before_submit=lambda: effects.append("intent"))
    assert effects == ["GET"]
    assert "original" not in str(error.value) and "changed" not in str(error.value)


def test_exact_schema_ingest_rechecks_after_preliminary_validation(exact_schema_account):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=SCHEMA if len(calls) == 1 else {**SCHEMA, "description": "changed"})
    client = make_client(handler)
    assert client.validate_records(EVENT, [event()], expected_schema=SCHEMA, account_id=PIN_ACCOUNT)
    with pytest.raises(ValueError):
        client.ingest(EVENT, [event()], expected_schema=SCHEMA, account_id=PIN_ACCOUNT)
    assert calls == ["GET", "GET"]


@pytest.mark.parametrize("mutation_at", ["auth", "get", "before_submit"])
def test_exact_schema_snapshots_inputs_before_external_calls(mutation_at, exact_schema_account, monkeypatch):
    import copy
    import json
    expected = copy.deepcopy(SCHEMA)
    expected["properties"]["note"] = {"type": ["number", "boolean"]}
    served = copy.deepcopy(expected)
    record = event(fields={"note": 0})
    effects, bodies = [], []
    def mutate():
        expected["properties"]["note"] = {"const": False}
        record["note"] = False
    def handler(request):
        effects.append(request.method)
        if request.method == "GET":
            if mutation_at == "get":
                mutate()
            return httpx.Response(200, json=served)
        bodies.append(request.content)
        return httpx.Response(201, json={"upload_id": "synthetic"})
    client = make_client(handler)
    if mutation_at == "auth":
        def token():
            mutate()
            return "synthetic-token"
        monkeypatch.setattr(client.client, "get_token", token)
    def callback():
        effects.append("intent")
        if mutation_at == "before_submit":
            mutate()
    client.ingest(EVENT, [record], expected_schema=expected, account_id=PIN_ACCOUNT, before_submit=callback)
    assert effects == ["GET", "intent", "POST"]
    assert type(json.loads(bodies[0])["note"]) is int and json.loads(bodies[0])["note"] == 0


def invalid_exact_schema(kind):
    import copy
    from collections import UserDict
    value = copy.deepcopy(SCHEMA)
    if kind == "mapping":
        return UserDict(value)
    if kind == "boolean":
        return True
    if kind == "key":
        value["x"] = {1: "private-value"}
    elif kind == "unicode":
        value["x"] = "\ud800"
    elif kind == "nan":
        value["x"] = float("nan")
    elif kind == "cycle":
        value["x"] = value
    elif kind == "bytes":
        value["x"] = "x" * (1024 * 1024)
    elif kind == "depth":
        nested = 0
        for _ in range(65):
            nested = [nested]
        value["x"] = nested
    elif kind == "nodes":
        value["x"] = [0] * 100001
    elif kind == "draft":
        value["properties"]["note"] = {"type": "private-value"}
    elif kind == "properties":
        value["properties"] = []
    return value


@pytest.mark.parametrize("method", ["validate_records", "ingest"])
@pytest.mark.parametrize("kind", ["mapping", "boolean", "key", "unicode", "nan", "cycle", "bytes", "depth", "nodes", "draft", "properties"])
def test_exact_schema_invalid_expected_refuses_locally_even_when_empty(method, kind, monkeypatch):
    client = make_client(lambda _: pytest.fail("invalid pin reached HTTP"))
    monkeypatch.setattr(client.client, "get_token", lambda: pytest.fail("invalid pin reached auth"))
    monkeypatch.setattr(client.client, "_client", lambda: pytest.fail("invalid pin reached client"))
    with pytest.raises(ValueError) as error:
        getattr(client, method)(EVENT, [], expected_schema=invalid_exact_schema(kind), account_id=PIN_ACCOUNT)
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize("account", [None, "", " ", False, 7])
def test_exact_schema_requires_explicit_account_before_io(account, monkeypatch):
    client = make_client(lambda _: pytest.fail("invalid account reached HTTP"))
    monkeypatch.setattr(client.client, "get_token", lambda: pytest.fail("invalid account reached auth"))
    with pytest.raises(ValueError):
        client.ingest(EVENT, [], expected_schema=SCHEMA, account_id=account)


def test_exact_schema_empty_is_local_without_claiming_remote_equality(monkeypatch):
    import copy
    client = make_client(lambda _: pytest.fail("empty pin reached HTTP"))
    monkeypatch.setattr(client.client, "get_token", lambda: pytest.fail("empty pin reached auth"))
    expected = copy.deepcopy(SCHEMA)
    expected["required"] = tuple(expected["required"])
    assert client.validate_records(EVENT, [], expected_schema=expected, account_id=PIN_ACCOUNT) == []
    assert client.ingest(EVENT, [], expected_schema=expected, account_id=PIN_ACCOUNT,
        before_submit=lambda: pytest.fail("empty pin reached callback")) is None


def test_explicit_none_keeps_legacy_schema_read_and_ignores_account(monkeypatch):
    from fulcra_common.data_types import FulcraAPI
    monkeypatch.setattr(FulcraAPI, "get_fulcra_userid", lambda _: pytest.fail("default added account proof"))
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            assert request.headers["accept-encoding"] != "identity"
            return httpx.Response(200, json=SCHEMA, headers={"Content-Range": "synthetic"})
        return httpx.Response(201, json={"upload_id": "synthetic"})
    client = make_client(handler)
    assert client.validate_records(EVENT, [event()], expected_schema=None, account_id=False)
    assert client.ingest(EVENT, [event()], expected_schema=None, account_id=False) == {"upload_id": "synthetic"}
    assert calls == ["GET", "GET", "POST"]


@pytest.mark.parametrize("reference", ["local", "remote"])
def test_exact_schema_validates_same_snapshot_with_no_remote_reference_fetch(reference, exact_schema_account, monkeypatch):
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("remote schema fetched"))
    schema = {**SCHEMA, "$defs": {"note": {"type": "number"}},
        "properties": {**SCHEMA["properties"], "note": {"$ref": "#/$defs/note" if reference == "local" else "https://synthetic.invalid/schema"}}}
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=schema)
    client = make_client(handler)
    if reference == "local":
        assert client.validate_records(EVENT, [event(fields={"note": 1})], expected_schema=schema, account_id=PIN_ACCOUNT)
        with pytest.raises(ValueError, match="validation failed"):
            client.validate_records(EVENT, [event(fields={"note": "private-value"})], expected_schema=schema, account_id=PIN_ACCOUNT)
        assert calls == ["GET", "GET"]
    else:
        with pytest.raises(ValueError, match="reference"):
            client.ingest(EVENT, [event(fields={"note": 1})], expected_schema=schema, account_id=PIN_ACCOUNT)
        assert calls == ["GET"]


@pytest.mark.parametrize("outcome", ["timeout", "missing_receipt"])
def test_exact_schema_submission_keeps_uncertain_outcome_without_retry(outcome, exact_schema_account):
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json=SCHEMA)
        if outcome == "timeout":
            raise httpx.ReadTimeout("synthetic uncertain outcome", request=request)
        return httpx.Response(201, json={})
    with pytest.raises((httpx.ReadTimeout, RuntimeError)):
        make_client(handler).ingest(EVENT, [event()], expected_schema=SCHEMA, account_id=PIN_ACCOUNT)
    assert calls == ["GET", "POST"]


def test_default_receipt_contract_is_not_strengthened_by_observed_mode():
    # Opt-in observed receipts require a string; existing callers retain truthy-ID semantics.
    def handler(request):
        return httpx.Response(200, json=SCHEMA) if request.method == "GET" else httpx.Response(201, json={"upload_id": 7})
    assert make_client(handler).ingest(EVENT, [event()]) == {"upload_id": 7}
