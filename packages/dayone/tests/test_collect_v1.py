"""Exercise the real worker/session/outbox with synthetic Day One rows only."""
import base64
import io
import json
import re
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from fulcra_collect import config, credentials, db, state, worker
from fulcra_collect.plugin import RunContext
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC
from fulcra_common.v1 import build_event_record
from fulcra_dayone import collect_plugin
from fulcra_dayone.entry import DayOneEntry
from fulcra_dayone.v1 import JOURNAL_SPEC, build_journal_record

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
DESTINATION = "Event/11111111-2222-4333-8444-555555555555"
CONTROL = "Event/aaaaaaaa-2222-4333-8444-555555555555"
TAG = "aaaaaaaa-bbbb-4ccc-8ddd-123456789abc"
PENDING = "v1_dayone_pending"


def token(account=ACCOUNT):
    claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": account,
        "exp": 4102444800}).encode()).decode().rstrip("=")
    return "synthetic." + claims + ".signature"


def entry(**changes):
    return replace(DayOneEntry(uuid="ABC123", creation_date=datetime(2024, 2, 3, tzinfo=timezone.utc),
        text="Synthetic entry", tags=("synthetic",), starred=False, journal="Synthetic journal",
        location=None, photo_count=0, word_count=2), **changes)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
        lambda *_: pytest.fail("Day One test attempted real network access"))
    monkeypatch.setattr(worker, "_make_fulcra_definition_client",
        lambda: pytest.fail("Day One v1 writer attempted legacy definition resolution"))


def test_auth_precedes_source_access_and_plugin_declares_a_v1_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    monkeypatch.setattr(RunContext, "collect_token", lambda _: None)
    monkeypatch.setattr(collect_plugin, "read", lambda *a, **kw: pytest.fail("source accessed before sign-in"))
    cfg = config.load()
    cfg.enable("dayone")
    cfg.update_plugin_settings("dayone", {"local_db": "live_app"})
    config.save(cfg)
    assert worker.run_plugin(collect_plugin.PLUGIN, out=io.StringIO()) == "error"
    assert collect_plugin.PLUGIN.record_data_types({}) == {"journal": JOURNAL_SPEC}
    step = next(s for s in collect_plugin.PLUGIN.setup_steps if s.kind == "definition_picker")
    assert step.record_slot == "journal"


@pytest.mark.parametrize("mode", ["visible", "delayed", "preview", "invalid", "edited", "excluded",
    "post_excluded", "post_account_change", "post_settings_change", "legacy_unmapped"])
