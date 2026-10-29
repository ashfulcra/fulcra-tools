"""V1 acceptance: partial uploads and indexing delays must not lose samples."""
from copy import deepcopy
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from fulcra_purpleair import collect_plugin
from fulcra_purpleair.definitions import METRICS
from fulcra_purpleair.models import Reading
from datetime import datetime, timezone
import logging
from fulcra_collect.plugin import RunContext


def reading(**overrides):
    values = dict(sensor_id="synthetic-sensor", observed_at=datetime(2026, 7, 22, tzinfo=timezone.utc),
                  pm2_5=8.3, pm10=9.1, aqi=35, temperature_f=72, humidity=45, pressure=1013.2)
    return Reading(**{**values, **overrides})


class FakeSession:
    account_id = "synthetic-account"

    def __init__(self):
        self.rows = {}
        self.posts = []
        self.resolved = []
        self.allow_creation = {}
        self.fail_after = None
        self.visible = True
        self.reject_commit = False
        self.excluded = set()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def resolve_destination(self, spec, *, pinned_id=None, allow_create=False):
        assert spec.base_type == "Metric"
        self.resolved.append((spec, pinned_id))
        self.allow_creation[spec.key] = allow_create
        return SimpleNamespace(data_type=pinned_id or "Metric/" + str(uuid5(NAMESPACE_URL, spec.key)))

    def get_record(self, destination, identifier):
        return deepcopy(self.rows.get((destination, identifier)))

    def get_records(self, destination, identifiers):
        return {i: deepcopy(self.rows[destination, i]) for i in identifiers if (destination, i) in self.rows}

    def _policy(self):
        return SimpleNamespace(data_type="Event/aaaaaaaa-2222-4333-8444-555555555555",
            excluded_ids=lambda _destination, identifiers: self.excluded.intersection(identifiers))

    def excluded_ids(self, destination, identifiers):
        from fulcra_common.record_delivery import enforce_exclusions
        return enforce_exclusions(self, self._policy(), destination, identifiers)

    def delete_records(self, destination, identifiers, *, action_id):
        for identifier in identifiers:
            self.rows.pop((destination, identifier), None)

    def record_write_scope(self):
        return nullcontext()

    def deliver_records(self, destination, records, *, dispatch):
        from fulcra_common.record_delivery import reconcile_records
        return reconcile_records(self, self._policy(), destination, records, dispatch=dispatch)

    def ingest(self, destination, records, *, before_submit=None):
        if before_submit is not None:
            before_submit()
        assert destination.startswith("Metric/")
        self.posts.append((destination, deepcopy(records)))
        if self.fail_after is not None and len(self.posts) > self.fail_after:
            raise RuntimeError("uncertain upload")
        if self.visible:
            for record in records:
                self.rows[destination, record["id"]] = deepcopy(record)
        return {"upload_id": "synthetic-receipt"}

    def commit(self, callback):
        if self.reject_commit:
            raise RuntimeError("Collect configuration changed")
        return callback()


@pytest.fixture
def setup(monkeypatch):
    kv = {}
    claimed = set()
    ctx = RunContext(plugin_id="purpleair", config={"mode": "api", "sensor_index": "synthetic"},
                     credentials={"api_key": "synthetic"}, state=SimpleNamespace(),
                     log=logging.getLogger("synthetic"),
                     _emit=lambda event: kv.setdefault("_events", []).append(event),
                     _plugin_kv_get=lambda key, default: deepcopy(kv.get(key, default)),
                     _plugin_kv_set=lambda key, value: kv.__setitem__(key, deepcopy(value)),
                     _claim_dedup_keys=lambda keys: claimed.update(keys) or True)
    ctx._kv = kv
    ctx._claimed = claimed
    ctx.config_epoch = "synthetic-epoch"
    session = FakeSession()
    monkeypatch.setattr(collect_plugin, "CollectRecordSession", lambda _: session, raising=False)
    monkeypatch.setattr(collect_plugin, "fetch_api", lambda *_: reading())
    return ctx, session


def receipts(ctx):
    return [e for e in ctx._kv.get("_events", []) if e.get("type") == "annotation"]


