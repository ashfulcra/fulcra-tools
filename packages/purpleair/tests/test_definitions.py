"""The metric definitions stay consistent with the Reading model."""
from __future__ import annotations

from dataclasses import fields

from fulcra_purpleair.definitions import METRICS, NUMERIC_EXPECTED_SPEC
from fulcra_purpleair.models import Reading


def test_six_metrics_unique_keys_and_names():
    assert len(METRICS) == 6
    assert len({m.key for m in METRICS}) == 6
    assert len({m.canonical_name for m in METRICS}) == 6


def test_every_metric_maps_to_a_real_reading_attribute():
    reading_attrs = {f.name for f in fields(Reading)}
    for m in METRICS:
        assert m.reading_attr in reading_attrs, m.key


def test_expected_spec_is_numeric():
    assert NUMERIC_EXPECTED_SPEC == {"annotation_type": "NumericAnnotation"}


def test_create_extra_carries_description_not_unit():
    for m in METRICS:
        extra = m.create_extra()
        assert extra["description"]
        assert "unit" not in extra  # unit is per-record, not on the def create
