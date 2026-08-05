"""Record building (pure) + the typed ingest POST (httpx MockTransport)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx

from fulcra_common.client import BaseFulcraClient
from fulcra_purpleair.ingest import build_records, post_records
from fulcra_purpleair.models import Reading

_DEF_IDS = {
    "pm2_5": "d-pm25",
    "pm10": "d-pm10",
    "aqi": "d-aqi",
    "temperature": "d-temp",
    "humidity": "d-hum",
    "pressure": "d-pres",
}


def _reading(**kw) -> Reading:
    base = dict(
        sensor_id="90210",
        observed_at=datetime(2026, 7, 22, 20, 0, 0, tzinfo=timezone.utc),
        pm2_5=8.3,
        pm10=9.1,
        aqi=35,
        temperature_f=72.0,
        humidity=45.0,
        pressure=1013.2,
    )
    base.update(kw)
    return Reading(**base)


def test_build_records_full_reading_fans_out_to_six():
    r = _reading()
    records = build_records(r, _DEF_IDS)
    assert len(records) == 6
    by_unit = {rec["value"]: rec["unit"] for rec in records}
    assert by_unit[8.3] == "ug/m3"   # pm2.5
    assert by_unit[35.0] == "AQI"    # aqi coerced to float
    # Each record carries its measure's definition in sources + a per-metric id.
    src_pm25 = next(rec for rec in records if rec["value"] == 8.3)["sources"]
    assert f"{r.dedup_key()}:pm2_5" in src_pm25
    assert "com.fulcradynamics.annotation.d-pm25" in src_pm25


def test_build_records_skips_missing_values_and_defs():
    # pm10/pressure absent on the reading; humidity has no resolved def.
    r = _reading(pm10=None, pressure=None)
    def_ids = {k: v for k, v in _DEF_IDS.items() if k != "humidity"}
    records = build_records(r, def_ids)
    keys = {s.split(":")[-1] for rec in records for s in rec["sources"]
            if s.startswith("purpleair:")}
    assert keys == {"pm2_5", "aqi", "temperature"}


def test_build_records_empty_when_nothing_present():
    r = _reading(pm2_5=None, pm10=None, aqi=None,
                 temperature_f=None, humidity=None, pressure=None)
    assert build_records(r, _DEF_IDS) == []


class _Client(BaseFulcraClient):
    """BaseFulcraClient wired to a MockTransport, with a canned token."""

    def __init__(self, transport: httpx.MockTransport) -> None:
        super().__init__(base_url="https://api.test", transport=transport)

    def get_token(self) -> str:  # no CLI/env in tests
        return "test-token"


def test_post_records_hits_typed_endpoint_with_jsonl():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["ctype"] = request.headers.get("content-type")
        seen["body"] = request.content.decode()
        return httpx.Response(201, json={"upload_id": "u1"})

    client = _Client(httpx.MockTransport(handler))
    post_records(client, build_records(_reading(), _DEF_IDS))

    assert seen["url"].endswith("/ingest/v1/record/NumericAnnotation")
    assert seen["auth"] == "Bearer test-token"
    assert "x-jsonl" in seen["ctype"]
    lines = [json.loads(ln) for ln in seen["body"].splitlines()]
    assert len(lines) == 6


def test_post_records_noop_on_empty(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not POST an empty batch")

    client = _Client(httpx.MockTransport(handler))
    post_records(client, [])  # no exception, no request
