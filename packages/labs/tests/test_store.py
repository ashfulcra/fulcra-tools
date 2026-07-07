"""Storage-layer tests — hermetic (httpx MockTransport + stubbed lib)."""
from __future__ import annotations

import json

import httpx

from fulcra_labs import store
from fulcra_labs.store import LabsState, ingest_extraction, load_state, save_state
from fulcra_labs.validate import det_source_id
from labs_test_helpers import json_response, make_client


def _obs(marker, value, unit):
    return {"marker_raw": marker, "value_raw": value, "unit_raw": unit,
            "flag_raw": None, "reference_range_raw": None}


def _extraction(observations, collected="2026-03-15"):
    return {"lab": "LabCorp", "report_date": collected, "collected_at": collected,
            "observations": observations}


class _Recorder:
    """Handler that mints def ids, accepts ingests, and records posted records."""
    def __init__(self):
        self.created = 0
        self.records: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/v1alpha1/annotation" and request.method == "POST":
            self.created += 1
            return json_response(200, {"id": f"def-{self.created}"})
        if request.url.path == "/ingest/v1/record" and request.method == "POST":
            self.records.append(json.loads(request.content))
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {request.url}")


def test_det_id_is_deterministic():
    a = det_source_id("glucose", "2026-03-15T00:00:00Z", 92.0)
    b = det_source_id("glucose", "2026-03-15T00:00:00Z", 92.0)
    assert a == b and a.startswith("com.fulcra.labs.v1.")
    assert a != det_source_id("glucose", "2026-03-15T00:00:00Z", 93.0)


def test_create_on_first_use_once_then_cached():
    rec = _Recorder()
    client, transport = make_client(rec)
    ext = _extraction([_obs("Glucose", "92", "mg/dL"), _obs("Glucose", "100", "mg/dL")])
    outcome, _ = ingest_extraction(client, ext, state=LabsState())
    assert rec.created == 1                  # one marker -> one def, despite 2 rows
    assert len(rec.records) == 2
    assert outcome.ingested == 2
    assert outcome.tracks_created == ["glucose"]


def test_dry_run_writes_nothing():
    def boom(request):
        raise AssertionError(f"dry-run made a request: {request.url}")
    client, transport = make_client(boom)
    ext = _extraction([_obs("Glucose", "92", "mg/dL")])
    outcome, _ = ingest_extraction(client, ext, state=LabsState(), dry_run=True)
    assert outcome.ingested == 1
    assert outcome.tracks_created == []
    assert transport.requests == []
    assert not store.state_path().exists()   # nothing persisted


def test_record_payload_shape():
    rec = _Recorder()
    client, _ = make_client(rec)
    ext = _extraction([{"marker_raw": "Glucose", "value_raw": "92", "unit_raw": "mg/dL",
                        "flag_raw": "H", "reference_range_raw": "65-99"}])
    ingest_extraction(client, ext, state=LabsState())
    record = rec.records[0]
    assert record["metadata"]["data_type"] == "NumericAnnotation"
    src = record["metadata"]["source"]
    assert any(s.startswith("com.fulcra.labs.v1.") for s in src)
    assert any(s.startswith("com.fulcradynamics.annotation.") for s in src)
    data = json.loads(record["data"])
    assert data["value"] == 92.0
    assert data["unit"] == "mg/dL"
    assert data["raw_value"] == "92"
    assert data["raw_unit"] == "mg/dL"
    assert data["flag"] == "H"
    assert data["reference_range"] == "65-99"
    assert data["lab"] == "LabCorp"
    assert data["qualifier"] is None
    assert "source_doc" in data


def test_adopts_existing_track_from_catalog():
    rec = _Recorder()
    catalog = [{
        "id": "existing-glucose",
        "annotation_type": "numeric",
        "deleted_at": None,
        "description": "Glucose. Canonical unit: mg/dL. [com.fulcra.labs.marker.glucose]",
    }]
    client, _ = make_client(rec, catalog=catalog)
    ext = _extraction([_obs("Glucose", "92", "mg/dL")])
    outcome, _ = ingest_extraction(client, ext, state=LabsState())
    assert rec.created == 0                    # adopted, never created
    assert outcome.tracks_adopted == ["glucose"]
    assert rec.records[0]["metadata"]["source"][-1].endswith("existing-glucose")


