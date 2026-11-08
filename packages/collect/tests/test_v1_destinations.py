"""Wizard destination setup must use owned v1 types and preserve source consent."""
import base64
import json
import httpx
import pytest
from fulcra_collect import config, db, state
from fulcra_collect.plugin import RunContext
from fulcra_collect.v1_session import CollectRecordSession


from fastapi.testclient import TestClient

from fulcra_collect.config import Config
from fulcra_collect.daemon import Daemon
from fulcra_collect.plugin import Plugin, SetupStep
from fulcra_collect.registry import RegistryResult
from fulcra_collect.web import build_app, _ensure_token
from fulcra_common.data_types import DataTypeSpec
from fulcra_common.record_exclusions import EXCLUSION_SPEC


def specs(settings):
    return {"synthetic": DataTypeSpec(key="collect.synthetic.v1", base_type="Event", name="Synthetic events",
        description="Synthetic events for setup tests.", fields={"note": {"type": "string"}})}


def client(plugin):
    daemon = Daemon(registry=RegistryResult(plugins={plugin.id: plugin}), config=Config())
    app = build_app(daemon)
    result = TestClient(app)
    result.headers["Authorization"] = "Bearer " + _ensure_token()
    return result


def test_contract_identifies_v1_record_destination_setup(collect_home):
    plugin = Plugin(id="synthetic", name="Synthetic", kind="manual", collect_mode="historical", run=lambda _: None,
                    record_data_types=specs)
    response = client(plugin).get("/api/plugin/synthetic/contract")
    assert response.status_code == 200
    assert response.json()["record_api_version"] == "v1"


def test_legacy_and_file_only_contracts_do_not_claim_v1_recording(collect_home):
    plugin = Plugin(id="legacy", name="Legacy", kind="manual", collect_mode="historical", run=lambda _: None)
    response = client(plugin).get("/api/plugin/legacy/contract")
    assert response.status_code == 200
    assert response.json()["record_api_version"] is None


def test_multi_destination_step_declares_its_stable_slot(collect_home):
    plugin = Plugin(id="synthetic", name="Synthetic", kind="manual", collect_mode="historical", run=lambda _: None,
                    record_data_types=specs, setup_steps=(SetupStep(kind="definition_picker", title="Destination",
                    record_slot="synthetic"),))
    response = client(plugin).get("/api/plugin/synthetic/contract")
    assert response.json()["setup_steps"][0]["record_slot"] == "synthetic"

# Synthetic account/catalog; no real token or provider data leaves the test.

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
DESTINATION = "Event/11111111-2222-4333-8444-555555555555"
CONTROL = "Event/aaaaaaaa-2222-4333-8444-555555555555"
SCHEMA = {"type": "object", "properties": {
    "id": {"type": "string", "format": "uuid"}, "sources": {"type": "array"}, "tags": {"type": "array"},
    "start_time": {"type": "string", "format": "date-time"}, "end_time": {"type": "string", "format": "date-time"},
    "note": {"type": "string"},
}}