def test_v1_six_metrics_and_repeat_readback_skip(setup):
    ctx, session = setup
    collect_plugin.run(ctx)
    assert len(session.posts) == 6
    assert len(session.rows) == 6
    assert ctx._kv["v1_last_observed"]["observed_at"] == reading().observed_at.isoformat()
    assert receipts(ctx)[0]["observed_at"] == reading().observed_at.isoformat()
    collect_plugin.run(ctx)
    assert len(session.posts) == 6
    assert len(receipts(ctx)) == 1  # unchanged source clock must not manufacture a yield
    assert ctx._claimed == set()   # legacy sample claims cannot suppress v1 migration


def test_same_sample_existing_shortcut_repairs_an_excluded_metric(setup):
    ctx, session = setup
    collect_plugin.run(ctx)
    destination, row = next(iter(session.rows.items()))
    session.excluded.add(row["id"])
    session.posts.clear()
    before = len(receipts(ctx))
    collect_plugin.run(ctx)
    assert destination not in session.rows
    assert not session.posts
    assert len(receipts(ctx)) == before


def test_pending_sample_excluded_after_uncertain_upload_never_reposts_or_claims_measures(setup):
    ctx, session = setup
    session.fail_after = 0
    with pytest.raises(RuntimeError, match="uncertain"):
        collect_plugin.run(ctx)
    pending = ctx._kv["v1_pending"]["items"][0]
    session.excluded = {record["id"] for record in pending["records"].values()}
    session.fail_after = None
    before = len(session.posts)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        collect_plugin.run(ctx)
    assert len(session.posts) == before
    assert not session.rows
    assert ctx._kv["v1_pending"]["items"]
    assert not receipts(ctx)


def test_v1_missing_measure_later_arrives_at_same_sample_time(setup, monkeypatch):
    ctx, session = setup
    monkeypatch.setattr(collect_plugin, "fetch_api", lambda *_: reading(pm10=None))
    collect_plugin.run(ctx)
    assert len(session.rows) == 5
    monkeypatch.setattr(collect_plugin, "fetch_api", lambda *_: reading())
    collect_plugin.run(ctx)
    assert len(session.rows) == len(session.posts) == 6


def test_v1_partial_upload_keeps_outbox_and_reconciles_before_new_sensor_fetch(setup, monkeypatch):
    ctx, session = setup
    session.fail_after = 2
    with pytest.raises(RuntimeError, match="uncertain"):
        collect_plugin.run(ctx)
    assert len(session.rows) == 2
    assert ctx._kv["v1_pending"]["items"]
    assert "v1_last_observed" not in ctx._kv
    assert not receipts(ctx)
    session.fail_after = None
    session.posts.clear()
    # The source can be offline on retry. Previously captured data must land first.
    def offline(*_):
        raise RuntimeError("source offline")
    monkeypatch.setattr(collect_plugin, "fetch_api", offline)
    with pytest.raises(RuntimeError, match="UNKNOWN"):
        collect_plugin.run(ctx)
    assert len(session.rows) == 2
    assert not session.posts
    # The uncertain source request later becomes exactly visible. Only then can
    # the remaining never-issued destinations submit before the next fetch.
    item = ctx._kv["v1_pending"]["items"][0]
    for destination, state in item["dispatch"].items():
        for row in state["issued_records"]:
            session.rows[destination, row["id"]] = deepcopy(row)
    with pytest.raises(RuntimeError, match="source offline"):
        collect_plugin.run(ctx)
    assert len(session.rows) == 6
    assert len(session.posts) == 3
    assert ctx._kv["v1_pending"]["items"] == []
    assert receipts(ctx)


def test_v1_receipt_without_visibility_keeps_progress_uncommitted(setup):
    ctx, session = setup
    session.visible = False
    with pytest.raises(RuntimeError, match="visible"):
        collect_plugin.run(ctx)
    assert ctx._kv["v1_pending"]["items"]
    assert "v1_last_observed" not in ctx._kv
    assert not receipts(ctx)


def test_v1_account_change_cannot_reuse_pending_records_or_destination_pins(setup):
    ctx, session = setup
    session.visible = False
    with pytest.raises(RuntimeError):
        collect_plugin.run(ctx)
    session.account_id = "another-synthetic-account"
    session.posts.clear()
    with pytest.raises(RuntimeError, match="pending.*account"):
        collect_plugin.run(ctx)
    assert not session.posts


