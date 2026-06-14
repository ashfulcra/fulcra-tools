"""Signed zero must canonicalize identically to positive zero.

round() can produce -0.0 (e.g. round(-1e-9, 6)), and json.dumps emits "-0.0"
while 0.0 emits "0.0". Two mathematically-equal values then serialize to
different bytes, violating the byte-identical determinism contract.
"""
from fulcra_prefs.schema import canonical_json


def test_signed_zero_canonicalizes_identically():
    assert canonical_json(-0.0) == canonical_json(0.0) == "0.0"


def test_tiny_negative_rounds_to_unsigned_zero():
    assert canonical_json(-1e-9) == "0.0"


def test_signed_zero_inside_structure():
    assert canonical_json({"w": -0.0}) == canonical_json({"w": 0.0})
