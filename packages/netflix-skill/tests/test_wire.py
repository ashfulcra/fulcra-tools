import json


def test_build_record_wire_shape(ni, fixtures_dir):
    ev = list(ni.parse_slim(fixtures_dir / "slim.csv"))[2]
    rec = ni.build_record(ev, def_id="abc-123")
    assert rec["specversion"] == 1
    md = rec["metadata"]
    assert md["data_type"] == "DurationAnnotation"
    assert md["recorded_at"] == {
        "start_time": "2024-03-01T12:00:00Z",
        "end_time": "2024-03-01T12:00:01Z",
    }
    assert md["tags"] == []
    assert md["source"][0] == ev.det_id
    assert md["source"][-1] == "com.fulcradynamics.annotation.abc-123"
    assert md["content_type"] == "application/json"
    payload = json.loads(rec["data"])
    assert payload["title"] == ev.title
    assert payload["note"] == ev.note
    assert payload["external_ids"]["content_fingerprint"] == ev.fingerprint
    assert payload["external_ids"]["timestamp_confidence"] == "low"
    # canonical bytes: data is sorted-keys
    assert rec["data"] == json.dumps(payload, sort_keys=True)


def test_encode_batch_jsonl(ni, fixtures_dir):
    evs = list(ni.parse_slim(fixtures_dir / "slim.csv"))[:2]
    body = ni.encode_batch([ni.build_record(e, def_id="d") for e in evs])
    lines = body.decode().split("\n")
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # every line standalone JSON


def test_build_record_wire_shape_rich_variant(ni, fixtures_dir):
    ev = [e for e in ni.parse_rich(fixtures_dir / "gdpr.csv") if "Dune" in e.note][0]
    rec = ni.build_record(ev, def_id="abc-123")
    md = rec["metadata"]
    assert md["recorded_at"]["start_time"] == ni.iso_z(ev.start)
    assert md["recorded_at"]["end_time"] == ni.iso_z(ev.end)
    # recorded_at consistency: end - start round-trips through the wire strings
    assert md["recorded_at"]["start_time"] < md["recorded_at"]["end_time"]
    payload = json.loads(rec["data"])
    assert payload["external_ids"]["profile"] == "Ash"
    assert payload["external_ids"]["device_type"] == "TV"
    assert payload["external_ids"]["timestamp_confidence"] == "high"
    assert payload["duration_seconds"] == 9312