def test_v1_epoch_change_holds_old_pending_source_for_reconciliation(setup):
    ctx, session = setup
    session.visible = False
    with pytest.raises(RuntimeError):
        collect_plugin.run(ctx)
    ctx.config_epoch = "new-source-selection"
    session.posts.clear()
    with pytest.raises(RuntimeError, match="pending.*configuration"):
        collect_plugin.run(ctx)
    assert not session.posts


def test_v1_manual_source_edit_is_fenced_even_if_epoch_was_not_rotated(setup):
    ctx, session = setup
    session.visible = False
    with pytest.raises(RuntimeError):
        collect_plugin.run(ctx)
    ctx.config["sensor_index"] = "another-synthetic-source"
    session.posts.clear()
    with pytest.raises(RuntimeError, match="pending.*configuration"):
        collect_plugin.run(ctx)
    assert not session.posts


def test_v1_legacy_definition_cache_is_preserved_and_never_used_as_v1_pin(setup):
    ctx, session = setup
    old = {m.key: "old-definition-" + m.key for m in METRICS}
    ctx._kv["definition_ids"] = old.copy()
    collect_plugin.run(ctx)
    assert ctx._kv["definition_ids"] == old
    assert all(pin is None for _, pin in session.resolved)


def test_v1_commit_cancelled_before_outbox_blocks_record_post(setup):
    ctx, session = setup
    session.reject_commit = True
    with pytest.raises(RuntimeError, match="configuration changed"):
        collect_plugin.run(ctx)
    assert not session.posts


def test_local_mode_fetches_each_ip_and_preserves_distinct_sensor_ids(setup, monkeypatch):
    ctx, session = setup
    ctx.config = {"mode": "local", "sensor_ips": "10.0.0.5, 10.0.0.6"}
    seen = []

    def local(ip):
        seen.append(ip)
        return reading(sensor_id=ip)

    monkeypatch.setattr(collect_plugin, "fetch_local", local)
    collect_plugin.run(ctx)
    assert seen == ["10.0.0.5", "10.0.0.6"]
    assert len(session.rows) == 12
    assert len(receipts(ctx)) == 2


def test_unknown_mode_fails_before_collect_session(setup):
    ctx, session = setup
    ctx.config["mode"] = "banana"
    with pytest.raises(RuntimeError, match="unknown mode"):
        collect_plugin.run(ctx)
    assert not session.posts


def test_old_account_destination_cache_is_not_pinned_for_new_account(setup):
    ctx, session = setup
    ctx._kv["v1_destinations"] = {"account_id": "previous-account", "ids": {m.key: "old" for m in METRICS}}
    collect_plugin.run(ctx)
    assert all(pin is None for _, pin in session.resolved)
    assert ctx._kv["v1_destinations"]["account_id"] == session.account_id


def test_changed_sample_content_updates_same_metric_id_without_duplicate(setup, monkeypatch):
    ctx, session = setup
    collect_plugin.run(ctx)
    ids = set(session.rows)
    monkeypatch.setattr(collect_plugin, "fetch_api", lambda *_: reading(pm2_5=10.5))
    collect_plugin.run(ctx)
    assert set(session.rows) == ids
    assert len(session.posts) == 7
    assert any(record["value"] == 10.5 for record in session.rows.values())


def test_explicit_metric_selection_is_owned_lookup_only(setup):
    ctx, session = setup
    chosen = "Metric/11111111-2222-4333-8444-555555555555"
    ctx.config["v1_destination_ids"] = {"pm2_5": chosen}
    collect_plugin.run(ctx)
    assert next(pin for spec, pin in session.resolved if spec.key == "collect.purpleair.pm2_5.v1") == chosen
    assert session.allow_creation["collect.purpleair.pm2_5.v1"] is False
    assert any(destination == chosen for destination, _ in session.posts)


def test_invalid_later_metric_pin_fails_before_any_destination_create(setup):
    ctx, session = setup
    ctx.config["v1_destination_ids"] = {"pressure": "legacy-definition"}
    with pytest.raises(ValueError, match="destination"):
        collect_plugin.run(ctx)
    assert not session.resolved
    assert not session.posts
