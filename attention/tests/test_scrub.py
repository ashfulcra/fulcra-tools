"""Tier 1 param-strip: drop auth/tracking params from query+fragment."""
from __future__ import annotations

import json as _json
from pathlib import Path as _Path

import pytest

from fulcra_attention.scrub import scrub_url

_FIXTURE = _Path(__file__).parent / "fixtures" / "scrub_cases.json"
CASES = [(c["input"], c["expected"]) for c in _json.loads(_FIXTURE.read_text())]


@pytest.mark.parametrize("raw,expected", CASES, ids=[c[0] for c in CASES])
def test_scrub_url(raw: str, expected: str):
    assert scrub_url(raw) == expected
