"""Consent must fail safe on legacy / malformed grant data, not crash.

Grants are read back as raw dicts with no schema validation. A grant missing
audience or key_glob (legacy file, partial write, hand edit) must be treated as
inactive/non-matching — the consent-gated export path should never raise on it.
"""
from datetime import datetime, timezone

from fulcra_prefs.consent import filter_for_audience, _active

NOW = datetime(2026, 6, 12, tzinfo=timezone.utc)
DOC = {"v": 1, "compiled_at": "x",
       "keys": {"dining.cuisine": {"value": "thai", "weight": 0.5}}}


def test_active_false_for_grant_missing_audience():
    assert _active({"key_glob": "*", "expires": None}, "ea", NOW) is False


def test_filter_ignores_grant_missing_audience():
    out = filter_for_audience(DOC, [{"key_glob": "*", "expires": None}], "ea", NOW)
    assert out["keys"] == {}


def test_filter_ignores_active_grant_missing_key_glob():
    # audience matches (grant is "active") but it has no key_glob -> skip, no crash
    out = filter_for_audience(DOC, [{"audience": "ea", "expires": None}], "ea", NOW)
    assert out["keys"] == {}


def test_active_handles_naive_now_against_aware_expires():
    # `now` may arrive tz-naive (a caller using datetime.now() w/o tz) while the
    # grant's expires carries an offset. Coerce to a common UTC basis rather than
    # raising TypeError on the comparison -- mirrors decay._age_days.
    naive_now = datetime(2026, 6, 16, 2, 0, 0)
    future = {"audience": "ea", "key_glob": "*",
              "expires": "2026-12-31T00:00:00+00:00"}
    past = {"audience": "ea", "key_glob": "*",
            "expires": "2026-01-01T00:00:00+00:00"}
    assert _active(future, "ea", naive_now) is True
    assert _active(past, "ea", naive_now) is False
