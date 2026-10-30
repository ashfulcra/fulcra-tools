"""Retained local legacy evidence/source identity helpers.

Delivery/creation/review/verification contracts now exercise actual native Click
and kernel behavior in test_native_v1, rather than the retired typed writer.
"""

from fulcra_labs import store
from fulcra_labs.store import LabsState, load_state, save_state
from fulcra_labs.validate import det_source_id


def test_det_id_is_deterministic():
    a = det_source_id("glucose", "2026-03-15T00:00:00Z", 92.0)
    b = det_source_id("glucose", "2026-03-15T00:00:00Z", 92.0)
    assert a == b and a.startswith("com.fulcra.labs.v1.")
    assert a != det_source_id("glucose", "2026-03-15T00:00:00Z", 93.0)


def test_retained_legacy_state_reads_without_modifying_original(tmp_path):
    p = tmp_path / "markers.json"
    s = LabsState()
    s.markers["glucose"] = store.MarkerEntry(
        "11111111-2222-4333-8444-555555555555", "mg/dL", "2026-01-01T00:00:00Z"
    )
    s.last_ingest = "2026-03-15T00:00:00Z"
    import json

    original = json.dumps(s.to_dict())
    p.write_text(original)
    loaded = load_state(p)
    assert loaded.markers["glucose"].def_id == "11111111-2222-4333-8444-555555555555"
    assert loaded.last_ingest == "2026-03-15T00:00:00Z"
    assert p.read_text() == original


def test_legacy_loader_unknown_never_clean_empty(tmp_path):
    import pytest

    p = tmp_path / "markers.json"
    p.write_text("{broken")
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        load_state(p)


def test_retired_legacy_cache_writer_never_overwrites(tmp_path):
    import pytest

    path = tmp_path / "markers.json"
    path.write_text("retained original bytes")
    with pytest.raises(RuntimeError, match="retired"):
        save_state(LabsState(), path)
    assert path.read_text() == "retained original bytes"
