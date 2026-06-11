import json
import pytest
from fulcra_prefs.schema import (
    Signal, canonical_json, parse_record, temp_signal_id, SCHEMA_V,
)

def make_signal(**over):
    base = dict(
        id="rec-001", kind="preference", key="dining.cuisine.thai",
        scope="global", value={"liked": True}, strength=0.8, confidence=0.9,
        half_life_days=90.0, observed_at="2026-06-01T12:00:00+00:00",
        platform="claude-code", agent="a", session="s", supersedes=None,
    )
    base.update(over)
    return Signal(**base)

def test_canonical_json_is_sorted_compact_and_float_normalized():
    s = canonical_json({"b": 1.23456789, "a": {"y": 2, "x": 1}})
    assert s == '{"a":{"x":1,"y":2},"b":1.234568}'

def test_canonical_json_is_stable_under_key_insertion_order():
    assert canonical_json({"a": 1, "b": 2}) == canonical_json({"b": 2, "a": 1})

def test_signal_roundtrip_through_record_payload():
    sig = make_signal()
    payload = sig.to_payload()           # dict for the record `data` field
    env = {                              # what get-records returns
        "id": "rec-001",
        "recorded_at": "2026-06-01T12:00:00+00:00",
        "sources": ["com.fulcra-prefs.sig.0000-aaaa",
                    "com.fulcra-prefs.capture.claude-code"],
        "data": json.dumps(payload),
    }
    back = parse_record(env)
    assert back == sig
    assert back.id == "rec-001"          # persisted id wins over temp id

def test_parse_record_uses_temp_id_when_unpersisted():
    sig = make_signal()
    env = {"id": None, "recorded_at": "2026-06-01T12:00:00+00:00",
           "sources": ["com.fulcra-prefs.sig.0000-aaaa",
                       "com.fulcra-prefs.capture.claude-code"],
           "data": json.dumps(sig.to_payload())}
    assert parse_record(env).id == "com.fulcra-prefs.sig.0000-aaaa"

def test_parse_record_preserves_source_ids_for_supersedes_aliases():
    sig = make_signal()
    env = {"id": "rec-001", "recorded_at": "2026-06-01T12:00:00+00:00",
           "sources": ["com.fulcra-prefs.sig.0000-aaaa",
                       "com.fulcra-prefs.capture.claude-code"],
           "data": json.dumps(sig.to_payload())}
    back = parse_record(env)
    assert back.id == "rec-001"
    assert "com.fulcra-prefs.sig.0000-aaaa" in back.source_ids

def test_temp_signal_id_is_deterministic_for_same_inputs():
    a = temp_signal_id("dining.cuisine.thai", "2026-06-01T12:00:00+00:00", "claude-code")
    b = temp_signal_id("dining.cuisine.thai", "2026-06-01T12:00:00+00:00", "claude-code")
    assert a == b and a.startswith("com.fulcra-prefs.sig.")

def test_payload_carries_schema_version():
    assert make_signal().to_payload()["v"] == SCHEMA_V

def test_invalid_kind_rejected():
    with pytest.raises(ValueError):
        make_signal(kind="whim")

def test_invalid_scope_rejected():
    with pytest.raises(ValueError):
        make_signal(scope="galaxy")


def test_nonpositive_half_life_rejected():
    """BUG C: half_life_days <= 0 makes effective_weight divide by zero
    (2 ** (-age/0)). Worse, the bad signal would be cached, so EVERY later
    compile would crash permanently. Reject it at construction so the poisoned
    signal never reaches the cache. None (no decay) stays valid."""
    with pytest.raises(ValueError):
        make_signal(half_life_days=0.0)
    with pytest.raises(ValueError):
        make_signal(half_life_days=-30.0)
    make_signal(half_life_days=None)   # no-decay sentinel still allowed
