"""V1 metric records preserve source identity, units and stable UUIDs."""
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fulcra_purpleair.definitions import METRICS
from fulcra_purpleair.ingest import build_records
from fulcra_purpleair.models import Reading

DESTINATIONS = {m.key: f"Metric/11111111-2222-4333-8444-{i:012d}" for i, m in enumerate(METRICS)}


def reading(**overrides):
    return Reading(**{**dict(sensor_id="synthetic", observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                             pm2_5=0, pm10=9.1, aqi=35, temperature_f=72, humidity=45, pressure=1013.2),
                      **overrides})


def test_full_reading_has_six_metric_destinations_units_and_primary_ids():
    records = build_records(reading(), DESTINATIONS)
    assert set(records) == set(DESTINATIONS.values())
    for metric in METRICS:
        record = records[DESTINATIONS[metric.key]]
        assert record["sources"] == [f"{reading().dedup_key()}:{metric.key}"]
        assert record["unit"] == metric.unit
        assert record["value"] == getattr(reading(), metric.reading_attr)
        assert str(UUID(record["id"])) == record["id"]
        assert record["start_time"] == record["end_time"] == "2026-01-02T00:00:00Z"
    assert build_records(reading(), DESTINATIONS) == records


def test_missing_values_are_omitted_but_missing_destinations_fail():
    assert len(build_records(reading(pm10=None, pressure=None), DESTINATIONS)) == 4
    with pytest.raises(ValueError, match="destination"):
        build_records(reading(), {k: v for k, v in DESTINATIONS.items() if k != "humidity"})


def test_empty_sample_does_not_create_records():
    assert build_records(reading(pm2_5=None, pm10=None, aqi=None, temperature_f=None, humidity=None,
                                 pressure=None), {}) == {}


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "3"])
def test_invalid_sensor_value_cannot_be_coerced_or_partially_sent(value):
    with pytest.raises(ValueError):
        build_records(reading(pm2_5=value), DESTINATIONS)


def test_actual_full_v1_metric_destination_is_required():
    with pytest.raises(ValueError):
        build_records(reading(), {k: "old-definition" for k in DESTINATIONS})
