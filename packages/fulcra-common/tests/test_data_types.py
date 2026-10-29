"""Account-bound v1 destination resolution and durable creation recovery."""
import base64
import json

import httpx
import pytest

from fulcra_common.client import BaseFulcraClient

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
TYPE_ID = "Event/11111111-2222-4333-8444-555555555555"
SECOND_ID = "Event/11111111-2222-4333-8444-666666666666"
FIELDS = {"note": {"type": ["string", "null"]}}


@pytest.fixture
def served_schema_account(monkeypatch):
    from fulcra_common.data_types import FulcraAPI
    monkeypatch.setattr(FulcraAPI, "get_fulcra_userid", lambda _: ACCOUNT)


def test_public_served_schema_reader_is_account_bound_and_preserves_full_dictionary(served_schema_account):
    from fulcra_common.data_types import read_served_schema
    calls = []
    schema = {"description": "Synthetic full document", "x-extra": {"const": False}}
    def handler(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == f"/data/v1/catalog/{TYPE_ID}/v1/schema"
        assert not request.url.query
        assert request.headers["accept-encoding"] == "identity"
        assert request.headers["cache-control"] == "no-store"
        assert request.extensions["timeout"] == dict.fromkeys(["connect", "read", "write", "pool"], 2.5)
        return httpx.Response(200, json=schema)
    assert read_served_schema(client=client(handler), account_id=ACCOUNT, data_type=TYPE_ID, timeout=2.5) == schema
    assert len(calls) == 1


@pytest.mark.parametrize("account,destination", [(None, TYPE_ID), ("", TYPE_ID), (" ", TYPE_ID),
    (False, TYPE_ID), (7, TYPE_ID), (ACCOUNT, "Event"), (ACCOUNT, "DeletedRecord")])
def test_public_served_schema_invalid_scope_refuses_before_auth(account, destination, monkeypatch):
    from fulcra_common.data_types import read_served_schema
    current = client(lambda _: pytest.fail("invalid scope reached HTTP"))
    monkeypatch.setattr(current, "get_token", lambda: pytest.fail("invalid scope reached auth"))
    with pytest.raises(ValueError):
        read_served_schema(client=current, account_id=account, data_type=destination)


def test_public_served_schema_rechecks_token_account_for_each_call(served_schema_account, monkeypatch):
    from fulcra_common.data_types import FulcraAPI, read_served_schema
    calls = []
    current = client(lambda request: calls.append(request) or httpx.Response(200, json={}))
    assert read_served_schema(client=current, account_id=ACCOUNT, data_type=TYPE_ID) == {}
    monkeypatch.setattr(FulcraAPI, "get_fulcra_userid", lambda _: OTHER_ACCOUNT)
    with pytest.raises(ValueError, match="account changed"):
        read_served_schema(client=current, account_id=ACCOUNT, data_type=TYPE_ID)
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["duplicate", "precision", "invalid_utf8", "unicode", "nonfinite", "bytes", "depth", "nodes", "not_object",
    "partial", "encoded", "owner", "redirect", "origin", "path", "query", "history"])
