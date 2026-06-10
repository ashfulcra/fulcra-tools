import json
import pytest
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

def test_read_missing_returns_none(fake_api):
    assert FulcraStore(fake_api).read_json("prefs/compiled.json") is None

def test_written_bytes_are_canonical(fake_api):
    store = FulcraStore(fake_api)
    store.write_json("prefs/meta.json", {"b": 1.23456789, "a": 1})
    assert fake_api.files["/prefs/meta.json"] == b'{"a":1,"b":1.234568}'

def test_ingest_signal_posts_data_record_v1(fake_api):
    store = FulcraStore(fake_api)
    store.ingest_signal(make_signal(id=None), data_type="MomentAnnotation/def-123")
    rec = fake_api.ingested[0]
    assert rec["specversion"] == 1
    assert rec["metadata"]["data_type"] == "MomentAnnotation/def-123"
    assert rec["metadata"]["recorded_at"] == "2026-06-01T12:00:00+00:00"
    assert rec["metadata"]["source"][0].startswith("com.fulcra-prefs.sig.")
    assert rec["metadata"]["source"][1] == "com.fulcra-prefs.capture.claude-code"
    assert json.loads(rec["data"])["key"] == "dining.cuisine.thai"
