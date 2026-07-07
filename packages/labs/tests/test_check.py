"""Double-extraction cross-check tests."""
from __future__ import annotations

from fulcra_labs.check import cross_check


def test_identical_passes_all_agree(load_fixture):
    a = load_fixture("labcorp_pass_a.json")
    result = cross_check(a, a)
    assert len(result.observations) == len(a["observations"])
    assert result.disagreements == []


def test_one_value_differs_is_flagged(load_fixture):
    a = load_fixture("labcorp_pass_a.json")
    b = load_fixture("labcorp_pass_b.json")  # triglycerides 120 vs 130
    result = cross_check(a, b)
    assert len(result.observations) == 9
    assert len(result.disagreements) == 1
    d = result.disagreements[0]
    assert d["marker"] == "triglycerides"
    assert d["reason"] == "value/unit differ between passes"


def test_missing_in_one_pass_is_disagreement(load_fixture):
    a = load_fixture("labcorp_pass_a.json")
    b = {**a, "observations": a["observations"][:-1]}  # drop last row
    result = cross_check(a, b)
    reasons = {d["reason"] for d in result.disagreements}
    assert "present in only one pass" in reasons


def test_agreed_extraction_is_ingest_ready(load_fixture):
    a = load_fixture("labcorp_pass_a.json")
    b = load_fixture("labcorp_pass_b.json")
    ext = cross_check(a, b).agreed_extraction()
    assert set(ext) == {"lab", "report_date", "collected_at", "observations"}
    assert all("marker_raw" in o for o in ext["observations"])


def test_collection_date_mismatch_flagged(load_fixture):
    a = load_fixture("labcorp_pass_a.json")
    b = {**a, "collected_at": "2026-03-14"}
    result = cross_check(a, b)
    assert any(d["marker"] == "__collected_at__" for d in result.disagreements)