def test_public_served_schema_rejects_unreadable_or_mismatched_proof(mode, served_schema_account, monkeypatch):
    from fulcra_common.data_types import read_served_schema
    bodies = {"duplicate": b'{"x":1,"\\u0078":1}', "precision": b'{"x":1.0000000000000001}',
        "invalid_utf8": b'\xff', "unicode": b'{"x":"\\ud800"}', "nonfinite": b'{"x":NaN}',
        "not_object": b'[]'}
    body = bodies.get(mode, b'{}')
    if mode == "bytes":
        body = b' ' * (1024 * 1024 + 1)
    elif mode == "depth":
        body = b'{"x":' + b'[' * 65 + b'0' + b']' * 65 + b'}'
    elif mode == "nodes":
        body = json.dumps({"x": [0] * 100001}).encode()
    calls = []
    def handler(request):
        calls.append(request)
        headers = {"partial": {"Content-Range": "synthetic"}, "encoded": {"Content-Encoding": "gzip"},
            "owner": {"X-Fulcra-UserID": OTHER_ACCOUNT}, "redirect": {"Location": "/other"}}.get(mode, {})
        # Set encoding after construction: payload is already decoded at this boundary.
        response = httpx.Response(302 if mode == "redirect" else 200, content=body,
            headers={} if mode == "encoded" else headers)
        if mode == "encoded":
            response.headers.update(headers)
        return response
    current = client(handler)
    http = current._client()
    original_get = http.get
    def get(*args, **kwargs):
        response = original_get(*args, **kwargs)
        if mode in {"origin", "path", "query"}:
            url = response.url
            url = url.copy_with(host="other.example") if mode == "origin" else url.copy_with(path="/other") if mode == "path" else url.copy_set_param("x", "other")
            response.request = httpx.Request("GET", url)
        elif mode == "history":
            response.history = [httpx.Response(301, request=response.request)]
        return response
    monkeypatch.setattr(http, "get", get)
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        read_served_schema(client=current, account_id=ACCOUNT, data_type=TYPE_ID)
    assert len(calls) == 1


@pytest.mark.parametrize("first,second,third", [
    ("data_types", "v1", "record_observations"), ("v1", "data_types", "record_observations"),
    ("record_observations", "v1", "data_types"), ("record_observations", "data_types", "v1"),
    ("v1", "record_observations", "data_types"), ("data_types", "record_observations", "v1"),
])
def test_exact_schema_modules_import_in_all_orders_without_io(first, second, third):
    import os
    import subprocess
    import sys
    script = f'''import socket
def forbidden(*args, **kwargs):
    raise AssertionError("import attempted I/O")
socket.create_connection = socket.getaddrinfo = forbidden
socket.socket.connect = forbidden
import fulcra_common.{first}
import fulcra_common.{second}
import fulcra_common.{third}
'''
    result = subprocess.run([sys.executable, "-c", script], env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_generated_tag_names_are_bounded_stable_and_keep_supported_labels():
    from fulcra_common.data_types import tag_create_name
    for name in ["lastfm", "Apple Music", "日本語"]:
        assert tag_create_name(name) == name
    a = "Synthetic source label with a shared prefix and ending one"
    b = "Synthetic source label with a shared prefix and ending two"
    assert len(tag_create_name(a)) <= 30
    assert tag_create_name(a) == tag_create_name(a)
    assert tag_create_name(a) != tag_create_name(b)
    assert "/" not in tag_create_name("https://example.com/feed")
    for value in [None, "", False, "\x00", "\ud800"]:
        with pytest.raises(ValueError):
            tag_create_name(value)

SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string", "format": "uuid"},
    "sources": {"type": "array", "items": {"type": "string"}},
    "tags": {"type": "array", "items": {"type": "string"}},
    "start_time": {"type": "string", "format": "date-time"},
    "end_time": {"type": "string", "format": "date-time"},
    **FIELDS,
}}


def spec(**kwargs):
    from fulcra_common.data_types import DataTypeSpec
    args = dict(key="collect.synthetic.v1", base_type="Event", name="Synthetic events",
                description="Synthetic test records.", fields=FIELDS)
    args.update(kwargs)
    return DataTypeSpec(**args)


def test_concurrent_resolution_cannot_replace_a_known_original_destination(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from fulcra_common.data_types import CreationJournal
    path = tmp_path / "types.sqlite"
    first, second = CreationJournal(path), CreationJournal(path)
    definition = spec()
    assert first.begin(ACCOUNT, definition)
    barrier = Barrier(2)
    original_read = first.read
    for journal in (first, second):
        read = journal.read
        def synchronized(account, value, read=read):
            row = read(account, value)
            barrier.wait(timeout=5)
            return row
        monkeypatch.setattr(journal, "read", synchronized)
    def resolve(journal, destination):
        try:
            journal.resolved(ACCOUNT, definition, destination)
            return destination
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(resolve, first, TYPE_ID), executor.submit(resolve, second, SECOND_ID)]
        winners = [future.result(timeout=10) for future in futures]
    assert sum(value is not None for value in winners) == 1
    assert original_read(ACCOUNT, definition)[2] == next(value for value in winners if value is not None)


