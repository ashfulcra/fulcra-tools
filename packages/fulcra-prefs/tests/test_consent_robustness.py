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
