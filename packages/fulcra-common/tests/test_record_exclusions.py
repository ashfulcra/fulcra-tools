"""A native tombstone cannot itself keep a later source replay excluded."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

CONTROL = "Event/aaaaaaaa-2222-4333-8444-555555555555"
EVENT = "Event/11111111-2222-4333-8444-555555555555"
METRIC = "Metric/11111111-2222-4333-8444-555555555555"
TARGET = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACTION = "bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_ACTION = "cccccccc-bbbb-4ccc-8ddd-eeeeeeeeeeee"
ACCOUNT = "11111111-2222-4333-8444-555555555555"
OTHER_ACCOUNT = "22222222-3333-4444-8555-666666666666"
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


class Store:
    def __init__(self):
        self.rows = {}
        self.posts = []
        self.fail_read = False
        self.visible = True

    def get_records(self, destination, identities):
        assert destination == CONTROL
        if self.fail_read:
            raise RuntimeError("synthetic unreadable policy")
        return {i: deepcopy(self.rows[i]) for i in identities if i in self.rows}

    def ingest(self, destination, rows):
        assert destination == CONTROL
        self.posts.append(deepcopy(rows))
        if self.visible:
            self.rows.update({row["id"]: deepcopy(row) for row in rows})
        return {"upload_id": "synthetic-not-visibility"}


def ensure(control, **kwargs):
    args = dict(data_type=EVENT, record_id=TARGET, action_id=ACTION,
                requested_at=NOW, reason="undo", evidence=["synthetic-marker"])
    args.update(kwargs)
    return control.exclude(**args)


def test_exclusion_is_stable_target_scoped_and_queryable_independently_of_tombstone():
    from fulcra_common.record_exclusions import RecordExclusions, EXCLUSION_SPEC
    from fulcra_common.v1 import record_id_for
    store = Store()
    control = RecordExclusions(store, CONTROL)
    assert EXCLUSION_SPEC.base_type == "Event"
    assert control.excluded_ids(EVENT, [TARGET]) == set()
    row = ensure(control)
    assert row["id"] == record_id_for(CONTROL, f"collect:v1:excluded:{EVENT}:{TARGET}")
    assert row["target_data_type"] == EVENT and row["target_record_id"] == TARGET
    assert row["action_id"] == ACTION and row["reason"] == "undo"
    assert row["evidence"] == ["synthetic-marker"]
    assert row["start_time"] == row["end_time"] == "2026-01-02T00:00:00Z"
    assert control.excluded_ids(EVENT, [TARGET, TARGET]) == {TARGET}
    assert control.excluded_ids(METRIC, [TARGET]) == set()
    assert RecordExclusions(store, CONTROL).excluded_ids(EVENT, [TARGET]) == {TARGET}


def test_existing_exclusion_is_never_replaced_by_another_action_or_changed_time():
    from fulcra_common.record_exclusions import RecordExclusions
    store = Store()
    control = RecordExclusions(store, CONTROL)
    first = ensure(control)
    again = ensure(control, action_id=OTHER_ACTION, reason="migration",
                   requested_at=datetime(2026, 2, 2, tzinfo=timezone.utc))
    assert again == first
    assert len(store.posts) == 1


def test_receipt_without_policy_visibility_is_pending_and_retry_reconciles_first():
    from fulcra_common.record_exclusions import RecordExclusions
    store = Store()
    store.visible = False
    control = RecordExclusions(store, CONTROL)
    with pytest.raises(RuntimeError, match="not yet visible"):
        ensure(control)
    assert len(store.posts) == 1
    # A delayed response may land before the next process starts.
    row = store.posts[0][0]
    store.rows[row["id"]] = row
    assert ensure(RecordExclusions(store, CONTROL)) == row
    assert len(store.posts) == 1


def test_unknown_policy_never_means_no_exclusion_and_never_authorizes_post():
    from fulcra_common.record_exclusions import RecordExclusions
    store = Store()
    store.fail_read = True
    control = RecordExclusions(store, CONTROL)
    with pytest.raises(RuntimeError, match="unreadable"):
        control.excluded_ids(EVENT, [TARGET])
    with pytest.raises(RuntimeError, match="unreadable"):
        ensure(control)
    assert not store.posts


@pytest.mark.parametrize("name", ["fulcra_userid", "userid"])
@pytest.mark.parametrize("owner", [OTHER_ACCOUNT, None, False])
def test_policy_row_owner_contradiction_holds_instead_of_becoming_an_exclusion(name, owner):
    from fulcra_common.record_exclusions import RecordExclusions
    store = Store()
    store.account_id = ACCOUNT
    control = RecordExclusions(store, CONTROL)
    row = ensure(control)
    store.rows[row["id"]][name] = owner
    with pytest.raises(ValueError, match="exclusion"):
        control.excluded_ids(EVENT, [TARGET])
    with pytest.raises(ValueError, match="exclusion"):
        ensure(control)
    assert len(store.posts) == 1


@pytest.mark.parametrize("name", ["fulcra_userid", "userid"])
def test_matching_policy_owner_is_accepted_but_an_unknown_store_scope_cannot_attest_it(name):
    from fulcra_common.record_exclusions import RecordExclusions
    store = Store()
    store.account_id = ACCOUNT
    control = RecordExclusions(store, CONTROL)
    row = ensure(control)
    store.rows[row["id"]][name] = ACCOUNT
    assert control.excluded_ids(EVENT, [TARGET]) == {TARGET}
    del store.account_id
    with pytest.raises(ValueError, match="exclusion"):
        RecordExclusions(store, CONTROL).excluded_ids(EVENT, [TARGET])


@pytest.mark.parametrize("phase", ["before", "during"])
def test_account_change_cannot_return_clean_empty_policy_even_when_no_row_is_visible(phase):
    from fulcra_common.record_exclusions import RecordExclusions
    class SwitchingStore(Store):
        reads = 0
        def get_records(self, *args):
            self.reads += 1
            self.account_id = OTHER_ACCOUNT
            return {}
    store = SwitchingStore()
    store.account_id = ACCOUNT
    control = RecordExclusions(store, CONTROL)
    if phase == "before":
        store.account_id = OTHER_ACCOUNT
    with pytest.raises(ValueError, match="exclusion"):
        control.excluded_ids(EVENT, [TARGET])
    assert store.reads == int(phase == "during")


@pytest.mark.parametrize("change", [
    {"target_data_type": METRIC}, {"target_record_id": OTHER_ACTION},
    {"action_id": "bad"}, {"reason": "bad"}, {"evidence": [False]},
    {"schema_version": True}, {"sources": ["another-target"]},
    {"end_time": "2026-01-03T00:00:00Z"}, {"start_time": "naive"},
])
def test_malformed_or_wrong_target_exclusion_is_unknown_not_clean_missing(change):
    from fulcra_common.record_exclusions import RecordExclusions
    store = Store()
    control = RecordExclusions(store, CONTROL)
    row = ensure(control)
    store.rows[row["id"]].update(change)
    with pytest.raises(ValueError, match="exclusion"):
        control.excluded_ids(EVENT, [TARGET])
    with pytest.raises(ValueError, match="exclusion"):
        ensure(control)
    assert len(store.posts) == 1


@pytest.mark.parametrize("change", [
    {"data_type": "MomentAnnotation"}, {"record_id": "bad"},
    {"action_id": "bad"}, {"action_id": ACTION.upper()},
    {"requested_at": NOW.replace(tzinfo=None)}, {"reason": "bad"},
    {"evidence": [None]},
])
def test_bad_exclusion_intent_fails_before_io(change):
    from fulcra_common.record_exclusions import RecordExclusions
    class NoIO(Store):
        def get_records(self, *args):
            raise AssertionError("intent must be validated before IO")
    with pytest.raises(ValueError):
        ensure(RecordExclusions(NoIO(), CONTROL), **change)


def test_exclusion_control_never_accepts_a_metric_or_legacy_destination():
    from fulcra_common.record_exclusions import RecordExclusions
    for destination in [METRIC, "Event", "MomentAnnotation"]:
        with pytest.raises(ValueError):
            RecordExclusions(Store(), destination)


def test_invalid_late_target_in_lookup_refuses_the_whole_batch_before_io():
    from fulcra_common.record_exclusions import RecordExclusions
    class NoIO(Store):
        def get_records(self, *args):
            raise AssertionError("all targets must be validated before IO")
    with pytest.raises(ValueError):
        RecordExclusions(NoIO(), CONTROL).excluded_ids(EVENT, [TARGET, "bad"])


def test_empty_lookup_needs_no_io_and_control_rows_cannot_be_excluded_recursively():
    from fulcra_common.record_exclusions import RecordExclusions
    class NoIO(Store):
        def get_records(self, *args):
            raise AssertionError("empty/recursive lookup must not contact API")
    control = RecordExclusions(NoIO(), CONTROL)
    assert control.excluded_ids(EVENT, []) == set()
    with pytest.raises(ValueError, match="control"):
        ensure(control, data_type=CONTROL)


@pytest.mark.parametrize("change", [None, {"userid": OTHER_ACCOUNT}, {"fulcra_userid": False},
    {"target_record_id": OTHER_ACTION}, {"action_id": "bad"}, {"reason": "bad"},
    {"evidence": [False]}, {"schema_version": True}, {"sources": ["wrong"]},
    {"start_time": "naive"}, {"end_time": "2026-01-03T00:00:00Z"}])
def test_retained_policy_rows_and_live_read_share_full_validation(change):
    # Dropping owner/action/time/target checks in the shared core must fail here.
    from fulcra_common import record_exclusions as module
    validate = getattr(module, "_validated_exclusion_rows", None)
    assert callable(validate), "retained policy needs the existing pure full-row validator"
    store = Store()
    store.account_id = ACCOUNT
    control = module.RecordExclusions(store, CONTROL)
    row = ensure(control)
    if change is not None:
        store.rows[row["id"]].update(change)
        with pytest.raises(ValueError, match="exclusion"):
            validate(CONTROL, EVENT, {row["id"]: TARGET}, store.rows, account_id=ACCOUNT)
        with pytest.raises(ValueError, match="exclusion"):
            control.excluded_ids(EVENT, [TARGET])
    else:
        store.rows[row["id"]]["server_added"] = "retained"
        assert validate(CONTROL, EVENT, {row["id"]: TARGET}, store.rows,
                        account_id=ACCOUNT) == {TARGET: store.rows[row["id"]]}
        assert control.excluded_ids(EVENT, [TARGET]) == {TARGET}


@pytest.mark.parametrize("rows", [[], None, {OTHER_ACTION: {}}])
def test_retained_policy_validator_rejects_nonmaps_and_foreign_control_ids(rows):
    from fulcra_common import record_exclusions as module
    validate = getattr(module, "_validated_exclusion_rows", None)
    assert callable(validate), "retained policy validator missing"
    with pytest.raises(ValueError, match="exclusion"):
        validate(CONTROL, EVENT, {}, rows, account_id=ACCOUNT)


@pytest.mark.parametrize("mode", ["renamed", "ambiguous", "unknown", "broken_named", "bound_missing", "capped", "invalid_schema"])
def test_control_discovery_uses_schema_or_durable_pin_without_relying_on_catalog_metadata(tmp_path, mode):
    import base64
    import httpx
    from fulcra_common.client import BaseFulcraClient
    from fulcra_common.data_types import CreationJournal
    from fulcra_common.record_exclusions import EXCLUSION_SPEC, resolve_exclusion_type
    account = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
    class Client(BaseFulcraClient):
        def get_token(self):
            claims = base64.urlsafe_b64encode(__import__('json').dumps({"fulcradynamics.com/userid": account}).encode()).decode().rstrip("=")
            return "synthetic." + claims + ".signature"
    schema = {"properties": {**{k: {} for k in ("id", "sources", "tags", "start_time", "end_time")},
                             **EXCLUSION_SPEC.fields}, "required": list(EXCLUSION_SPEC.required_fields)}
    catalog = [{"id": CONTROL, "name": EXCLUSION_SPEC.name if mode == "broken_named" else "Renamed control",
                "api_version": "v1", "fulcra_userid": account, "recordable": True, "deprecated": False,
                "record_spec": {"type": "event"}, "metadata": {"created_at": "synthetic"}}]
    if mode == "ambiguous":
        catalog.append({**catalog[0], "id": EVENT})
    if mode == "capped":
        from uuid import UUID
        catalog.extend({**catalog[0], "id": "Event/" + str(UUID(int=i + 1))} for i in range(64))
    journal = CreationJournal(tmp_path / "types.sqlite")
    if mode == "bound_missing":
        journal.resolved(account, EXCLUSION_SPEC, EVENT)
    calls = []
    def handler(request):
        calls.append(request.method)
        assert request.method == "GET", "discovery must never create around unknown prior policy"
        if request.url.path == "/input/v1/data_type":
            return httpx.Response(200, json=catalog)
        if mode == "unknown":
            return httpx.Response(503)
        if mode == "capped":
            raise AssertionError("capped discovery must stop before schema fan-out")
        if mode == "invalid_schema":
            return httpx.Response(200, json={"properties": []})
        return httpx.Response(200, json={"properties": {}} if mode == "broken_named" else schema)
    c = Client(base_url="https://api.example", transport=httpx.MockTransport(handler))
    if mode == "renamed":
        result = resolve_exclusion_type(client=c, account_id=account, journal=journal, allow_create=True)
        assert result.data_type == CONTROL
        assert journal.read(account, EXCLUSION_SPEC)[2] == CONTROL
    else:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            resolve_exclusion_type(client=c, account_id=account, journal=journal, allow_create=True)
    assert "POST" not in calls
