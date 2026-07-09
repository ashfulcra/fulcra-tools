"""Storage-layer tests — hermetic (httpx MockTransport + stubbed lib)."""
from __future__ import annotations

import json

import httpx
import pytest

from fulcra_labs import store
from fulcra_labs.store import LabsState, ingest_extraction, load_state, save_state
from fulcra_labs.validate import det_source_id, parse_collected_at
from labs_test_helpers import json_response, make_client


def _obs(marker, value, unit):
    return {"marker_raw": marker, "value_raw": value, "unit_raw": unit,
            "flag_raw": None, "reference_range_raw": None}


def _extraction(observations, collected="2026-03-15"):
    return {"lab": "LabCorp", "report_date": collected, "collected_at": collected,
            "observations": observations}


class _TypedRecorder:
    """Handler mirroring the live typed endpoints: mints def ids, accepts
    typed single (application/json) or JSONL (application/x-jsonl) posts to
    ``/ingest/v1/record/NumericAnnotation`` (→ 201 {"upload_id": …}), and
    serves the posted records back from the event endpoint so the store's
    landed-verification poll can see them.

    ``visible=False`` simulates the async-lag / silent-drop hazard: posts
    return 201 but the records never become queryable, so verification must
    report them missing and fail loudly.
    """

    def __init__(self, *, visible: bool = True):
        self.created = 0
        self.records: list[dict] = []
        self.visible = visible

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/user/v1alpha1/annotation" and method == "POST":
            self.created += 1
            return json_response(200, {"id": f"def-{self.created}"})
        if path.startswith("/user/v1alpha1/annotation/") and method == "GET":
            # definition_exists revalidation — a live, non-deleted def.
            return json_response(200, {"id": path.rsplit("/", 1)[-1],
                                       "deleted_at": None})
        if path == "/ingest/v1/record/NumericAnnotation" and method == "POST":
            ctype = request.headers.get("content-type", "")
            if "x-jsonl" in ctype:
                for line in request.content.decode().split("\n"):
                    if line.strip():
                        self.records.append(json.loads(line))
            else:
                self.records.append(json.loads(request.content))
            return json_response(201, {"upload_id": "up-1"})
        if path == "/data/v1alpha1/event/NumericAnnotation" and method == "GET":
            return json_response(200, self.records if self.visible else [])
        raise AssertionError(f"unexpected request {method} {request.url}")


def _visible_after_precheck():
    """records_visible stub for FakePipe tests (no real POST populates the
    recorder): call 1 is the pre-POST already-present check → empty (nothing
    ingested in a prior run); later calls are the landing poll → everything
    visible (success)."""
    calls = {"n": 0}

    def fake(base_type, source_ids, window_start, window_end):
        calls["n"] += 1
        return set() if calls["n"] == 1 else set(source_ids)
    return fake


def _run_ingest_with_one_ok_row(monkeypatch, tmp_path, captured):
    """Drive ``ingest_extraction`` for a single ok glucose row (99.1 mg/dL,
    flag H, ref 65-99, source doc ``2026-06-01-quest.pdf``) with the typed
    POST captured via a FakePipe, the pre-check finding nothing, and landing
    verification stubbed to succeed.
    """
    monkeypatch.setattr(store, "_LANDING_POLL_SLEEP_S", 0)
    pdf = tmp_path / "2026-06-01-quest.pdf"
    pdf.write_bytes(b"%PDF-1.4\nlabs traceability fixture\n")

    client, _ = make_client(_TypedRecorder())
    monkeypatch.setattr(client, "records_visible", _visible_after_precheck())
    ext = _extraction([{"marker_raw": "Glucose", "value_raw": "99.1",
                        "unit_raw": "mg/dL", "flag_raw": "H",
                        "reference_range_raw": "65-99"}])
    outcome, report = ingest_extraction(
        client, ext, state=LabsState(), source_doc=pdf)
    captured["outcome"] = outcome
    captured["report"] = report
    return outcome, report


def test_det_id_is_deterministic():
    a = det_source_id("glucose", "2026-03-15T00:00:00Z", 92.0)
    b = det_source_id("glucose", "2026-03-15T00:00:00Z", 92.0)
    assert a == b and a.startswith("com.fulcra.labs.v1.")
    assert a != det_source_id("glucose", "2026-03-15T00:00:00Z", 93.0)