@pytest.mark.parametrize("status,destination", [("unknown", None), ("resolved", None), ("resolved", "Event/bad"),
    ("pending", TYPE_ID), ("rejected:422", TYPE_ID)])
def test_malformed_creation_intent_is_held_without_implicit_resolution(tmp_path, status, destination):
    from fulcra_common.data_types import CreationJournal
    journal = CreationJournal(tmp_path / "types.sqlite")
    definition = spec()
    journal.begin(ACCOUNT, definition)
    with journal._connect() as db:
        db.execute("UPDATE creations SET status=?,destination=?", (status, destination))
    with pytest.raises(ValueError):
        journal.read(ACCOUNT, definition)
    with pytest.raises(ValueError):
        journal.resolved(ACCOUNT, definition, TYPE_ID)


@pytest.mark.parametrize("status,destination", [("resolved", None), ("pending", TYPE_ID)])
def test_atomic_resolution_does_not_heal_a_row_corrupted_after_preflight(tmp_path, monkeypatch, status, destination):
    from fulcra_common.data_types import CreationJournal
    journal = CreationJournal(tmp_path / "types.sqlite")
    definition = spec()
    journal.begin(ACCOUNT, definition)
    original = journal.read
    def corrupt_after_read(account, value):
        row = original(account, value)
        with journal._connect() as db:
            db.execute("UPDATE creations SET status=?,destination=?", (status, destination))
        return row
    monkeypatch.setattr(journal, "read", corrupt_after_read)
    with pytest.raises(ValueError):
        journal.resolved(ACCOUNT, definition, TYPE_ID)
    with journal._connect() as db:
        assert db.execute("SELECT status,destination FROM creations").fetchone() == (status, destination)


@pytest.mark.parametrize("changes", [{"fields": {1: {"type": "string"}}},
    {"fields": {"nested": {"type": "object", "properties": {False: {"type": "string"}}}}},
    {"fields": {"label": {"enum": ["\ud800"]}}}, {"name": "\ud800"},
    {"description": "\ud800"}, {"key": "\ud800"}])
def test_schema_and_creation_identity_reject_lossy_json_in_pure_preflight(changes):
    with pytest.raises(ValueError):
        spec(**changes)


def entry(**kwargs):
    d = dict(id=TYPE_ID, name="Synthetic events", api_version="v1",
             recordable=True, deprecated=False, fulcra_userid=ACCOUNT,
             record_spec={"type": "event"})
    d.update(kwargs)
    return d


def client(handler, account=ACCOUNT):
    class Client(BaseFulcraClient):
        def get_token(self):
            payload = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": account}).encode()).decode().rstrip("=")
            return "synthetic." + payload + ".signature"

    return Client(base_url="https://api.example", transport=httpx.MockTransport(handler))


def test_spec_snapshots_fields_and_declares_json_schema_fragment():
    fields = {"note": {"type": "string"}}
    s = spec(fields=fields)
    before = s.fingerprint
    fields["note"]["type"] = "number"
    body = s.create_body()
    assert s.fingerprint == before
    assert json.loads(body["record_spec"]["schema"])["properties"]["note"] == {"type": "string"}
    assert body["record_spec"]["type"] == "event"
    assert body["metadata"]["fulcra_tools"]["key"] == s.key


@pytest.mark.parametrize("base", ["Metric", "Event"])
def test_base_only_type_omits_empty_schema_fragment_rejected_by_live_api(base):
    s = spec(base_type=base, fields={})
    assert "schema" not in s.create_body()["record_spec"]


