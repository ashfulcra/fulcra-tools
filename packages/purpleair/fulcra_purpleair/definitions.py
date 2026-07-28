"""The six per-measure Fulcra definitions a PurpleAir reading writes to.

Fulcra ships no air-quality metrics, so each measure becomes its own
NumericAnnotation definition (find-or-create by canonical name via
``RunContext.resolved_definition_id``), mirroring the per-marker track
pattern from fulcra-labs. One reading fans out to up to six records, each
carrying its own unit; over time every PurpleAir sensor's PM2.5 (etc.)
lands on the same track so it reads as one time series in Context Web.
"""
from __future__ import annotations

from dataclasses import dataclass

# Every PurpleAir definition is a NumericAnnotation. The resolver needs only
# the annotation_type in the expected_spec; the unit rides on each record.
NUMERIC_EXPECTED_SPEC: dict = {"annotation_type": "NumericAnnotation"}


@dataclass(frozen=True)
class MetricDef:
    """One air-quality measure: its Fulcra definition + how to read it.

    ``reading_attr`` is the attribute on ``models.Reading`` that supplies the
    value; ``canonical_name`` is the stable find-or-create key in Fulcra;
    ``unit`` is attached to every record written for this measure.
    """
    key: str
    canonical_name: str
    unit: str
    reading_attr: str
    description: str

    def create_extra(self) -> dict:
        """Fields merged into the definition POST body ONLY on first create.

        Kept to ``description`` — the unit is authoritative per-record via
        ``wire.build_typed_record(unit=...)``, so we don't second-guess the
        definition-create schema with a unit key it may not accept.
        """
        return {"description": self.description}


METRICS: tuple[MetricDef, ...] = (
    MetricDef(
        key="pm2_5",
        canonical_name="PM2.5",
        unit="ug/m3",
        reading_attr="pm2_5",
        description="Fine particulate matter (PM2.5) mass concentration from a PurpleAir sensor.",
    ),
    MetricDef(
        key="pm10",
        canonical_name="PM10",
        unit="ug/m3",
        reading_attr="pm10",
        description="Coarse particulate matter (PM10) mass concentration from a PurpleAir sensor.",
    ),
    MetricDef(
        key="aqi",
        canonical_name="Air Quality Index",
        unit="AQI",
        reading_attr="aqi",
        description="US EPA Air Quality Index, derived from a PurpleAir sensor's PM2.5.",
    ),
    MetricDef(
        key="temperature",
        canonical_name="Temperature (PurpleAir)",
        unit="degF",
        reading_attr="temperature_f",
        description="Ambient temperature reported by a PurpleAir sensor (raw onboard reading; runs ~8 degF high).",
    ),
    MetricDef(
        key="humidity",
        canonical_name="Humidity (PurpleAir)",
        unit="%",
        reading_attr="humidity",
        description="Relative humidity reported by a PurpleAir sensor.",
    ),
    MetricDef(
        key="pressure",
        canonical_name="Barometric Pressure",
        unit="hPa",
        reading_attr="pressure",
        description="Barometric pressure reported by a PurpleAir sensor.",
    ),
)
