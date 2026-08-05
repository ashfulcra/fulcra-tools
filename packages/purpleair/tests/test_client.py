"""Parser + AQI unit tests (pure) + fetch tests over httpx MockTransport."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx

from fulcra_purpleair.client import (
    fetch_api,
    fetch_local,
    parse_api_response,
    parse_local_response,
)
from fulcra_purpleair.models import pm25_to_aqi


def test_pm25_to_aqi_breakpoints():
    assert pm25_to_aqi(0.0) == 0
    assert pm25_to_aqi(12.0) == 50
    assert pm25_to_aqi(12.1) == 51
    assert pm25_to_aqi(35.4) == 100
    assert pm25_to_aqi(35.5) == 101
    assert pm25_to_aqi(8.3) == 35  # (50/12)*8.3 = 34.58 -> 35
    assert pm25_to_aqi(600.0) == 500  # capped beyond the scale
    assert pm25_to_aqi(None) is None


def test_pm25_to_aqi_truncates_not_rounds():
    # 12.09 truncates to 12.0 (band 0, top=50), NOT up into band 1.
    assert pm25_to_aqi(12.09) == 50


def test_parse_api_response():
    payload = {
        "sensor": {
            "sensor_index": 90210,
            "last_seen": 1_690_000_000,
            "pm2.5": 8.3,
            "pm10.0": 9.1,
            "humidity": 45,
            "temperature": 72,
            "pressure": 1013.2,
        }
    }
    r = parse_api_response(payload)
    assert r.sensor_id == "90210"
    assert r.observed_at == datetime.fromtimestamp(1_690_000_000, tz=timezone.utc)
    assert r.pm2_5 == 8.3
    assert r.pm10 == 9.1
    assert r.aqi == 35
    assert r.temperature_f == 72.0
    assert r.humidity == 45.0
    assert r.pressure == 1013.2
    assert r.dedup_key() == "purpleair:90210:1690000000"


def test_parse_local_response():
    payload = {
        "SensorId": "aa:bb:cc:dd:ee:ff",
        "DateTime": "2026/07/22T20:26:10z",
        "pm2_5_atm": 35.5,
        "pm10_0_atm": 40.0,
        "current_temp_f": 68,
        "current_humidity": 50,
        "pressure": 1008.7,
    }
    r = parse_local_response(payload)
    assert r.sensor_id == "aa:bb:cc:dd:ee:ff"
    assert r.observed_at == datetime(2026, 7, 22, 20, 26, 10, tzinfo=timezone.utc)
    assert r.pm2_5 == 35.5
    assert r.aqi == 101
    assert r.temperature_f == 68.0
    assert r.humidity == 50.0


def test_parse_tolerates_missing_and_blank_fields():
    r = parse_api_response({"sensor": {"sensor_index": 1, "last_seen": 1, "pm2.5": ""}})
    assert r.pm2_5 is None
    assert r.aqi is None
    assert r.pm10 is None
    assert r.temperature_f is None


def test_fetch_api_sends_key_and_fields_and_parses():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={
            "sensor": {"sensor_index": 90210, "last_seen": 1_690_000_000,
                       "pm2.5": 8.3, "pm10.0": 9.1},
        })

    r = fetch_api("90210", "secret-key", transport=httpx.MockTransport(handler))
    assert seen["url"].startswith("https://api.purpleair.com/v1/sensors/90210")
    assert "fields=" in seen["url"]
    assert seen["key"] == "secret-key"
    assert r.sensor_id == "90210"
    assert r.pm2_5 == 8.3
    assert r.aqi == 35


def test_fetch_local_hits_json_endpoint_and_parses():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://10.0.0.5/json"
        return httpx.Response(200, json={
            "SensorId": "aa:bb", "DateTime": "2026/07/22T20:26:10z",
            "pm2_5_atm": 35.5, "pm10_0_atm": 40.0,
        })

    r = fetch_local("10.0.0.5", transport=httpx.MockTransport(handler))
    assert r.sensor_id == "aa:bb"
    assert r.pm2_5 == 35.5
    assert r.aqi == 101


def test_fetch_api_raises_on_http_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "bad key"})

    import pytest
    with pytest.raises(httpx.HTTPStatusError):
        fetch_api("1", "nope", transport=httpx.MockTransport(handler))