def test_explicit_validation_rejection_can_be_corrected_without_resetting_uncertain_intents(tmp_path):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    journal = CreationJournal(tmp_path / "types.sqlite")
    posts = []
    corrected = spec(description="Corrected datatype description")
    catalog = []

    def handler(request):
        if request.method == "POST":
            posts.append(json.loads(request.content))
            if len(posts) == 1:
                return httpx.Response(422, json={"detail": "schema fragment validation failed"})
            catalog.append(entry())
            return httpx.Response(201, json={"id": TYPE_ID})
        return httpx.Response(200, json=catalog if request.url.path == "/input/v1/data_type" else SCHEMA)

    c = client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        resolve_data_type(client=c, spec=spec(), account_id=ACCOUNT, allow_create=True, journal=journal)
    with pytest.raises(RuntimeError, match="rejected"):
        resolve_data_type(client=c, spec=spec(), account_id=ACCOUNT, allow_create=True, journal=journal)
    assert len(posts) == 1
    result = resolve_data_type(client=c, spec=corrected, account_id=ACCOUNT, allow_create=True, journal=journal)
    assert result.data_type == TYPE_ID
    assert len(posts) == 2
    assert journal.read(ACCOUNT, corrected)[1] == "resolved"


@pytest.mark.parametrize("kwargs", [
    {"base_type": "MomentAnnotation"}, {"name": ""}, {"description": ""},
    {"fields": {"start_time": {"type": "string"}}},
    {"fields": {"note": {"type": "invented"}}},
    {"base_type": "Metric", "aggregation": "invented"},
])
def test_invalid_spec_refused_without_io(kwargs):
    with pytest.raises(ValueError):
        spec(**kwargs)


def test_resolves_only_owned_v1_destination_and_reads_its_schema():
    from fulcra_common.data_types import resolve_data_type
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/input/v1/data_type":
            assert request.url.params["include_deprecated"] == "true"
            own = entry()
            del own["fulcra_userid"]  # The authority infers owner from auth, not a response field.
            return httpx.Response(200, json=[own])
        assert request.url.path == f"/data/v1/catalog/{TYPE_ID}/v1/schema"
        return httpx.Response(200, json=SCHEMA)

    result = resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT)
    assert result.data_type == TYPE_ID
    assert result.account_id == ACCOUNT
    assert result.schema == SCHEMA
    assert all(r.method == "GET" for r in requests)


def test_token_account_mismatch_fails_before_catalog_or_create():
    from fulcra_common.data_types import resolve_data_type

    def handler(request):
        raise AssertionError("wrong-account resolver must not contact API")

    with pytest.raises(ValueError, match="account"):
        resolve_data_type(client=client(handler, OTHER_ACCOUNT), spec=spec(), account_id=ACCOUNT)


def test_boolean_schema_constant_cannot_satisfy_a_numeric_protocol_version():
    from fulcra_common.data_types import resolve_data_type
    s = spec(fields={"counter": {"type": "integer", "const": 1}})
    changed = {**SCHEMA, "properties": {**SCHEMA["properties"],
                                       "counter": {"type": "integer", "const": True}}}
    def handler(request):
        return httpx.Response(200, json=[entry()] if request.url.path == "/input/v1/data_type" else changed)
    with pytest.raises(ValueError, match="incompatible"):
        resolve_data_type(client=client(handler), spec=s, account_id=ACCOUNT)


@pytest.mark.parametrize("rows", [
    [entry(deprecated=True)],
    [entry(), entry(id=SECOND_ID)],
    [entry(record_spec={"type": "metric"})],
])
def test_ambiguous_deprecated_or_wrong_base_refused(rows):
    from fulcra_common.data_types import resolve_data_type
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json=rows)

    with pytest.raises(ValueError):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT)
    assert calls == ["GET"]


def test_pin_disambiguates_only_with_owner_and_schema_proof():
    from fulcra_common.data_types import resolve_data_type

    def handler(request):
        return httpx.Response(200, json=[entry(), entry(id=SECOND_ID)]) if request.url.path == "/input/v1/data_type" else httpx.Response(200, json=SCHEMA)

    result = resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT, pinned_id=SECOND_ID)
    assert result.data_type == SECOND_ID