@pytest.fixture
def setup(collect_home, monkeypatch):
    plugin = Plugin(id="synthetic", name="Synthetic", kind="manual", collect_mode="historical", run=lambda _: None,
                    record_data_types=specs)
    cfg = config.load()
    cfg.update_plugin_settings("synthetic", {"source": "original"})
    config.save(cfg)
    claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": ACCOUNT}).encode()).decode().rstrip("=")
    monkeypatch.setattr(RunContext, "collect_token", lambda _: "synthetic." + claims + ".signature")
    requests = []
    catalog = [{"id": DESTINATION, "name": "Synthetic events", "fulcra_userid": ACCOUNT,
                "api_version": "v1", "recordable": True, "deprecated": False, "record_spec": {"type": "event"}},
               {"id": CONTROL, "name": EXCLUSION_SPEC.name, "fulcra_userid": ACCOUNT,
                "api_version": "v1", "recordable": True, "deprecated": False, "record_spec": {"type": "event"}}]
    during_schema = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/input/v1/data_type":
            return httpx.Response(200, json=catalog)
        if request.url.path.endswith("/schema"):
            for callback in during_schema:
                callback()
            if CONTROL in request.url.path:
                return httpx.Response(200, json={"type": "object", "properties": {
                    **{k: {} for k in ("id", "sources", "tags", "start_time", "end_time")}, **EXCLUSION_SPEC.fields},
                    "required": list(EXCLUSION_SPEC.required_fields)})
            return httpx.Response(200, json=SCHEMA)
        if request.url.path == "/data/v1/records":
            assert request.url.params["q"] == 'last_over_time({__name__="' + DESTINATION + '"}[30d])'
            assert request.url.params["fulcra_userid"] == ACCOUNT
            return httpx.Response(200, text=json.dumps({"id": "aaaaaaaa-bbbb-4ccc-8ddd-123456789abc",
                "note": "Synthetic existing event", "start_time": "2026-10-23T12:00:00Z",
                "end_time": "2026-10-23T12:00:00Z", "external_ids": {"not_needed_by_preview": "private"}}))
        if request.method == "POST":
            body = json.loads(request.content)
            identifier = CONTROL if body["name"] == EXCLUSION_SPEC.name else DESTINATION
            catalog.append({**body, "id": identifier, "fulcra_userid": ACCOUNT, "recordable": True,
                            "deprecated": False, "api_version": "v1"})
            return httpx.Response(201, json={"id": identifier})
        if request.url.path.startswith("/user/v1alpha1/annotation/"):
            return httpx.Response(200, json={"id": "synthetic-legacy", "name": "Synthetic old track"})
        raise AssertionError("unexpected API path")

    # The new route must use the actual guarded session, with its transport
    # replaced, rather than the old refresh/CLI-fallback web client.
    import fulcra_collect.routes.record_destinations as routes
    monkeypatch.setattr(routes, "CollectRecordSession", lambda ctx, **kw: CollectRecordSession(
        ctx, transport=httpx.MockTransport(handler), **kw))
    return client(plugin), catalog, requests, during_schema


def test_v1_picker_lists_only_owned_recordable_custom_types(setup):
    app, catalog, requests, _ = setup
    # This caller-owned authority returns custom types, including retired ones.
    catalog += [{**catalog[0], "id": "Event/11111111-2222-4333-8444-777777777777", "deprecated": True}]
    response = app.get("/api/plugin/synthetic/data-types")
    assert response.status_code == 200
    body = response.json()
    assert body["slot"] == "synthetic"
    assert [row["id"] for row in body["destinations"]] == [DESTINATION]
    assert all(request.method == "GET" for request in requests)
    assert requests[0].url.path == "/input/v1/data_type"
    assert requests[0].url.params["include_deprecated"] == "true"


def test_v1_picker_refuses_contradictory_provider_ownership_instead_of_clean_empty(setup):
    app, catalog, _, _ = setup
    catalog[0]["fulcra_userid"] = OTHER
    response = app.get("/api/plugin/synthetic/data-types")
    assert response.status_code == 400
    assert "destinations" not in response.json()


def test_v1_preview_proves_owner_and_schema_then_returns_only_display_fields(setup):
    app, _, requests, _ = setup
    response = app.get("/api/plugin/synthetic/data-type/recent", params={"slot": "synthetic", "data_type_id": DESTINATION})
    assert response.status_code == 200
    body = response.json()
    assert body["window_days"] == 30
    assert body["entries"][0]["note"] == "Synthetic existing event"
    assert "external_ids" not in body["entries"][0]
    assert all(request.method == "GET" for request in requests)


