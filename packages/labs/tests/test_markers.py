"""Registry tests — conversions are load-bearing, so they are pinned hard."""
from __future__ import annotations

import pytest

from fulcra_labs import markers as m


def test_every_marker_has_identity_canonical_unit():
    for mk in m.all_markers():
        cu = m.normalize_unit(mk.canonical_unit)
        assert cu in mk.accepted_units, f"{mk.key} canonical unit not accepted"
        assert mk.accepted_units[cu] == (1.0, 0.0), f"{mk.key} canonical != identity"


def test_plausible_ranges_well_formed():
    for mk in m.all_markers():
        lo, hi = mk.plausible_range
        assert lo < hi, f"{mk.key} range not ordered"
        assert mk.loinc is None or isinstance(mk.loinc, str)


def test_registry_size():
    keys = [mk.key for mk in m.all_markers()]
    assert len(keys) == len(set(keys)), "duplicate marker key"
    assert len(keys) >= 45


@pytest.mark.parametrize("key,unit,raw,expected", [
    ("glucose", "mmol/L", 5.5, 99.088),          # 5.5 * 18.016
    ("total-cholesterol", "mmol/L", 5.0, 193.35),  # 5.0 * 38.67
    ("triglycerides", "mmol/L", 2.0, 177.14),      # 2.0 * 88.57
    ("creatinine", "umol/L", 88.42, 1.00019504),   # 88.42 / 88.42
    ("calcium", "mmol/L", 2.4, 9.6192),            # 2.4 * 4.008
    ("iron", "umol/L", 17.9, 100.0073),            # 17.9 * 5.587
    ("free-t4", "pmol/L", 12.87, 1.0000),          # 12.87 / 12.87
    ("hba1c", "mmol/mol", 38.0, 5.62824),          # 0.09148*38 + 2.152 (AFFINE)
])
def test_conversions(key, unit, raw, expected):
    mk = m.BY_KEY[key]
    un = m.normalize_unit(unit)
    assert un in mk.accepted_units
    assert mk.convert(raw, un) == pytest.approx(expected, rel=1e-4)


def test_multiplicative_conversions_round_trip():
    """For every non-identity, non-affine (offset==0) unit, converting a
    canonical value out and back is lossless."""
    for mk in m.all_markers():
        for unit_norm, (factor, offset) in mk.accepted_units.items():
            if offset != 0.0 or factor == 1.0:
                continue
            canonical = (mk.plausible_range[0] + mk.plausible_range[1]) / 2
            raw = (canonical - offset) / factor
            assert mk.convert(raw, unit_norm) == pytest.approx(canonical, rel=1e-9)


@pytest.mark.parametrize("printed,key", [
    ("Cholesterol, Total", "total-cholesterol"),
    ("LDL Chol Calc (NIH)", "ldl-c"),
    ("T4,Free(Direct)", "free-t4"),
    ("GLUCOSE", "glucose"),
    ("Hemoglobin A1c", "hba1c"),
    ("Vitamin D, 25-Hydroxy", "vitamin-d-25oh"),
    ("PSA, TOTAL", "psa"),
    ("WHITE BLOOD CELL COUNT", "wbc"),
])
def test_alias_resolution(printed, key):
    res = m.resolve_marker(printed)
    assert res.marker is not None and res.marker.key == key


def test_unknown_marker_offers_suggestions_not_a_match():
    res = m.resolve_marker("Cholesteral Totl")  # misspelled
    assert res.marker is None
    # Fuzzy suggestion is a HINT only — never an auto-resolution.
    assert "total-cholesterol" in res.suggestions


@pytest.mark.parametrize("printed", ["10^3/µL", "x10E3/uL", "K/uL", "10*3/uL"])
def test_wbc_unit_variants_normalize_into_accepted(printed):
    mk = m.BY_KEY["wbc"]
    assert m.normalize_unit(printed) in mk.accepted_units


def test_glucose_unit_mixup_is_out_of_range_but_in_range_when_mmol():
    mk = m.BY_KEY["glucose"]
    lo, hi = mk.plausible_range
    # 5.5 mislabelled mg/dL is implausibly low.
    assert not (lo <= 5.5 <= hi)
    # 5.5 mmol/L converts to a normal fasting glucose.
    assert lo <= mk.convert(5.5, m.normalize_unit("mmol/L")) <= hi