def test_incompatible_served_field_is_not_adopted_or_changed():
    from fulcra_common.data_types import resolve_data_type
    calls = []

    def handler(request):
        calls.append(request.method)
        schema = {**SCHEMA, "properties": {**SCHEMA["properties"], "note": {"type": "number"}}}
        return httpx.Response(200, json=[entry()]) if request.url.path == "/input/v1/data_type" else httpx.Response(200, json=schema)

    with pytest.raises(ValueError, match="schema"):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT)
    assert calls == ["GET", "GET"]


def test_failed_catalog_is_unknown_never_permission_to_create(tmp_path):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                          allow_create=True, journal=CreationJournal(tmp_path / "types.sqlite"))
    assert calls == ["GET"]


def test_creation_rechecks_catalog_and_schema_before_returning(tmp_path):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    created = []
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            assert request.url.path == "/input/v1/data_type/Event"
            body = json.loads(request.content)
            assert json.loads(body["record_spec"]["schema"])["properties"] == FIELDS
            created.append(entry())
            return httpx.Response(200, json=entry())
        return httpx.Response(200, json=created) if request.url.path == "/input/v1/data_type" else httpx.Response(200, json=SCHEMA)

    result = resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                               allow_create=True, journal=CreationJournal(tmp_path / "types.sqlite"))
    assert result.data_type == TYPE_ID
    assert [r.method for r in requests] == ["GET", "POST", "GET", "GET"]


def test_lost_create_response_survives_restart_without_duplicate_creation(tmp_path):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    path = tmp_path / "types.sqlite"
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request)
            raise httpx.ReadTimeout("synthetic lost receipt", request=request)
        return httpx.Response(200, json=[])

    with pytest.raises(httpx.ReadTimeout):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                          allow_create=True, journal=CreationJournal(path))
    with pytest.raises(RuntimeError, match="reconcile"):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                          allow_create=True, journal=CreationJournal(path))
    assert len(posts) == 1


def test_uncertain_create_recovers_by_owned_schema_readback(tmp_path):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    path = tmp_path / "types.sqlite"
    visible = []

    def handler(request):
        if request.method == "POST":
            visible.append(entry())
            raise httpx.ReadTimeout("synthetic lost receipt", request=request)
        return httpx.Response(200, json=visible) if request.url.path == "/input/v1/data_type" else httpx.Response(200, json=SCHEMA)

    with pytest.raises(httpx.ReadTimeout):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                          allow_create=True, journal=CreationJournal(path))
    result = resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                               allow_create=True, journal=CreationJournal(path))
    assert result.data_type == TYPE_ID


def test_creation_requires_durable_journal():
    from fulcra_common.data_types import resolve_data_type

    with pytest.raises(ValueError, match="journal"):
        resolve_data_type(client=client(lambda r: httpx.Response(200, json=[])),
                          spec=spec(), account_id=ACCOUNT, allow_create=True)


def test_journal_refuses_silent_destination_replacement(tmp_path):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    rows = [entry()]

    def handler(request):
        return httpx.Response(200, json=rows) if request.url.path == "/input/v1/data_type" else httpx.Response(200, json=SCHEMA)

    journal = CreationJournal(tmp_path / "types.sqlite")
    resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT, journal=journal)
    rows[:] = [entry(id=SECOND_ID)]
    with pytest.raises(ValueError, match="destination"):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT, journal=journal)


def test_account_switch_before_create_does_not_reserve_an_unattempted_intent(tmp_path):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    journal = CreationJournal(tmp_path / "types.sqlite")
    current = client(lambda r: httpx.Response(200, json=[]))
    original = current.get_token
    other = client(lambda r: None, OTHER_ACCOUNT).get_token
    tokens = iter([original(), other()])
    current.get_token = lambda: next(tokens)
    with pytest.raises(ValueError, match="account"):
        resolve_data_type(client=current, spec=spec(), account_id=ACCOUNT, allow_create=True, journal=journal)
    assert journal.read(ACCOUNT, spec()) is None