def test_bind_proves_owned_schema_then_persists_full_id_without_enabling_source(setup):
    app, _, requests, _ = setup
    before = config.load().plugin_epochs["synthetic"]
    state.save(state.PluginState(plugin_id="synthetic", definition_id="synthetic-old-selected"))
    response = app.post("/api/plugin/synthetic/data-type", json={"slot": "synthetic", "data_type_id": DESTINATION})
    assert response.status_code == 200
    saved = config.load()
    assert saved.plugin_settings["synthetic"]["v1_destination_ids"] == {"synthetic": DESTINATION}
    assert saved.plugin_settings["synthetic"]["source"] == "original"
    assert saved.plugin_epochs["synthetic"] != before
    assert "synthetic" not in saved.enabled
    assert state.load("synthetic").definition_id == "synthetic-old-selected"
    assert all(request.method == "GET" for request in requests)


def test_foreign_type_cannot_be_bound_and_does_not_mutate_settings(setup):
    app, catalog, _, _ = setup
    catalog[0]["fulcra_userid"] = OTHER
    before = config.load().plugin_settings
    response = app.post("/api/plugin/synthetic/data-type", json={"slot": "synthetic", "data_type_id": DESTINATION})
    assert response.status_code == 400
    assert config.load().plugin_settings == before


def test_changed_source_while_schema_is_read_cancels_binding(setup):
    app, _, _, during_schema = setup
    def change_source():
        cfg = config.load()
        cfg.update_plugin_settings("synthetic", {"source": "changed"})
        config.save(cfg)
    during_schema.append(change_source)
    response = app.post("/api/plugin/synthetic/data-type", json={"slot": "synthetic", "data_type_id": DESTINATION})
    assert response.status_code == 409
    assert config.load().plugin_settings["synthetic"] == {"source": "changed"}


def test_pending_batch_blocks_destination_reassignment(setup):
    app, _, requests, _ = setup
    db.set_record_outbox(db.open(), "synthetic", "v1_pending_records", {"groups": [{"records": []}]})
    response = app.post("/api/plugin/synthetic/data-type", json={"slot": "synthetic", "data_type_id": DESTINATION})
    assert response.status_code == 409
    assert "pending" in response.json()["detail"].lower()
    assert not requests


@pytest.mark.parametrize("new_name", [None, "Synthetic events"])
def test_create_uses_declared_v1_spec_and_verifies_before_binding(setup, new_name):
    app, catalog, requests, _ = setup
    catalog.clear()
    body = {"slot": "synthetic", "use_or_create": True}
    if new_name is not None:
        body["new_name"] = new_name
    response = app.post("/api/plugin/synthetic/data-type", json=body)
    assert response.status_code == 200
    posts = [request for request in requests if request.method == "POST"]
    assert len(posts) == 2
    assert posts[0].url.path == "/input/v1/data_type/Event"
    assert json.loads(json.loads(posts[0].content)["record_spec"]["schema"])["properties"] == {"note": {"type": "string"}}
    assert json.loads(posts[0].content)["metadata"]["fulcra_tools"]["key"] == "collect.synthetic.v1"
    assert json.loads(posts[1].content)["metadata"]["fulcra_tools"]["key"] == EXCLUSION_SPEC.key
    assert config.load().plugin_settings["synthetic"]["v1_destination_ids"] == {"synthetic": DESTINATION}


@pytest.mark.parametrize("body", [{"definition_id": "legacy"}, {"force_new": True}])
def test_legacy_bind_endpoint_cannot_reconfigure_a_v1_record_writer(setup, body):
    app, _, _, _ = setup
    response = app.post("/api/plugin/synthetic/definition", json=body)
    assert response.status_code == 409
    assert "v1" in response.json()["detail"]


def test_saved_control_binding_keeps_picker_usable_beyond_discovery_cap(setup):
    from pathlib import Path
    from uuid import UUID
    from fulcra_common.data_types import CreationJournal
    app, catalog, requests, _ = setup
    CreationJournal(Path(config.config_dir()) / "v1-type-creations.sqlite").resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    catalog[1]["name"] = "Renamed control"
    catalog.extend({**catalog[0], "id": "Event/" + str(UUID(int=i + 1))} for i in range(64))
    response = app.get("/api/plugin/synthetic/data-types")
    assert response.status_code == 200
    assert CONTROL not in {row["id"] for row in response.json()["destinations"]}
    assert len(requests) == 1
