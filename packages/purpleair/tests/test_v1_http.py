"""Exercise the real Collect session, resolver and v1 HTTP serializer together."""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import logging
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from fulcra_collect import config, db
from fulcra_collect.plugin import RunContext
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from fulcra_purpleair import collect_plugin
from fulcra_purpleair.definitions import METRICS
from fulcra_purpleair.models import Reading

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string", "format": "uuid"},
    "sources": {"type": "array", "items": {"type": "string"}},
    "tags": {"type": "array", "items": {"type": "string", "format": "uuid"}},
    "start_time": {"type": "string", "format": "date-time"},
    "end_time": {"type": "string", "format": "date-time"},
    "value": {"type": "number"}, "unit": {"type": "string"},
}, "required": ["id", "start_time", "end_time", "value"]}


@pytest.mark.parametrize("mode", ["visible", "delayed", "lost"])
def test_real_v1_pipeline_creates_owned_metrics_and_verifies_before_progress(tmp_path, monkeypatch, mode):
    delayed = mode != "visible"
    lost = mode == "lost"
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "collect"))
    cfg = config.Config()
    cfg.enable("purpleair")
    cfg.update_plugin_settings("purpleair", {"mode": "api", "sensor_index": "synthetic"})
    config.save(cfg)
    cfg = config.load()
    payload = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": ACCOUNT}).encode()).decode().rstrip("=")
    token = "synthetic." + payload + ".signature"
    kv = {}
    events = []
    catalog = []
    rows = {}
    requests = []
    def put(key, value):
        db.set_plugin_kv(db.open(), "purpleair", key, value)
        kv[key] = deepcopy(value)
    ctx = RunContext(plugin_id="purpleair", config=cfg.plugin_settings["purpleair"], config_epoch=cfg.plugin_epochs["purpleair"],
                     credentials={"api_key": "synthetic-key"}, state=SimpleNamespace(), log=logging.getLogger("synthetic"),
                     _emit=events.append, _plugin_kv_get=lambda k, d: db.get_plugin_kv(db.open(), "purpleair", k, d),
                     _plugin_kv_set=put)
    monkeypatch.setattr(ctx, "collect_token", lambda: token)
    monkeypatch.setattr(collect_plugin, "fetch_api", lambda *_: Reading(sensor_id="synthetic", observed_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                       pm2_5=0, pm10=9.1, aqi=35, temperature_f=72, humidity=45, pressure=1013.2))

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + token
        path = request.url.path
        if path == "/input/v1/data_type":
            assert request.url.params["include_deprecated"] == "true"
            return httpx.Response(200, json=catalog)
        if path in {"/input/v1/data_type/Metric", "/input/v1/data_type/Event"}:
            body = json.loads(request.content)
            identifier = path.rsplit("/", 1)[1] + "/" + str(UUID(int=len(catalog) + 1))
            catalog.append({**body, "id": identifier, "api_version": "v1", "recordable": True,
                            "deprecated": False, "fulcra_userid": ACCOUNT})
            return httpx.Response(201, json={"id": identifier})
        if path.startswith("/data/v1/catalog/Metric/"):
            return httpx.Response(200, json=SCHEMA)
        if path.startswith("/data/v1/catalog/Event/"):
            return httpx.Response(200, json={**SCHEMA, "properties": {**SCHEMA["properties"], **EXCLUSION_SPEC.fields},
                "required": ["id", "start_time", "end_time", *EXCLUSION_SPEC.required_fields]})
        if path == "/data/v1/records":
            assert request.url.params["fulcra_userid"] == ACCOUNT
            query = request.url.params["q"]
            actual = rows.get(query)
            return httpx.Response(200, text=json.dumps(actual) + "\n" if actual else "")
        assert path.startswith("/ingest/v1/record/Metric/")
        assert request.method == "POST"
        assert request.url.params["api_version"] == "v1"
        destination = path.removeprefix("/ingest/v1/record/")
        for line in request.content.splitlines():
            record = json.loads(line)
            assert not {"metadata", "data", "recorded_at"} & record.keys()
            query = '{__name__=' + json.dumps(destination) + ',id=' + json.dumps(record["id"]) + '}'
            if not delayed:
                rows[query] = {**record, "start_time": record["start_time"].replace("Z", "+00:00")}
        if lost:
            raise httpx.ReadError("uncertain source reply")
        return httpx.Response(201, json={"upload_id": "synthetic-receipt"})

    monkeypatch.setattr(collect_plugin, "CollectRecordSession",
                        lambda ctx: CollectRecordSession(ctx, transport=httpx.MockTransport(handler)))
    if delayed:
        with pytest.raises((RuntimeError, httpx.ReadError)):
            collect_plugin.run(ctx)
        assert not [e for e in events if e["type"] == "annotation"]
        assert "v1_last_observed" not in kv
        assert kv["v1_pending"]["items"]
        initial = [r for r in requests if r.url.path.startswith("/ingest/")]
        assert len(initial) == 1
        db.close_all()  # next invocation reopens the actual persistent database
        lost = False
        def offline(*_):
            raise RuntimeError("source offline")
        monkeypatch.setattr(collect_plugin, "fetch_api", offline)
        with pytest.raises(RuntimeError, match="UNKNOWN"):
            collect_plugin.run(ctx)
        assert len([r for r in requests if r.url.path.startswith("/ingest/")]) == 1
        # Exact later source visibility authorizes completion, then new fetch.
        item = kv["v1_pending"]["items"][0]
        for destination, state in item["dispatch"].items():
            for record in state["issued_records"]:
                query = '{__name__=' + json.dumps(destination) + ',id=' + json.dumps(record["id"]) + '}'
                rows[query] = deepcopy(record)
        delayed = False
        with pytest.raises(RuntimeError, match="source offline"):
            collect_plugin.run(ctx)
        assert len([r for r in requests if r.url.path.startswith("/ingest/")]) == 6
        assert not kv["v1_pending"]["items"]
        assert "v1_last_observed" in kv
        assert len([e for e in events if e["type"] == "annotation"]) == 1
    else:
        collect_plugin.run(ctx)
        assert len(rows) == 6
        assert len([e for e in events if e["type"] == "annotation"]) == 1
        assert not kv["v1_pending"]["items"]
        before = len([r for r in requests if r.method == "POST"])
        collect_plugin.run(ctx)
        assert len([r for r in requests if r.method == "POST"]) == before
    metric_types = [row for row in catalog if row["record_spec"]["type"] == "metric"]
    assert len(metric_types) == len(METRICS)
    assert {row["record_spec"]["unit"] for row in metric_types} == {m.unit for m in METRICS}
    assert len(catalog) == len(METRICS) + 1
    assert [row["name"] for row in catalog if row not in metric_types] == [EXCLUSION_SPEC.name]