def test_metric_spec_preserves_scale_and_value_map_snapshot():
    scale = {"min": 0, "max": 1, "step": 1}
    labels = {"0": "False", "1": "True"}
    s = spec(base_type="Metric", aggregation="discrete", scale=scale, value_map=labels)
    scale["max"] = 9
    labels["1"] = "changed"
    body = s.create_body()["record_spec"]
    assert body["scale"] == {"min": 0, "max": 1, "step": 1}
    assert body["value_map"] == {"0": "False", "1": "True"}


@pytest.mark.parametrize("scale", [
    {"min": 2, "max": 1, "step": 1},
    {"min": 0, "max": 1, "step": 0},
    {"min": False, "max": 1, "step": 1},
    {"min": 0, "max": 1},
])
def test_metric_scale_must_be_complete_and_valid(scale):
    with pytest.raises(ValueError):
        spec(base_type="Metric", scale=scale)


def test_metric_destination_scale_and_labels_must_match():
    from fulcra_common.data_types import resolve_data_type
    scale = {"min": 0, "max": 1, "step": 1}
    labels = {"0": "False", "1": "True"}
    s = spec(base_type="Metric", scale=scale, value_map=labels)
    metric_id = TYPE_ID.replace("Event/", "Metric/")

    def handler(request):
        row = entry(id=metric_id, record_spec={"type": "metric", "aggregation": "discrete", "unit": None, "scale": scale, "value_map": labels})
        return httpx.Response(200, json=[row]) if request.url.path == "/input/v1/data_type" else httpx.Response(200, json={**SCHEMA, "properties": {**SCHEMA["properties"], "value": {"type": "number"}}, "required": ["value"]})

    assert resolve_data_type(client=client(handler), spec=s, account_id=ACCOUNT).data_type == metric_id


@pytest.mark.parametrize("direct", [503, [entry()], {"unexpected": "wrapper"}])
def test_partial_catalog_cannot_authorize_creation_when_owned_input_is_unknown_or_present(tmp_path, direct):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    posts = []

    def handler(request):
        if request.url.path == "/input/v1/data_type":
            assert request.url.params["include_deprecated"] == "true"
            return httpx.Response(direct) if isinstance(direct, int) else httpx.Response(200, json=direct)
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(201, json={"id": TYPE_ID})
        if request.url.path == "/data/v1/catalog":
            return httpx.Response(200, json=[entry()] if posts else [])
        return httpx.Response(200, json=SCHEMA)

    journal = CreationJournal(tmp_path / "types.sqlite")
    if isinstance(direct, list):
        result = resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                                   allow_create=True, journal=journal)
        assert result.data_type == TYPE_ID
    else:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                              allow_create=True, journal=journal)
        assert journal.read(ACCOUNT, spec()) is None
    assert posts == []


def test_partial_catalog_cannot_hide_a_second_owned_same_name_type():
    from fulcra_common.data_types import resolve_data_type

    def handler(request):
        if request.url.path == "/input/v1/data_type":
            return httpx.Response(200, json=[entry(), entry(id=SECOND_ID)])
        return httpx.Response(200, json=[entry()] if request.url.path == "/data/v1/catalog" else SCHEMA)

    with pytest.raises(ValueError, match="ambiguous"):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT)


@pytest.mark.parametrize("rows", [
    None, {}, [None], [entry(), entry()], [entry(fulcra_userid=OTHER_ACCOUNT)],
    [entry(api_version="v1alpha1")], [entry(recordable=False)], [entry(recordable=1)],
    [entry(deprecated=None)], [entry(deprecated=0)], [entry(id="Event")],
    [entry(id="Event/not-a-uuid")], [entry(record_spec={"type": "metric"})],
    [entry(name="")], [entry(name=123)], [entry(record_spec=None)],
])
def test_owned_type_protocol_unknown_is_never_partial_or_creation_permission(tmp_path, rows):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/input/v1/data_type"
        assert dict(request.url.params) == {"include_deprecated": "true"}
        assert "x-fulcra-userid" not in request.headers
        return httpx.Response(200, json=rows)

    journal = CreationJournal(tmp_path / "types.sqlite")
    with pytest.raises(ValueError):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                          allow_create=True, journal=journal)
    assert len(requests) == 1
    assert journal.read(ACCOUNT, spec()) is None


