"""Verification-engine tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fulcra_labs import validate as V


def _obs(marker, value, unit=None, flag=None, ref=None):
    return {"marker_raw": marker, "value_raw": value, "unit_raw": unit,
            "flag_raw": flag, "reference_range_raw": ref}


def _extraction(observations, collected="2026-03-15", lab="LabCorp"):
    return {"lab": lab, "report_date": collected, "collected_at": collected,
            "observations": observations}


@pytest.mark.parametrize("raw,num,qual", [
    ("<0.1", 0.1, V.QUALIFIER_LT),
    (">300", 300.0, V.QUALIFIER_GT),
    ("≤5", 5.0, V.QUALIFIER_LT),
    ("1,234", 1234.0, None),
    ("92", 92.0, None),
    ("Negative", None, None),
    ("", None, None),
])
def test_parse_value(raw, num, qual):
    assert V.parse_value(raw) == (num, qual)


def test_ok_row_gets_deterministic_id():
    rep = V.validate_extraction(_extraction([_obs("Glucose", "92", "mg/dL")]))
    v = rep.verdicts[0]
    assert v.verdict == V.OK
    assert v.canonical_value == 92.0
    assert v.det_source_id == V.det_source_id("glucose", v.collected_at, 92.0)


def test_mmol_glucose_converts_and_is_ok():
    rep = V.validate_extraction(_extraction([_obs("Glucose", "5.5", "mmol/L")]))
    v = rep.verdicts[0]
    assert v.verdict == V.OK
    assert v.canonical_value == pytest.approx(99.088, rel=1e-4)
    assert v.canonical_unit == "mg/dL"


def test_unit_mixup_flagged_review():
    rep = V.validate_extraction(_extraction([_obs("Glucose", "5.5", "mg/dL")]))
    v = rep.verdicts[0]
    assert v.verdict == V.REVIEW
    assert any("implausible" in r for r in v.reasons)


def test_missing_unit_is_review_never_inferred():
    rep = V.validate_extraction(_extraction([_obs("Glucose", "92", None)]))
    v = rep.verdicts[0]
    assert v.verdict == V.REVIEW
    assert any("missing unit" in r for r in v.reasons)
    assert v.canonical_value is None


def test_unknown_unit_is_review():
    rep = V.validate_extraction(_extraction([_obs("Glucose", "92", "furlongs")]))
    v = rep.verdicts[0]
    assert v.verdict == V.REVIEW
    assert any("unknown unit" in r for r in v.reasons)


def test_unknown_marker_is_review_not_reject():
    rep = V.validate_extraction(_extraction([_obs("Flibbertigibbet", "5", "mg/dL")]))
    v = rep.verdicts[0]
    assert v.verdict == V.REVIEW
    assert any("unresolved marker" in r for r in v.reasons)


def test_non_numeric_value_is_reject():
    rep = V.validate_extraction(_extraction([_obs("Glucose", "Negative", "mg/dL")]))
    v = rep.verdicts[0]
    assert v.verdict == V.REJECT


def test_future_date_rejects_every_row():
    future = "2999-01-01"
    rep = V.validate_extraction(_extraction([_obs("Glucose", "92", "mg/dL")], collected=future))
    assert rep.verdicts[0].verdict == V.REJECT
    assert any("future" in r for r in rep.verdicts[0].reasons)


def test_pre_1990_date_rejects():
    rep = V.validate_extraction(_extraction([_obs("Glucose", "92", "mg/dL")], collected="1980-06-01"))
    assert rep.verdicts[0].verdict == V.REJECT
    assert any("before 1990" in r for r in rep.verdicts[0].reasons)


def test_in_batch_duplicate_detection():
    rep = V.validate_extraction(_extraction([
        _obs("Glucose", "92", "mg/dL"),
        _obs("Glucose", "92", "mg/dL"),
    ]))
    assert rep.verdicts[0].verdict == V.OK
    assert rep.verdicts[1].verdict == V.REVIEW
    assert any("duplicate" in r for r in rep.verdicts[1].reasons)


def test_qualifier_preserved_on_ok_row():
    rep = V.validate_extraction(_extraction([_obs("PSA, TOTAL", "<0.1", "ng/mL")]))
    v = rep.verdicts[0]
    assert v.verdict == V.OK
    assert v.qualifier == V.QUALIFIER_LT
    assert v.canonical_value == 0.1


def test_labcorp_fixture_all_ok(load_fixture):
    rep = V.validate_extraction(load_fixture("labcorp_pass_a.json"),
                                now=datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert rep.counts[V.OK] == 10
    assert rep.counts[V.REVIEW] == 0
    assert rep.counts[V.REJECT] == 0


def test_quest_fixture_conversions_ok(load_fixture):
    rep = V.validate_extraction(load_fixture("quest_pass_a.json"),
                                now=datetime(2026, 6, 1, tzinfo=timezone.utc))
    by_key = {v.marker_key: v for v in rep.verdicts}
    assert by_key["glucose"].canonical_value == pytest.approx(99.088, rel=1e-4)
    assert by_key["creatinine"].canonical_value == pytest.approx(0.9007, rel=1e-3)
    assert by_key["hba1c"].canonical_value == pytest.approx(5.62824, rel=1e-4)
    assert by_key["psa"].qualifier == V.QUALIFIER_LT
    assert all(v.verdict == V.OK for v in rep.verdicts)
