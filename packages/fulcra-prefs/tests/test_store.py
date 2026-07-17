import json
from fulcra_prefs.store import FulcraStore, PREFS_ROOT
from test_schema import make_signal

def test_prefs_root_is_namespaced():
    assert PREFS_ROOT == "prefs"

def test_write_then_read_json_roundtrip(fake_api):
    store = FulcraStore(fake_api)
    store.write_json("prefs/compiled.json", {"v": 1, "keys": {}})
    assert store.read_json("prefs/compiled.json") == {"v": 1, "keys": {}}

def test_list_json_reads_folder_children(fake_api):
    # NOTE: the [a, b] ordering comes from the fake's sorted(); the real server makes no ordering promise. Compile is input-order-independent, so nothing depends on this order.
    store = FulcraStore(fake_api)
    store.write_json("prefs/signals-cache/b.json", {"id": "b"})
    store.write_json("prefs/signals-cache/a.json", {"id": "a"})
    assert [rec["id"] for rec in store.list_json("prefs/signals-cache")] == ["a", "b"]

def test_list_json_parallel_path_roundtrips_many_shards(fake_api):
    """list_json fetches shards concurrently (perf: compile shouldn't scale as
    N sequential round-trips). Correctness + completeness must be unchanged for
    many shards regardless of fetch order."""
    store = FulcraStore(fake_api)
    for i in range(25):
        store.write_json(f"prefs/signals-cache/s{i:02d}.json", {"id": f"s{i:02d}"})
    got = {rec["id"] for rec in store.list_json("prefs/signals-cache")}
    assert got == {f"s{i:02d}" for i in range(25)}


def test_list_json_propagates_download_errors(fake_api):
    """A failed shard download must still surface (not be silently dropped),
    matching the old sequential behavior — ex.map re-raises on iteration."""
    store = FulcraStore(fake_api)
    for i in range(5):
        store.write_json(f"prefs/signals-cache/s{i}.json", {"id": i})
    orig = fake_api.download_file
    def boom(file_id):
        if file_id.endswith("s3.json"):
            raise OSError("simulated download outage")
        return orig(file_id)
    fake_api.download_file = boom
    import pytest
    with pytest.raises(OSError):
        store.list_json("prefs/signals-cache")


def test_read_missing_returns_none(fake_api):
    assert FulcraStore(fake_api).read_json("prefs/compiled.json") is None

def test_written_bytes_are_canonical(fake_api):
    store = FulcraStore(fake_api)
    store.write_json("prefs/meta.json", {"b": 1.23456789, "a": 1})
    assert fake_api.files["/prefs/meta.json"] == b'{"a":1,"b":1.234568}'

def test_ingest_signal_posts_typed_unwrapped_body(fake_api):
    """ingest_signal writes to the TYPED surface: POST /ingest/v1/record/{base}
    with an unwrapped body {note, recorded_at, sources} — NOT the legacy wrapped
    DataRecordV1 envelope. The base data_type is the path segment; the custom
    definition rides in `sources`."""
    store = FulcraStore(fake_api)
    store.ingest_signal(make_signal(id=None), data_type="MomentAnnotation/def-123")
    # Endpoint carries the BARE base type — the API rejects a compound path segment.
    assert fake_api.ingest_paths[0] == "/ingest/v1/record/MomentAnnotation"
    rec = fake_api.ingested[0]
    assert set(rec) == {"note", "recorded_at", "sources"}   # unwrapped, no envelope
    assert "specversion" not in rec and "metadata" not in rec
    assert rec["recorded_at"] == "2026-06-01T12:00:00+00:00"
    # sources[0]: temp signal id; [1]: annotation linkage (definition id from the
    # part after the "/" in data_type); [2]: capture marker
    assert rec["sources"][0].startswith("com.fulcra-prefs.sig.")
    assert rec["sources"][1] == "com.fulcradynamics.annotation.def-123"
    assert rec["sources"][2] == "com.fulcra-prefs.capture.claude-code"
    assert json.loads(rec["note"])["key"] == "dining.cuisine.thai"

def test_ingest_signal_bare_data_type_has_no_annotation_source(fake_api):
    """When data_type has no slash (no definition id), sources is [sid, capture-marker]
    — the annotation linkage entry is omitted rather than emitting an empty string."""
    store = FulcraStore(fake_api)
    store.ingest_signal(make_signal(id=None), data_type="MomentAnnotation")
    assert fake_api.ingest_paths[0] == "/ingest/v1/record/MomentAnnotation"
    rec = fake_api.ingested[0]
    assert len(rec["sources"]) == 2
    assert rec["sources"][0].startswith("com.fulcra-prefs.sig.")
    assert rec["sources"][1] == "com.fulcra-prefs.capture.claude-code"


def test_ingest_preflight_is_loud_but_non_fatal(fake_api, capsys):
    """validate_records pre-flight (0.1.37 adoption): a schema-invalid record
    emits a loud, precise stderr warning but STILL ingests — non-fatal, the
    server stays the authority; nothing is lost or blocked."""
    fake_api.validation_errors = "recorded_at: 'nope' is not a 'date-time'"
    store = FulcraStore(fake_api)
    store.ingest_signal(make_signal(id=None), data_type="MomentAnnotation/def-123")
    assert len(fake_api.ingested) == 1                       # non-fatal: still posted
    err = capsys.readouterr().err
    assert "pre-ingest schema warning" in err and "recorded_at" in err  # loud + precise

def test_ingest_no_warning_when_valid(fake_api, capsys):
    store = FulcraStore(fake_api)
    store.ingest_signal(make_signal(id=None), data_type="MomentAnnotation/def-123")
    assert len(fake_api.ingested) == 1
    assert "schema warning" not in capsys.readouterr().err

def test_ingest_preflight_skips_on_schema_outage(fake_api, capsys):
    """A catalog/schema-fetch failure must never block or noise the ingest —
    validation silently degrades to skip and the record posts normally."""
    fake_api.fail_validate = True
    store = FulcraStore(fake_api)
    store.ingest_signal(make_signal(id=None), data_type="MomentAnnotation/def-123")
    assert len(fake_api.ingested) == 1
    assert capsys.readouterr().err == ""

def test_typed_body_and_endpoint_map_from_canonical_envelope():
    """typed_body/typed_ingest_endpoint translate the canonical build_record
    envelope (kept for the outbox spool + shard cache) to the typed wire shape."""
    from fulcra_prefs.store import build_record, typed_body, typed_ingest_endpoint
    record = build_record(make_signal(id=None), "MomentAnnotation/def-123")
    assert typed_ingest_endpoint(record) == "/ingest/v1/record/MomentAnnotation"
    body = typed_body(record)
    assert body == {"note": record["data"],
                    "recorded_at": record["metadata"]["recorded_at"],
                    "sources": record["metadata"]["source"]}