def test_deprecated_owned_type_cannot_be_silently_replaced(tmp_path):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    requests = []

    def handler(request):
        requests.append(request)
        assert request.method == "GET" and request.url.path == "/input/v1/data_type"
        return httpx.Response(200, json=[entry(deprecated=True)])

    journal = CreationJournal(tmp_path / "types.sqlite")
    with pytest.raises(ValueError, match="deprecated"):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                          allow_create=True, journal=journal)
    assert len(requests) == 1
    assert journal.read(ACCOUNT, spec()) is None


def test_oversized_owned_type_list_refuses_without_truncating_or_creating(tmp_path):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    journal = CreationJournal(tmp_path / "types.sqlite")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[entry()] * 4097)

    with pytest.raises(ValueError, match="limit"):
        resolve_data_type(client=client(handler), spec=spec(), account_id=ACCOUNT,
                          allow_create=True, journal=journal)
    assert len(requests) == 1 and requests[0].method == "GET"
    assert journal.read(ACCOUNT, spec()) is None


@pytest.mark.parametrize("kind", ["partial_status", "content_range", "pagination_link", "cursor", "redirect", "total", "retargeted_request"])
def test_partial_or_redirected_owned_type_read_cannot_authorize_post(tmp_path, kind):
    from fulcra_common.data_types import CreationJournal, resolve_data_type
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(503)
        if request.url.path == "/data/v1/catalog":
            return httpx.Response(200, json=[])
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "/data/v1/catalog"})
        headers = {"content_range": {"Content-Range": "items 0-0/2"},
                   "pagination_link": {"Link": '</input/v1/data_type?cursor=next>; rel="next"'},
                   "cursor": {"X-Next-Cursor": "next"}, "total": {"X-Total-Count": "2"}}.get(kind, {})
        return httpx.Response(206 if kind == "partial_status" else 200, json=[], headers=headers)

    journal = CreationJournal(tmp_path / "types.sqlite")
    current = client(handler)
    if kind == "retargeted_request":
        def retarget(request):
            request.url = request.url.copy_with(path="/data/v1/catalog")
        current._client().event_hooks["request"] = [retarget]
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        resolve_data_type(client=current, spec=spec(), account_id=ACCOUNT,
                          allow_create=True, journal=journal)
    assert [request.method for request in requests] == ["GET"]
    assert journal.read(ACCOUNT, spec()) is None


def test_readonly_creation_journal_never_creates_its_file_or_parent(tmp_path):
    from fulcra_common.data_types import CreationJournal
    path = tmp_path / "missing" / "types.sqlite"
    with pytest.raises(FileNotFoundError):
        CreationJournal(path, read_only=True)
    assert not path.parent.exists()


def test_readonly_creation_journal_preserves_binding_and_rejects_writes(tmp_path):
    import sqlite3
    from fulcra_common.data_types import CreationJournal
    original = CreationJournal(tmp_path / "types.sqlite")
    original.resolved(ACCOUNT, spec(), TYPE_ID)
    readonly = CreationJournal(original.path, read_only=True)
    assert readonly.read(ACCOUNT, spec()) == original.read(ACCOUNT, spec())
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        readonly.begin(OTHER_ACCOUNT, spec())
    assert original.read(OTHER_ACCOUNT, spec()) is None


def test_public_observed_schema_rejects_plain_context_before_credentials(monkeypatch):
    from fulcra_common.data_types import observe_served_schema
    current = client(lambda _: pytest.fail("plain context reached HTTP"))
    monkeypatch.setattr(current, "get_token", lambda: pytest.fail("plain context reached credentials"))
    with pytest.raises(ValueError):
        observe_served_schema(client=current, account_id=ACCOUNT, data_type=TYPE_ID,
            context={}, on_observation=lambda _: pytest.fail("plain context reached sink"),
            request_budget=None, request_guard=lambda **_: None)