def test_real_worker_observes_v1_delivery_and_import_once(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    cfg = config.load()
    cfg.enable("dayone")
    settings = {"local_db": True if mode == "visible" else "live_app", "preview_only": mode == "preview",
        "v1_destination_ids": {"journal": DESTINATION}}
    if mode == "legacy_unmapped":
        settings.pop("v1_destination_ids")
    cfg.update_plugin_settings("dayone", settings)
    config.save(cfg)
    # Completed destination setup supplies the original policy binding. A
    # preview may verify it, never discover/adopt it as a side effect.
    binding_path = tmp_path / "v1-type-creations.sqlite"
    CreationJournal(binding_path).resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    binding_before = binding_path.read_bytes()
    old = state.load("dayone")
    old.definition_id = "legacy-journal-selection"
    old.watermark = "2020-01-01T00:00:00Z"
    state.save(old)
    auth = [token()]
    monkeypatch.setattr(credentials, "get_user_secret_fresh", lambda _: auth[0])
    monkeypatch.setattr(credentials, "get_secret", lambda *_: None)
    incoming = entry(text="Edited source entry") if mode == "edited" else entry()
    selected = [incoming, entry(uuid="BAD", word_count=True)] if mode == "invalid" else [incoming]
    reads = []
    monkeypatch.setattr(collect_plugin, "read", lambda *a, **kw: reads.append(True) or selected)
    original = build_journal_record(entry(), DESTINATION, tags=[TAG])
    rows, posts, paths, deletes = {}, [], [], []
    if mode == "edited":
        rows[DESTINATION, original["id"]] = {**original, "note": "Cloud version retained", "tags": []}
    visible = mode != "delayed"
    def exclude():
        row = build_event_record(data_type=CONTROL,
            source_id=f"collect:v1:excluded:{DESTINATION}:{original['id']}",
            start_time=datetime(2024, 2, 3, tzinfo=timezone.utc),
            fields={"schema_version": 1, "target_data_type": DESTINATION,
                "target_record_id": original["id"], "action_id": OTHER_ACCOUNT, "reason": "undo", "evidence": []})
        rows[CONTROL, row["id"]] = row
    if mode == "excluded":
        exclude()
        rows[DESTINATION, original["id"]] = original  # An old writer revived an excluded row.
    def handler(request):
        path = request.url.path
        paths.append((request.method, path))
        assert request.headers["Authorization"] == "Bearer " + auth[0]
        if path == "/input/v1/data_type":
            return httpx.Response(200, json=[{"id": full, "fulcra_userid": ACCOUNT, "name": name,
                "api_version": "v1", "recordable": True, "deprecated": False, "record_spec": {"type": "event"}}
                for full, name in ((DESTINATION, "Journal"), (CONTROL, EXCLUSION_SPEC.name))])
        if path.endswith("/schema"):
            if "/DeletedRecord/" in path:
                return httpx.Response(200, json={"properties": {"id": {}, "record_id": {}, "data_type": {}}})
            spec = EXCLUSION_SPEC if CONTROL in path else JOURNAL_SPEC
            return httpx.Response(200, json={"type": "object", "properties": {
                **{k: {} for k in ("id", "sources", "tags", "start_time", "end_time")}, **spec.fields},
                "required": list(spec.required_fields)})
        if path.startswith(("/user/v1alpha1/tag/name/", "/user/v1alpha1/tag/id/")):
            return httpx.Response(200, json={"id": TAG, "name": "synthetic", "fulcra_userid": ACCOUNT})
        if path == "/data/v1/records":
            assert request.url.params["fulcra_userid"] == ACCOUNT
            query = request.url.params["q"]
            full = re.search('__name__="([^"]+)"', query).group(1)
            ids = re.findall('id="([^"]+)"', query)
            return httpx.Response(200, text="\n".join(json.dumps(rows[full, i]) for i in ids
                if (full, i) in rows and (full != DESTINATION or visible)))
        if path == "/ingest/v1/record/" + DESTINATION:
            assert request.url.params["api_version"] == "v1"
            batch = [json.loads(line) for line in request.content.splitlines()]
            assert len(batch) == 1 and batch[0] == original
            posts.extend(batch)
            rows.update({(DESTINATION, row["id"]): row for row in batch})
            if mode == "post_excluded":
                exclude()
            if mode == "post_account_change":
                auth[0] = token(OTHER_ACCOUNT)
            if mode == "post_settings_change":
                changed = config.load()
                changed.plugin_settings["dayone"]["path"] = "changed-source"
                # Simulate an external/raw edit bypassing the normal exclusive
                # configuration lease, as the captured-settings fence must do.
                monkeypatch.setattr(config, "load", lambda: changed)
            return httpx.Response(201, json={"upload_id": "synthetic"})
        if path == "/ingest/v1/record/DeletedRecord":
            for line in request.content.splitlines():
                tombstone = json.loads(line)
                deletes.append(tombstone)
                rows.pop((DESTINATION, tombstone["record_id"]), None)
            return httpx.Response(201, json={"upload_id": "synthetic-delete"})
        raise AssertionError("unexpected synthetic API path: " + path)
    monkeypatch.setattr(collect_plugin, "CollectRecordSession", lambda ctx: CollectRecordSession(
        ctx, transport=httpx.MockTransport(handler)))
    output = io.StringIO()
    failed = mode in {"delayed", "invalid", "legacy_unmapped", "post_account_change", "post_settings_change"}
    assert worker.run_plugin(collect_plugin.PLUGIN, out=output) == ("error" if failed else "done")
    emitted = [json.loads(line) for line in output.getvalue().splitlines()]
    receipts = [item for item in emitted if item.get("type") == "annotation"]
    expected_posts = int(mode in {"visible", "delayed", "post_excluded", "post_account_change", "post_settings_change"})
    assert len(posts) == expected_posts
    pending = db.get_record_outbox(db.open(), "dayone", PENDING)
    if mode in {"delayed", "post_account_change", "post_settings_change"}:
        assert pending["groups"] and not receipts
    elif mode in {"invalid", "legacy_unmapped", "preview"}:
        assert pending is None and not receipts
    else:
        assert not pending or not pending["groups"]
    if mode in {"invalid", "legacy_unmapped"}:
        assert not paths  # Entire-source preflight and legacy mapping precede metadata calls.
    if mode == "preview":
        assert all(method == "GET" for method, _ in paths)
        assert binding_path.read_bytes() == binding_before
    if mode == "edited":
        assert rows[DESTINATION, original["id"]]["note"] == "Cloud version retained"
        assert not receipts
        assert not any("/tag/" in path for _, path in paths)
    if mode in {"excluded", "post_excluded"}:
        assert len(deletes) == 1 and (DESTINATION, original["id"]) not in rows
        imported = next(item for item in emitted if item.get("stage") == "imported")
        assert imported["posted"] == imported["verified"] == 0 and imported["excluded"] == 1
    assert state.load("dayone").definition_id == "legacy-journal-selection"
    assert state.load("dayone").watermark == "2020-01-01T00:00:00Z"
    assert not any("annotation" in path or path.endswith("/batch") for _, path in paths)
    if mode in {"post_account_change", "post_settings_change"}:
        paths.clear()
        monkeypatch.setattr(collect_plugin, "read", lambda *a, **kw: pytest.fail("old pending context must block reads"))
        assert worker.run_plugin(collect_plugin.PLUGIN, out=io.StringIO()) == "error"
        assert not paths
    if mode == "delayed":
        # An issued source batch holds until exact readback; source access stays
        # blocked. Once visible, recovery must not issue another POST.
        monkeypatch.setattr(collect_plugin, "read", lambda *a, **kw: pytest.fail("read before pending drain"))
        assert worker.run_plugin(collect_plugin.PLUGIN, out=io.StringIO()) == "error"
        assert len(posts) == 1
        visible = True
        monkeypatch.setattr(collect_plugin, "read", lambda *a, **kw: [])
        assert worker.run_plugin(collect_plugin.PLUGIN, out=io.StringIO()) == "done"
        assert len(posts) == 1
        assert not db.get_record_outbox(db.open(), "dayone", PENDING)["groups"]


def synthetic_writer():
    import logging
    from contextlib import nullcontext
    from copy import deepcopy
    from types import SimpleNamespace
    from fulcra_common.record_delivery import RecordDelivery
    from fulcra_dayone.collect_v1 import JournalWriter
    storage, output, calls = {}, [], []
    ctx = RunContext(plugin_id="dayone", config={"v1_destination_ids": {"journal": DESTINATION}},
        credentials={}, state=state.PluginState("dayone"), config_epoch="synthetic-epoch",
        log=logging.getLogger("synthetic"), _emit=output.append,
        _plugin_kv_get=lambda key, default: storage.get(key, default),
        _plugin_kv_set=lambda key, value: storage.__setitem__(key, deepcopy(value)),
        _record_outbox_get=lambda key, default: storage.get(key, default),
        _record_outbox_set=lambda key, value: storage.__setitem__(key, deepcopy(value)))
    class Session:
        account_id = ACCOUNT
        def guard(self, **_):
            pass
        def commit(self, callback):
            return callback()
        def record_write_scope(self):
            return nullcontext()
        def resolve_destination(self, *a, **kw):
            calls.append("destination")
            return SimpleNamespace(data_type=DESTINATION)
        def resolve_tag(self, name):
            calls.append("tag")
            return TAG
        def excluded_ids(self, *a, **kw):
            return set()
        def get_records(self, *a, **kw):
            return {}
        def deliver_records(self, full, rows, *, dispatch):
            assert storage[PENDING]["groups"][0]["records"] == rows
            calls.append(deepcopy(rows))
            return RecordDelivery(tuple(row["id"] for row in rows), ())
    return JournalWriter(ctx, Session()), calls, output


def test_subsecond_order_and_receipt_use_instants_not_timestamp_text():
    writer, calls, output = synthetic_writer()
    early = entry(uuid="early", creation_date=datetime(2024, 2, 3, tzinfo=timezone.utc))
    late = replace(early, uuid="late", creation_date=early.creation_date.replace(microsecond=500000))
    result = writer.run_import([late, early])
    posted = next(call for call in calls if isinstance(call, list))
    assert [row["external_ids"]["dayone_uuid"] for row in posted] == ["early", "late"]
    assert next(item for item in output if item["type"] == "annotation")["observed_at"] == "2024-02-03T00:00:00.500000Z"
    assert result.posted == result.verified == 2


@pytest.mark.parametrize("conflicting", [False, True])
def test_whole_source_preflight_rejects_invalid_or_conflicting_later_entries(conflicting):
    writer, calls, output = synthetic_writer()
    rows = [entry(uuid="first"), entry(uuid="second")]
    rows.append(replace(rows[0], text="conflicting") if conflicting else entry(uuid="bad", photo_count=True))
    with pytest.raises(ValueError, match="conflicting|schema"):
        writer.run_import(rows, chunk_size=1)
    assert not calls and not output


def test_identical_source_duplicates_are_coalesced_before_staging():
    writer, calls, _ = synthetic_writer()
    result = writer.run_import([entry(), entry()])
    assert result.total == result.posted == result.verified == 1
    assert len(next(call for call in calls if isinstance(call, list))) == 1
