"""Turn a normalized :class:`~fulcra_purpleair.models.Reading` into Fulcra
NumericAnnotation records and post them.

The build step is pure (no I/O) so it is trivially unit-testable; the post
step wraps the shared :class:`fulcra_common.ingest.IngestPipeline` typed
endpoint. One reading fans out to up to six records — one per measure that
carries a value — each bound to its own per-measure definition and unit.
"""
from __future__ import annotations

from collections.abc import Iterable

from fulcra_common import wire
from fulcra_common.client import BaseFulcraClient
from fulcra_common.ingest import IngestPipeline

from .definitions import METRICS
from .models import Reading

BASE_TYPE = "NumericAnnotation"


def build_records(reading: Reading, definition_ids: dict[str, str]) -> list[dict]:
    """Build the typed NumericAnnotation records for one reading.

    Skips any measure whose reading value is ``None`` (the source omitted
    it) or whose definition id is missing from ``definition_ids`` (defensive
    — a resolve that returned nothing must not emit a record with no def).

    ``source_id`` is ``<reading.dedup_key()>:<metric.key>`` so each measure
    of each sample has a stable, unique identity; the unit rides on the
    record per :func:`wire.build_typed_record`.
    """
    records: list[dict] = []
    for metric in METRICS:
        value = getattr(reading, metric.reading_attr)
        if value is None:
            continue
        definition_id = definition_ids.get(metric.key)
        if not definition_id:
            continue
        records.append(
            wire.build_typed_record(
                base_type=BASE_TYPE,
                start_time=reading.observed_at,
                source_id=f"{reading.dedup_key()}:{metric.key}",
                value=float(value),
                unit=metric.unit,
                definition_id=definition_id,
            )
        )
    return records


def post_records(client: BaseFulcraClient, records: Iterable[dict]) -> None:
    """POST already-built records to the typed NumericAnnotation endpoint.

    No-ops on an empty batch. The typed endpoint does NO server-side
    source-id dedup (see ``IngestPipeline.ingest_typed``), so the caller
    must guard re-processing before it gets here — the plugin does that with
    the daemon-backed per-reading ``claim_dedup_keys``.
    """
    records = list(records)
    if not records:
        return
    IngestPipeline(client).ingest_typed(BASE_TYPE, records)
