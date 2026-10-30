"""Pure PurpleAir to v1 Metric records, keyed by full destination ID."""
from __future__ import annotations

from fulcra_common.v1 import build_metric_record

from .definitions import METRICS
from .models import Reading


def build_records(reading: Reading, destinations: dict[str, str]) -> dict[str, dict]:
    """Preserve the per-measure primary ID used by the historical converter.

    Missing sensor values produce no record. A present value without a proven
    destination fails the sample, so a cursor cannot silently skip a measure.
    """
    records = {}
    for metric in METRICS:
        value = getattr(reading, metric.reading_attr)
        if value is None:
            continue
        destination = destinations.get(metric.key)
        if not destination:
            raise ValueError("purpleair: missing v1 metric destination")
        if destination in records:
            raise ValueError("purpleair: measures require distinct v1 destinations")
        records[destination] = build_metric_record(
            data_type=destination, start_time=reading.observed_at,
            source_id=f"{reading.dedup_key()}:{metric.key}", value=value, unit=metric.unit,
        )
    return records
