"""Fetch + parse PurpleAir readings from the cloud API or a LAN sensor.

Parsing is separated from I/O so it is trivially unit-testable: the parse
functions take an already-decoded JSON dict and never touch the network.
The ``fetch_*`` helpers do the HTTP and delegate to the parsers; ``httpx``
is imported lazily inside them so importing this module (and unit-testing
the parsers) needs no third-party deps.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .models import Reading, pm25_to_aqi

PURPLEAIR_API_BASE = "https://api.purpleair.com/v1"

# Fields we request from the cloud API. AQI is derived locally (the API does
# not return it), so it is intentionally absent here.
_API_FIELDS = ("pm2.5", "pm10.0", "humidity", "temperature", "pressure", "last_seen")


def _f(value: object) -> float | None:
    """Coerce a JSON scalar to float, tolerating missing/blank/non-numeric."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_api_response(payload: dict) -> Reading:
    """Parse a PurpleAir ``GET /v1/sensors/<index>`` body into a Reading.

    Shape: ``{"sensor": {"sensor_index": N, "last_seen": <epoch>, "pm2.5": ...}}``.
    """
    sensor = payload.get("sensor") or {}
    idx = sensor.get("sensor_index")
    last_seen = sensor.get("last_seen")
    observed_at = (
        datetime.fromtimestamp(int(last_seen), tz=timezone.utc)
        if last_seen is not None
        else datetime.now(tz=timezone.utc)
    )
    pm2_5 = _f(sensor.get("pm2.5"))
    return Reading(
        sensor_id=str(idx) if idx is not None else "unknown",
        observed_at=observed_at,
        pm2_5=pm2_5,
        pm10=_f(sensor.get("pm10.0")),
        aqi=pm25_to_aqi(pm2_5),
        temperature_f=_f(sensor.get("temperature")),
        humidity=_f(sensor.get("humidity")),
        pressure=_f(sensor.get("pressure")),
    )


def parse_local_response(payload: dict) -> Reading:
    """Parse a LAN sensor ``GET http://<ip>/json`` body into a Reading.

    The LAN payload is flat and uses ATM-corrected particulate fields; we take
    channel A (``*_atm``) as primary.
    """
    sensor_id = str(payload.get("SensorId") or payload.get("Id") or "unknown")
    observed_at = _parse_local_datetime(payload.get("DateTime"))
    pm2_5 = _f(payload.get("pm2_5_atm"))
    return Reading(
        sensor_id=sensor_id,
        observed_at=observed_at,
        pm2_5=pm2_5,
        pm10=_f(payload.get("pm10_0_atm")),
        aqi=pm25_to_aqi(pm2_5),
        temperature_f=_f(payload.get("current_temp_f")),
        humidity=_f(payload.get("current_humidity")),
        pressure=_f(payload.get("pressure")),
    )


def _parse_local_datetime(raw: object) -> datetime:
    """LAN DateTime looks like ``2026/07/22T20:26:10z``; fall back to now(utc)."""
    if isinstance(raw, str):
        for fmt in ("%Y/%m/%dT%H:%M:%Sz", "%Y/%m/%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return datetime.now(tz=timezone.utc)


def fetch_api(
    sensor_index: str,
    api_key: str,
    *,
    timeout: float = 15.0,
    transport: "object | None" = None,
) -> Reading:
    """Fetch one reading from the cloud API for ``sensor_index``.

    ``transport`` (an ``httpx.BaseTransport``) is injectable so tests drive a
    ``MockTransport`` instead of the network — mirroring ``BaseFulcraClient``.
    """
    import httpx  # lazy: keeps the parsers dependency-free

    url = f"{PURPLEAIR_API_BASE}/sensors/{sensor_index}"
    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.get(
            url,
            params={"fields": ",".join(_API_FIELDS)},
            headers={"X-API-Key": api_key},
        )
    resp.raise_for_status()
    return parse_api_response(resp.json())


def fetch_local(
    sensor_ip: str,
    *,
    timeout: float = 10.0,
    transport: "object | None" = None,
) -> Reading:
    """Fetch one reading from a LAN sensor's JSON endpoint."""
    import httpx  # lazy

    with httpx.Client(transport=transport, timeout=timeout) as client:
        resp = client.get(f"http://{sensor_ip}/json")
    resp.raise_for_status()
    return parse_local_response(resp.json())