def test_ingest_posts_typed_numeric_with_first_class_unit(monkeypatch, tmp_path):
    """Typed ingest carries value+unit as schema fields (the legacy path
    left record-level unit null — live-verified fix, 2026-07-08).
    Traceability fields ride in note (no typed data slot) + local archive."""
    captured: dict = {}

    class FakePipe:
        def __init__(self, client=None):
            pass

        def ingest_typed(self, base_type, records):
            captured["base_type"] = base_type
            captured["records"] = records

    monkeypatch.setattr("fulcra_labs.store.IngestPipeline", FakePipe)
    _run_ingest_with_one_ok_row(monkeypatch, tmp_path, captured)

    rec = captured["records"][0]
    assert captured["base_type"] == "NumericAnnotation"
    assert rec["value"] == 99.1 and rec["unit"] == "mg/dL"
    # note packs the traceability fields in the fixed parseable format.
    assert "H 99.1 mg/dL" in rec["note"]
    assert "[ref 65-99]" in rec["note"]
    assert "LabCorp" in rec["note"]
    assert "2026-06-01-quest.pdf#" in rec["note"]
    # no wrapped envelope, no data payload — those are stripped by the endpoint.
    assert "data" not in rec and "metadata" not in rec and "specversion" not in rec
    assert any(s.startswith("com.fulcradynamics.annotation.") for s in rec["sources"])
    assert any(s.startswith("com.fulcra.labs.v1.") for s in rec["sources"])


def test_ingest_verifies_landing_and_reports_missing(monkeypatch, tmp_path):
    """Typed ingest: 201 = queued, not stored; silent drops possible. The
    store must re-query (records_visible) and report any det-ids that did
    not land, raising (→ nonzero CLI exit) — never a silent success."""
    monkeypatch.setattr(store, "_LANDING_POLL_SLEEP_S", 0)
    # visible=False -> records_visible returns empty -> everything missing.
    client, _ = make_client(_TypedRecorder(visible=False))
    ext = _extraction([_obs("Glucose", "92", "mg/dL")])

    with pytest.raises(store.LandingVerificationError) as excinfo:
        ingest_extraction(client, ext, state=LabsState())

    _, iso = parse_collected_at("2026-03-15")
    det = det_source_id("glucose", iso, 92.0)
    # The missing det-id is named in the error (surfaced to the operator via
    # cli.main -> nonzero exit); nothing was silently reported as success.
    assert det in str(excinfo.value)


def test_ingest_note_omits_absent_reference_and_flag(monkeypatch, tmp_path):
    """flag and [ref …] segments are omitted when the source has neither."""
    captured: dict = {}

    class FakePipe:
        def __init__(self, client=None):
            pass

        def ingest_typed(self, base_type, records):
            captured["records"] = records

    monkeypatch.setattr("fulcra_labs.store.IngestPipeline", FakePipe)
    monkeypatch.setattr(store, "_LANDING_POLL_SLEEP_S", 0)
    client, _ = make_client(_TypedRecorder())
    monkeypatch.setattr(client, "records_visible", _visible_after_precheck())
    ingest_extraction(client, _extraction([_obs("Glucose", "92", "mg/dL")]),
                      state=LabsState())
    note = captured["records"][0]["note"]
    assert note.startswith("92 mg/dL")   # no leading flag
    assert "[ref" not in note            # no reference-range segment


def test_create_on_first_use_once_then_cached():
    rec = _TypedRecorder()
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
    assert transport.requests == []          # no POST and no landing poll
    assert not store.state_path().exists()   # nothing persisted


def test_adopts_existing_track_from_catalog():
    rec = _TypedRecorder()
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
    assert rec.records[0]["sources"][-1].endswith("existing-glucose")


