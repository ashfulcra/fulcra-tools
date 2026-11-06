"""The metric definitions stay consistent with the Reading model."""
from __future__ import annotations

from dataclasses import fields

from fulcra_purpleair.definitions import METRICS
from fulcra_purpleair.models import Reading


def test_six_metrics_unique_keys_and_names():
    assert len(METRICS) == 6
    assert len({m.key for m in METRICS}) == 6
    assert len({m.canonical_name for m in METRICS}) == 6


def test_every_metric_maps_to_a_real_reading_attribute():
    reading_attrs = {f.name for f in fields(Reading)}
    for m in METRICS:
        assert m.reading_attr in reading_attrs, m.key


def test_specs_are_distinct_v1_metrics_with_authoritative_units():
    specs = [m.data_type_spec() for m in METRICS]
    assert len({s.key for s in specs}) == 6
    for metric, spec in zip(METRICS, specs):
        body = spec.create_body()
        assert spec.base_type == "Metric"
        assert spec.key == f"collect.purpleair.{metric.key}.v1"
        assert body["record_spec"]["type"] == "metric"
        assert body["record_spec"]["aggregation"] == "discrete"
        assert body["record_spec"]["unit"] == metric.unit
        assert body["description"] == metric.description


def test_collect_contract_exposes_all_six_v1_destination_slots():
    from fulcra_purpleair.collect_plugin import PLUGIN
    specs = PLUGIN.record_data_types({})
    assert set(specs) == {m.key for m in METRICS}
    assert all(spec.base_type == "Metric" for spec in specs.values())
