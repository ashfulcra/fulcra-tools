"""Normalized PurpleAir reading + the EPA PM2.5 AQI conversion.

Both acquisition modes (cloud API and LAN /json) parse into the same
``Reading`` so the ingest path is source-agnostic. AQI is *derived* here
because neither source reports it: PurpleAir gives raw particulate mass,
and the US EPA AQI is a piecewise-linear function of truncated PM2.5.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# US EPA PM2.5 -> AQI breakpoints: (conc_low, conc_high, aqi_low, aqi_high).
# Concentrations are truncated to 1 decimal (EPA rule) before lookup.
_PM25_BREAKPOINTS: tuple[tuple[float, float, int, int], ...] = (
    (0.0, 12.0, 0, 50),
    (12.1, 35.4, 51, 100),
    (35.5, 55.4, 101, 150),
    (55.5, 150.4, 151, 200),
    (150.5, 250.4, 201, 300),
    (250.5, 350.4, 301, 400),
    (350.5, 500.4, 401, 500),
)


def pm25_to_aqi(pm2_5: float | None) -> int | None:
    """Convert a PM2.5 mass concentration (ug/m3) to a US EPA AQI value.

    Returns ``None`` for a missing reading. Above the 500.4 ug/m3 top of the
    scale the AQI is capped at 500 (EPA "beyond the AQI"): we do not
    extrapolate a fictitious number.
    """
    if pm2_5 is None:
        return None
    conc = int(pm2_5 * 10) / 10.0  # truncate to 1 decimal, do not round
    if conc < 0:
        return None
    if conc > 500.4:
        return 500
    for c_lo, c_hi, a_lo, a_hi in _PM25_BREAKPOINTS:
        if c_lo <= conc <= c_hi:
            return round((a_hi - a_lo) / (c_hi - c_lo) * (conc - c_lo) + a_lo)
    return None


@dataclass(frozen=True)
class Reading:
    """One PurpleAir sample, normalized across the API and LAN sources.

    ``sensor_id`` is the stable identity used for dedup (API sensor index or
    LAN SensorId). ``observed_at`` is the sensor's own timestamp, not fetch
    time, so re-fetching the same sample dedups correctly. Any field the
    source omits is ``None`` and simply isn't written.
    """
    sensor_id: str
    observed_at: datetime
    pm2_5: float | None = None
    pm10: float | None = None
    aqi: int | None = None
    temperature_f: float | None = None
    humidity: float | None = None
    pressure: float | None = None

    def dedup_key(self) -> str:
        """Deterministic per-sample key: sensor identity + observation time."""
        return f"purpleair:{self.sensor_id}:{int(self.observed_at.timestamp())}"