def test_cached_def_revalidated_and_reused():
    rec = _TypedRecorder()
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
    source ids. NOTE: the typed endpoint has NO server-side dedup — these
    stable ids are what the store's own client-side de-dup and landed
    verification key on (live-verified 2026-07-08)."""
    rec1 = _TypedRecorder()
    c1, _ = make_client(rec1)
    rec2 = _TypedRecorder()
    c2, _ = make_client(rec2)
    ext = _extraction([_obs("Glucose", "92", "mg/dL"), _obs("TSH", "2.1", "uIU/mL")])
    ingest_extraction(c1, ext, state=LabsState())
    ingest_extraction(c2, ext, state=LabsState())
    ids1 = sorted(r["sources"][0] for r in rec1.records)
    ids2 = sorted(r["sources"][0] for r in rec2.records)
    assert ids1 == ids2


def test_reingest_same_report_posts_nothing(monkeypatch):
    """Cross-run idempotency: the typed endpoint has NO server-side source-id
    dedup (live-verified 2026-07-08), so re-ingesting the same lab report
    must be stopped CLIENT-side — the pre-POST already-present check skips
    every det-id that is already visible, posting nothing."""
    monkeypatch.setattr(store, "_LANDING_POLL_SLEEP_S", 0)
    rec = _TypedRecorder()
    client, _ = make_client(rec)
    state = LabsState()
    ext = _extraction([_obs("Glucose", "92", "mg/dL"), _obs("TSH", "2.1", "uIU/mL")])

    first, _ = ingest_extraction(client, ext, state=state)
    assert first.ingested == 2 and len(rec.records) == 2
    assert first.skipped_already_present == 0

    second, _ = ingest_extraction(client, ext, state=state)
    assert second.ingested == 0
    assert second.skipped_already_present == 2
    assert len(rec.records) == 2      # zero new POSTs — no duplicates


def test_partial_overlap_posts_only_new_ids(monkeypatch):
    """A report that partially overlaps a prior ingest posts ONLY the new
    det-ids; the overlapping ones are skipped and reported distinctly."""
    monkeypatch.setattr(store, "_LANDING_POLL_SLEEP_S", 0)
    rec = _TypedRecorder()
    client, _ = make_client(rec)
    state = LabsState()

    ingest_extraction(client, _extraction([_obs("Glucose", "92", "mg/dL")]),
                      state=state)
    assert len(rec.records) == 1

    ext2 = _extraction([_obs("Glucose", "92", "mg/dL"),
                        _obs("TSH", "2.1", "uIU/mL")])
    second, _ = ingest_extraction(client, ext2, state=state)
    assert second.ingested == 1
    assert second.skipped_already_present == 1
    assert len(rec.records) == 2      # only TSH was posted
    _, iso = parse_collected_at("2026-03-15")
    tsh_det = det_source_id("tsh", iso, 2.1)
    assert rec.records[-1]["sources"][0] == tsh_det
    assert second.ingested_markers == ["tsh"]


def test_precheck_error_refuses_to_ingest(monkeypatch):
    """If the already-present check cannot run, labs REFUSES to ingest
    (raise, no POST) rather than risk duplicate medical records — the
    verify-before-ingest posture (opposite of media's fail-open)."""
    monkeypatch.setattr(store, "_LANDING_POLL_SLEEP_S", 0)
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user/v1alpha1/annotation" and request.method == "POST":
            return json_response(200, {"id": "def-1"})
        if path == "/data/v1alpha1/event/NumericAnnotation":
            return httpx.Response(500)   # pre-check query fails
        if path == "/ingest/v1/record/NumericAnnotation":
            posted.append(request)
            raise AssertionError("must not POST after a failed pre-check")
        raise AssertionError(f"unexpected request {request.url}")

    client, _ = make_client(handler)
    ext = _extraction([_obs("Glucose", "92", "mg/dL")])
    with pytest.raises(store.IngestPrecheckError, match="refusing to ingest"):
        ingest_extraction(client, ext, state=LabsState())
    assert posted == []


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
    rec = _TypedRecorder()
    client, _ = make_client(rec)
    # Missing unit -> review; must not ingest.
    ext = _extraction([{"marker_raw": "Glucose", "value_raw": "92", "unit_raw": None,
                        "flag_raw": None, "reference_range_raw": None}])
    outcome, _ = ingest_extraction(client, ext, state=LabsState())
    assert outcome.ingested == 0
    assert outcome.review_held == 1
    assert rec.records == []


def test_confirmed_review_key_gets_ingested():
    rec = _TypedRecorder()
    client, _ = make_client(rec)
    # Implausible value (unit mixup) -> review, but marker+value+unit resolved,
    # so an explicit --yes-reviewed can push it through.
    ext = _extraction([_obs("Glucose", "5.5", "mg/dL")])
    outcome, _ = ingest_extraction(client, ext, state=LabsState(),
                                   confirmed_keys={"glucose"})
    assert outcome.ingested == 1
    assert rec.records[0]["value"] == 5.5


def test_set_unit_at_creation_toggle(monkeypatch):
    rec = _TypedRecorder()
    captured = {}

    def handler(request):
        if request.url.path == "/user/v1alpha1/annotation" and request.method == "POST":
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