def test_cached_def_revalidated_and_reused():
    rec = _Recorder()
    catalog = [{"id": "def-cached", "annotation_type": "numeric", "deleted_at": None,
                "description": "x"}]
    client, _ = make_client(rec, catalog=catalog)
    state = LabsState()
    state.markers["glucose"] = store.MarkerEntry(
        def_id="def-cached", canonical_unit="mg/dL", created_at="2026-01-01T00:00:00Z")
    ingest_extraction(client, _extraction([_obs("Glucose", "92", "mg/dL")]), state=state)
    assert rec.created == 0                     # cached def still live -> reuse


def test_reingest_produces_identical_source_ids():
    """Idempotency: the same extraction twice emits the same deterministic
    source ids (Fulcra dedupes server-side on them)."""
    rec1 = _Recorder()
    c1, _ = make_client(rec1)
    rec2 = _Recorder()
    c2, _ = make_client(rec2)
    ext = _extraction([_obs("Glucose", "92", "mg/dL"), _obs("TSH", "2.1", "uIU/mL")])
    ingest_extraction(c1, ext, state=LabsState())
    ingest_extraction(c2, ext, state=LabsState())
    ids1 = sorted(r["metadata"]["source"][0] for r in rec1.records)
    ids2 = sorted(r["metadata"]["source"][0] for r in rec2.records)
    assert ids1 == ids2


def test_state_round_trips(tmp_path):
    p = tmp_path / "markers.json"
    s = LabsState()
    s.markers["glucose"] = store.MarkerEntry("def-1", "mg/dL", "2026-01-01T00:00:00Z")
    s.last_ingest = "2026-03-15T00:00:00Z"
    save_state(s, p)
    loaded = load_state(p)
    assert loaded.markers["glucose"].def_id == "def-1"
    assert loaded.last_ingest == "2026-03-15T00:00:00Z"


def test_review_rows_are_held_not_ingested():
    rec = _Recorder()
    client, _ = make_client(rec)
    # Missing unit -> review; must not ingest.
    ext = _extraction([{"marker_raw": "Glucose", "value_raw": "92", "unit_raw": None,
                        "flag_raw": None, "reference_range_raw": None}])
    outcome, _ = ingest_extraction(client, ext, state=LabsState())
    assert outcome.ingested == 0
    assert outcome.review_held == 1
    assert rec.records == []


def test_confirmed_review_key_gets_ingested():
    rec = _Recorder()
    client, _ = make_client(rec)
    # Implausible value (unit mixup) -> review, but marker+value+unit resolved,
    # so an explicit --yes-reviewed can push it through.
    ext = _extraction([_obs("Glucose", "5.5", "mg/dL")])
    outcome, _ = ingest_extraction(client, ext, state=LabsState(),
                                   confirmed_keys={"glucose"})
    assert outcome.ingested == 1
    assert json.loads(rec.records[0]["data"])["value"] == 5.5


def test_set_unit_at_creation_toggle(monkeypatch):
    rec = _Recorder()
    captured = {}

    def handler(request):
        if request.url.path == "/user/v1alpha1/annotation":
            captured["body"] = json.loads(request.content)
            return json_response(200, {"id": "def-1"})
        return rec(request)
    # Default (False): unit is null at creation (spiked-verified path).
    client, _ = make_client(handler)
    ingest_extraction(client, _extraction([_obs("Glucose", "92", "mg/dL")]),
                      state=LabsState())
    assert captured["body"]["measurement_spec"]["unit"] is None

    # Flipped: the API create body carries the canonical unit (httpx-mock only;
    # NOT verified against the live account).
    monkeypatch.setattr(store, "SET_UNIT_AT_CREATION", True)
    client2, _ = make_client(handler)
    ingest_extraction(client2, _extraction([_obs("Glucose", "92", "mg/dL")]),
                      state=LabsState())
    assert captured["body"]["measurement_spec"]["unit"] == "mg/dL"
