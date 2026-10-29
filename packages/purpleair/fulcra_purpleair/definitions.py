"""Versioned per-measure v1 Metric specifications for PurpleAir."""
from __future__ import annotations

from dataclasses import dataclass
from fulcra_common.data_types import DataTypeSpec

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

    def data_type_spec(self) -> DataTypeSpec:
        return DataTypeSpec(key=f"collect.purpleair.{self.key}.v1", base_type="Metric",
                            name=self.canonical_name, description=self.description,
                            aggregation="discrete", unit=self.unit)


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
