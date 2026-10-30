"""New v1 writers must remain bound to Collect's captured account and consent."""
import base64
import json
import logging
from datetime import datetime, timezone

import httpx
import pytest

from fulcra_collect import config
from fulcra_collect.plugin import RunContext
from fulcra_collect.state import PluginState

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
OTHER_ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-ffffffffffff"
DESTINATION = "Event/11111111-2222-4333-8444-555555555555"


def token(account=ACCOUNT, nonce=None):
    claims = {"fulcradynamics.com/userid": account}
    if nonce is not None:
        claims["nonce"] = nonce
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "synthetic." + payload + ".signature"


def context(tmp_path, monkeypatch, settings=None):
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path / "collect"))
    cfg = config.Config()
    cfg.enable("synthetic")
    cfg.update_plugin_settings("synthetic", settings or {})
    config.save(cfg)
    cfg = config.load()
    kv = {}
    ctx = RunContext(plugin_id="synthetic", config=settings or {}, credentials={},
                     state=PluginState(plugin_id="synthetic"), log=logging.getLogger("synthetic"),
                     _emit=lambda _: None, config_epoch=cfg.plugin_epochs.get("synthetic", ""),
                     _plugin_kv_get=lambda key, default: kv.get(key, default),
                     _plugin_kv_set=lambda key, value: kv.__setitem__(key, value))
    monkeypatch.setattr(ctx, "collect_token", lambda: token())
    return ctx


def record():
    from fulcra_common.v1 import build_event_record
    return build_event_record(data_type=DESTINATION, source_id="example.source.1",
                              start_time=datetime(2026, 1, 2, tzinfo=timezone.utc))


def schema():
    return {"type": "object", "properties": {k: {} for k in record()}}


@pytest.mark.parametrize("racing_exclusion", [False, True])
@pytest.mark.parametrize("revoke", [None, "account", "settings"])
def test_delivery_checks_owned_policy_and_repairs_an_exclusion_racing_post(tmp_path, monkeypatch, racing_exclusion, revoke):
    import re
    from fulcra_collect.v1_session import CollectRecordSession
    from fulcra_common.record_exclusions import EXCLUSION_SPEC
    from fulcra_common.v1 import build_event_record
    ctx = context(tmp_path, monkeypatch)
    current_token = [token()]
    monkeypatch.setattr(ctx, "collect_token", lambda: current_token[0])
    control = "Event/aaaaaaaa-2222-4333-8444-555555555555"
    rows, posted, deleted = {}, [], []
    target = record()
    control_schema = {"type": "object", "properties": {**schema()["properties"], **EXCLUSION_SPEC.fields},
                      "required": list(EXCLUSION_SPEC.required_fields)}
    def handler(request):
        path = request.url.path
        if path == "/input/v1/data_type":
            return httpx.Response(200, json=[{"id": control, "name": EXCLUSION_SPEC.name,
                "fulcra_userid": ACCOUNT, "api_version": "v1", "recordable": True, "deprecated": False, "record_spec": {"type": "event"}}])
        if path == "/data/v1/catalog/DeletedRecord/v1/schema":
            return httpx.Response(200, json={"properties": {"id": {}, "record_id": {}, "data_type": {}}})
        if path.endswith("/schema"):
            return httpx.Response(200, json=control_schema if control in path else schema())
        if path == "/data/v1/records":
            query = request.url.params["q"]
            destination = re.search('__name__="([^"]+)"', query).group(1)
            identities = re.findall('id="([^"]+)"', query)
            return httpx.Response(200, text="\n".join(json.dumps(rows[destination, i]) for i in identities if (destination, i) in rows))
        if path == "/ingest/v1/record/DeletedRecord":
            for line in request.content.splitlines():
                tombstone = json.loads(line)
                deleted.append(tombstone)
                rows.pop((DESTINATION, tombstone["record_id"]), None)
            return httpx.Response(201, json={"upload_id": "synthetic-delete"})
        if path == "/ingest/v1/record/" + DESTINATION:
            for line in request.content.splitlines():
                row = json.loads(line)
                rows[DESTINATION, row["id"]] = row
                posted.append(row)
            if racing_exclusion:
                exclusion = build_event_record(data_type=control,
                    source_id=f"collect:v1:excluded:{DESTINATION}:{target['id']}",
                    start_time=datetime(2026, 1, 2, tzinfo=timezone.utc),
                    fields={"schema_version": 1, "target_data_type": DESTINATION,
                        "target_record_id": target["id"], "action_id": OTHER_ACCOUNT,
                        "reason": "undo", "evidence": []})
                rows[control, exclusion["id"]] = exclusion
            if revoke == "account":
                current_token[0] = token(OTHER_ACCOUNT)
            elif revoke == "settings":
                changed = config.load()
                changed.plugin_settings["synthetic"] = {"source": "manual-change"}
                monkeypatch.setattr(config, "load", lambda: changed)
            return httpx.Response(201, json={"upload_id": "synthetic-source"})
        raise AssertionError("unexpected synthetic API path")
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        if revoke:
            with pytest.raises(RuntimeError, match="changed"):
                session.deliver_records(DESTINATION, [target], dispatch=memory_dispatch([target]))
            assert len(posted) == 1
            assert not deleted  # The old authority cannot issue a repair mutation.
            return
        result = session.deliver_records(DESTINATION, [target], dispatch=memory_dispatch([target]))
        assert result.verified_ids == (() if racing_exclusion else (target["id"],))
        assert result.excluded_ids == ((target["id"],) if racing_exclusion else ())
    assert len(posted) == 1
    assert len(deleted) == int(racing_exclusion)
    assert bool(rows.get((DESTINATION, target["id"]))) is (not racing_exclusion)


def test_delivery_preflight_rejects_schema_loss_before_policy_metadata(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    calls = []
    def handler(request):
        calls.append(request.url.path)
        assert request.url.path == f"/data/v1/catalog/{DESTINATION}/v1/schema"
        return httpx.Response(200, json=schema())
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(ValueError, match="undeclared"):
            session.deliver_records(DESTINATION, [{**record(), "private": "would-be-lost"}], dispatch=memory_dispatch([record()]))
    assert len(calls) == 1
    assert not (config.config_dir() / "v1-type-creations.sqlite").exists()


def test_renamed_tag_reuses_its_saved_current_account_identity(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    tag_id = "aaaaaaaa-bbbb-4ccc-8ddd-123456789abc"
    paths = []
    renamed = False
    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/user/v1alpha1/tag/id/" + tag_id:
            return httpx.Response(200, json={"id": tag_id, "name": "renamed", "fulcra_userid": ACCOUNT})
        assert not renamed, "a saved tag binding must not get replaced after a rename"
        assert request.url.path == "/user/v1alpha1/tag/name/synthetic"
        return httpx.Response(200, json={"id": tag_id, "name": "synthetic", "fulcra_userid": ACCOUNT})
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as first:
        assert first.resolve_tag("synthetic") == tag_id
    renamed = True
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as resumed:
        assert resumed.resolve_tag("synthetic") == tag_id
    assert paths[-1] == "/user/v1alpha1/tag/id/" + tag_id


def test_long_new_tag_uses_a_stable_short_name_without_losing_the_source_label(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    label = "Synthetic source label that exceeds thirty characters"
    tag_id = "aaaaaaaa-bbbb-4ccc-8ddd-123456789abc"
    created = []
    exists = False
    def handler(request):
        nonlocal exists
        if request.method == "POST":
            name = json.loads(request.content)["name"]
            assert len(name) <= 30 and name != label
            created.append(name)
            exists = True
            return httpx.Response(201, json={"id": tag_id})
        if request.url.path == "/user/v1alpha1/tag":
            return httpx.Response(200, json=[])
        if request.url.path == "/user/v1alpha1/tag/id/" + tag_id:
            return httpx.Response(200, json={"id": tag_id, "name": created[0]})
        return httpx.Response(200, json={"id": tag_id, "name": created[0]}) if exists else httpx.Response(404)
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        assert session.resolve_tag(label) == tag_id
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as resumed:
        assert resumed.resolve_tag(label) == tag_id
    assert len(created) == 1


def test_old_long_tag_is_reused_instead_of_creating_a_short_replacement(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    label = "Synthetic legacy label longer than thirty characters"
    tag_id = "aaaaaaaa-bbbb-4ccc-8ddd-123456789abc"
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/user/v1alpha1/tag"
        return httpx.Response(200, json=[{"id": tag_id, "name": label}])
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        assert session.resolve_tag(label) == tag_id



def test_session_uses_collect_token_and_preserves_receipt(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    requests = []

    def handler(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + token()
        return httpx.Response(200, json=schema()) if request.method == "GET" else httpx.Response(201, json={"upload_id": "synthetic-upload"})

    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        assert session.account_id == ACCOUNT
        assert session.ingest(DESTINATION, [record()]) == {"upload_id": "synthetic-upload"}
    assert [r.method for r in requests] == ["GET", "POST"]


def test_missing_collect_signin_never_falls_back_to_cli_account(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    monkeypatch.setattr(ctx, "collect_token", lambda: None)
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", token(OTHER_ACCOUNT))
    with pytest.raises(ValueError, match="Reconnect"):
        CollectRecordSession(ctx)


@pytest.mark.parametrize("account", [None, "not-a-user-uuid"])
def test_invalid_account_claim_cannot_establish_a_record_session(tmp_path, monkeypatch, account):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    monkeypatch.setattr(ctx, "collect_token", lambda: token(account))
    with pytest.raises(ValueError, match="authenticated account"):
        CollectRecordSession(ctx)


@pytest.mark.parametrize("change", ["disabled", "settings", "epoch", "transition"])
def test_changed_consent_cancels_before_network(tmp_path, monkeypatch, change):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)

    def handler(request):
        raise AssertionError("cancelled work must not contact Fulcra")

    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        cfg = config.load()
        if change == "disabled":
            cfg.disable("synthetic")
            config.save(cfg)
        elif change == "settings":
            cfg.update_plugin_settings("synthetic", {"source": "changed"})
            config.save(cfg)
        elif change == "epoch":
            cfg.rotate_plugin_epoch("synthetic")
            config.save(cfg)
        else:
            # Simulate an interrupted persisted account transition.
            config._persist_account_transition(config._config_path(), True)
        with pytest.raises(RuntimeError, match="changed"):
            session.ingest(DESTINATION, [record()])


def test_token_change_during_schema_read_prevents_record_post(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    current = [token()]
    monkeypatch.setattr(ctx, "collect_token", lambda: current[0])
    calls = []

    def handler(request):
        calls.append(request.method)
        current[0] = token(OTHER_ACCOUNT)
        return httpx.Response(200, json=schema())

    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(RuntimeError, match="changed"):
            session.ingest(DESTINATION, [record()])
    assert calls == ["GET"]


def test_same_account_refresh_updates_request_auth_without_changing_captured_consent(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    current = [token()]
    monkeypatch.setattr(ctx, "collect_token", lambda: current[0])
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == "GET":
            current[0] = token(nonce="refreshed-same-account")
            return httpx.Response(200, json=schema())
        assert request.headers["authorization"] == "Bearer " + current[0]
        return httpx.Response(201, json={"upload_id": "synthetic"})
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        assert session.ingest(DESTINATION, [record()]) == {"upload_id": "synthetic"}
        assert session.account_id == ACCOUNT
    assert calls == ["GET", "POST"]


def test_external_redirect_is_cancelled_before_receiving_collect_credentials(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    hosts = []
    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "outside.example":
            return httpx.Response(200, json=schema())
        return httpx.Response(307, headers={"Location": "https://outside.example/schema"})
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(RuntimeError, match="origin"):
            session.ingest(DESTINATION, [record()])
    assert hosts == ["api.fulcradynamics.com"]


def test_preview_cannot_create_or_upload(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch, {"dry_run": True})

    def handler(request):
        raise AssertionError("preview must not upload")

    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(RuntimeError, match="Preview"):
            session.ingest(DESTINATION, [record()])


@pytest.mark.parametrize("landed", [False, True])
def test_tag_creation_lost_receipt_is_reconciled_never_blindly_repeated(tmp_path, monkeypatch, landed):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    tag_id = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    requests = []
    exists = [False]

    def handler(request):
        requests.append(request.method)
        if request.method == "GET":
            assert request.url.path == "/user/v1alpha1/tag/name/synthetic"
            return httpx.Response(200, json={"id": tag_id, "name": "synthetic"}) if exists[0] else httpx.Response(404)
        assert json.loads(request.content) == {"name": "synthetic"}
        exists[0] = landed
        raise httpx.ReadTimeout("synthetic lost response", request=request)

    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(httpx.ReadTimeout):
            session.resolve_tag("synthetic")
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        if landed:
            assert session.resolve_tag("synthetic") == tag_id
        else:
            with pytest.raises(RuntimeError, match="tag creation.*unresolved"):
                session.resolve_tag("synthetic")
    assert requests == ["GET", "POST", "GET"]


def test_tag_resolution_checks_owner_and_consent_without_cli_fallback(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    def handler(request):
        return httpx.Response(200, json={"id": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                                       "name": "synthetic", "fulcra_userid": OTHER_ACCOUNT})
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(ValueError, match="tag"):
            session.resolve_tag("synthetic")


def test_tag_creation_intent_is_shared_across_plugins_on_the_same_account(tmp_path, monkeypatch):
    from dataclasses import replace
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    cfg = config.load()
    cfg.enable("second")
    config.save(cfg)
    cfg = config.load()
    other = replace(ctx, plugin_id="second", config_epoch=cfg.plugin_epochs["second"],
                    _plugin_kv_get=lambda key, default: default, _plugin_kv_set=lambda *_: None)
    monkeypatch.setattr(other, "collect_token", lambda: token())
    requests = []
    def handler(request):
        requests.append(request.method)
        if request.method == "GET":
            return httpx.Response(404)
        raise httpx.ReadTimeout("synthetic lost response", request=request)
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as first:
        with pytest.raises(httpx.ReadTimeout):
            first.resolve_tag("shared-synthetic")
    with CollectRecordSession(other, transport=httpx.MockTransport(handler)) as second:
        with pytest.raises(RuntimeError, match="tag creation.*unresolved"):
            second.resolve_tag("shared-synthetic")
    assert requests == ["GET", "POST", "GET"]


def test_read_only_tag_resolution_never_creates_missing_tags_or_journals(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch, {"dry_run": True})
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(404)
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        with pytest.raises(ValueError, match="existing tag"):
            session.resolve_tag("synthetic", allow_create=False)
    assert not (config.config_dir() / "v1-type-creations.sqlite").exists()


def test_read_only_destination_resolution_does_not_create_local_intents(tmp_path, monkeypatch):
    from fulcra_collect.v1_session import CollectRecordSession
    from fulcra_common.data_types import DataTypeSpec
    ctx = context(tmp_path, monkeypatch, {"dry_run": True})
    spec = DataTypeSpec(key="synthetic.v1", base_type="Event", name="Synthetic", description="Synthetic records.")
    def handler(request):
        assert request.method == "GET"
        if request.url.path == "/input/v1/data_type":
            return httpx.Response(200, json=[{"id": DESTINATION, "name": "Synthetic", "fulcra_userid": ACCOUNT,
                "recordable": True, "deprecated": False, "api_version": "v1", "record_spec": {"type": "event"}}])
        return httpx.Response(200, json=schema())
    with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
        assert session.resolve_destination(spec, allow_create=False).data_type == DESTINATION
    assert not (config.config_dir() / "v1-type-creations.sqlite").exists()


def memory_dispatch(rows):
    from fulcra_common.record_delivery import SourceDispatch, prepared_source_dispatch
    return SourceDispatch({}, rows, prepared_source_dispatch({}, rows), lambda _: None)


@pytest.fixture
def durable_source(tmp_path, monkeypatch):
    """Real session, HTTP serializer, source lease and reopened SQLite callbacks."""
    import re
    from types import SimpleNamespace
    from copy import deepcopy
    from fulcra_collect import db
    from fulcra_collect.v1_outbox import V1RecordOutbox
    from fulcra_collect.v1_session import CollectRecordSession
    ctx = context(tmp_path, monkeypatch)
    state = SimpleNamespace(rows={}, posts=[], received=[], blocked=set(), visible=False,
                            lost=False, checkpoint_error=False, repair_error=False, on_post=lambda: None, on_schema=lambda: None)
    def get(key, default=None):
        conn = db.open()
        try:
            return db.get_record_outbox(conn, ctx.plugin_id, key, default)
        finally:
            db.close_all()
    def put(key, value):
        conn = db.open()
        try:
            db.set_record_outbox(conn, ctx.plugin_id, key, value)
        finally:
            db.close_all()
        if state.checkpoint_error and any(v['phase'] == 'issued' for v in value.get('dispatch', {}).values()):
            raise RuntimeError('uncertain checkpoint write')
    ctx._record_outbox_get, ctx._record_outbox_set = get, put
    def handler(request):
        path = request.url.path
        if path.endswith('/schema'):
            state.on_schema()
            properties = {"id": {}, "record_id": {}, "data_type": {}} if 'DeletedRecord' in path else schema()['properties']
            return httpx.Response(200, json={"properties": properties})
        if path == '/data/v1/records':
            ids = re.findall('id="([^"]+)"', request.url.params['q'])
            return httpx.Response(200, text='\n'.join(json.dumps(state.rows[i]) for i in ids if i in state.rows))
        batch = [json.loads(line) for line in request.content.splitlines()]
        if path == '/ingest/v1/record/DeletedRecord':
            for row in batch:
                state.rows.pop(row['record_id'], None)
            if state.repair_error:
                raise httpx.ReadError('uncertain native repair')
            return httpx.Response(201, json={})
        assert path == '/ingest/v1/record/' + DESTINATION
        state.posts.append(bytes(request.content))
        state.received.extend(deepcopy(batch))
        if state.visible:
            state.rows.update({row['id']: deepcopy(row) for row in batch})
        state.on_post()
        if state.lost:
            raise httpx.ReadError('uncertain source reply')
        return httpx.Response(201, json={"upload_id": "synthetic"})
    def fresh():
        session = CollectRecordSession(ctx, transport=httpx.MockTransport(handler))
        policy = SimpleNamespace(data_type=DESTINATION,
            excluded_ids=lambda _, ids: set(ids) & state.blocked)
        session._exclusions = lambda **_: policy
        return V1RecordOutbox(ctx, session), session
    state.ctx, state.get, state.put, state.fresh = ctx, get, put, fresh
    return state


@pytest.mark.parametrize('lost', [False, True])
def test_actual_sqlite_restart_retains_issued_absence_then_completes_exactly(durable_source, lost):
    from copy import deepcopy
    state = durable_source
    box, session = state.fresh()
    batch = [record()]
    box.stage({DESTINATION: batch}, message='Synthetic')
    state.lost = lost
    with pytest.raises((RuntimeError, httpx.ReadError)):
        box.drain()
    session.close()
    state.lost = False
    box, session = state.fresh()
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        box.drain()
    assert len(state.posts) == 1
    issued = state.get('v1_pending_records')['dispatch'][DESTINATION]
    assert issued['phase'] == 'issued' and issued['issued_records'] == batch
    state.rows[batch[0]['id']] = deepcopy(batch[0])
    assert box.drain() == 1
    assert len(state.posts) == 1 and not state.get('v1_pending_records')['groups']
    session.close()


def test_actual_checkpoint_write_then_raise_prevents_post_and_future_replay(durable_source):
    state = durable_source
    box, session = state.fresh()
    box.stage({DESTINATION: [record()]}, message='Synthetic')
    state.checkpoint_error = True
    with pytest.raises(RuntimeError, match='checkpoint'):
        box.drain()
    assert not state.posts
    session.close()
    state.checkpoint_error = False
    box, session = state.fresh()
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        box.drain()
    assert not state.posts
    assert state.get('v1_pending_records')['dispatch'][DESTINATION]['phase'] == 'issued'
    session.close()


def test_actual_schema_rejection_never_consumes_source_attempt(durable_source):
    state = durable_source
    box, session = state.fresh()
    box.stage({DESTINATION: [{**record(), 'undeclared': 'reject'}]}, message='Synthetic')
    original = state.get('v1_pending_records')
    with pytest.raises(ValueError, match='undeclared'):
        box.drain()
    assert not state.posts and state.get('v1_pending_records') == original
    session.close()


def test_actual_issued_partial_readback_never_resends_subset(durable_source):
    from uuid import uuid4
    state = durable_source
    first, second, third = record(), {**record(), 'id': str(uuid4())}, {**record(), 'id': str(uuid4())}
    state.rows[first['id']] = first
    box, session = state.fresh()
    box.stage({DESTINATION: [first, second, third]}, message='Synthetic')
    with pytest.raises(RuntimeError, match='visible'):
        box.drain()
    assert [json.loads(line) for line in state.posts[0].splitlines()] == [second, third]
    session.close()
    state.rows[second['id']] = second
    box, session = state.fresh()
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        box.drain()
    assert len(state.posts) == 1
    state.rows[third['id']] = third
    assert box.drain() == 3 and len(state.posts) == 1
    session.close()


def test_actual_issued_new_policy_absence_requires_positive_original(durable_source):
    state = durable_source
    box, session = state.fresh()
    box.stage({DESTINATION: [record()]}, message='Synthetic')
    with pytest.raises(RuntimeError):
        box.drain()
    state.blocked.add(record()['id'])
    original = state.get('v1_pending_records')
    with pytest.raises(RuntimeError, match='UNKNOWN'):
        box.drain()
    assert len(state.posts) == 1 and state.get('v1_pending_records') == original
    session.close()


def test_actual_positive_evidence_survives_native_repair_erasure_and_restart(durable_source):
    state = durable_source
    state.visible = True
    state.on_post = lambda: state.blocked.add(record()['id'])
    state.repair_error = True
    box, session = state.fresh()
    box.stage({DESTINATION: [record()]}, message='Synthetic')
    with pytest.raises(httpx.ReadError, match='native repair'):
        box.drain()
    pending = state.get('v1_pending_records')
    assert pending['dispatch'][DESTINATION]['positive_ids'] == [record()['id']]
    assert not state.rows and len(state.posts) == 1
    session.close()
    state.repair_error = False
    box, session = state.fresh()
    assert box.drain() == 0 and len(state.posts) == 1
    assert not state.get('v1_pending_records')['groups']
    session.close()


def test_actual_concurrent_invocations_read_pending_under_source_lease(durable_source):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    state = durable_source
    box, session = state.fresh()
    box.stage({DESTINATION: [record()]}, message='Synthetic')
    session.close()
    entered, release, second_read = Event(), Event(), Event()
    state.on_post = lambda: (entered.set(), release.wait(3))
    original_get = state.ctx._record_outbox_get
    def run(second=False):
        box, session = state.fresh()
        if second:
            # The second source read cannot occur while the first is in POST.
            box._get = lambda key, default: (second_read.set(), original_get(key, default))[1]
        try:
            with pytest.raises(RuntimeError):
                box.drain()
        finally:
            session.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run)
        assert entered.wait(2)
        second = pool.submit(run, True)
        assert not second_read.wait(.1)
        release.set()
        first.result(timeout=4)
        second.result(timeout=4)
    assert second_read.is_set() and len(state.posts) == 1


def test_actual_new_process_cannot_reissue_sqlite_pending_source(durable_source):
    import subprocess
    import sys
    import textwrap
    state = durable_source
    box, session = state.fresh()
    box.stage({DESTINATION: [record()]}, message='Synthetic')
    with pytest.raises(RuntimeError, match='visible'):
        box.drain()
    original = state.get('v1_pending_records')
    session.close()
    script = textwrap.dedent('''
        import base64, json, logging
        from types import SimpleNamespace
        import httpx
        from fulcra_collect import config, db
        from fulcra_collect.plugin import RunContext
        from fulcra_collect.v1_session import CollectRecordSession
        from fulcra_collect.v1_outbox import V1RecordOutbox
        cfg = config.load()
        pending = db.get_record_outbox(db.open(), 'synthetic', 'v1_pending_records')
        account = pending['account_id']
        claims = base64.urlsafe_b64encode(json.dumps({'fulcradynamics.com/userid': account}).encode()).decode().rstrip('=')
        ctx = RunContext(plugin_id='synthetic', config=cfg.plugin_settings['synthetic'],
            config_epoch=cfg.plugin_epochs['synthetic'], credentials={}, state=SimpleNamespace(),
            log=logging.getLogger('synthetic'), _emit=lambda _: None,
            _record_outbox_get=lambda k, d: db.get_record_outbox(db.open(), 'synthetic', k, d),
            _record_outbox_set=lambda k, v: db.set_record_outbox(db.open(), 'synthetic', k, v))
        ctx.collect_token = lambda: 'synthetic.' + claims + '.signature'
        posts = []
        def handler(request):
            if request.url.path.endswith('/schema'):
                return httpx.Response(200, json={'properties': {k: {} for k in pending['groups'][0]['records'][0]}})
            if request.method == 'GET':
                return httpx.Response(200, text='')
            posts.append(1)
            raise AssertionError('source POST must never occur in restarted process')
        with CollectRecordSession(ctx, transport=httpx.MockTransport(handler)) as session:
            session._exclusions = lambda **_: SimpleNamespace(excluded_ids=lambda *a: set())
            try:
                V1RecordOutbox(ctx, session).drain()
            except RuntimeError as error:
                assert 'UNKNOWN' in str(error)
                print('UNKNOWN', len(posts))
            else:
                raise AssertionError('absent source must hold pending')
    ''')
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == 'UNKNOWN 0'
    assert state.get('v1_pending_records') == original and len(state.posts) == 1


@pytest.mark.parametrize('change', ['account', 'settings'])
def test_actual_scope_change_during_schema_prevents_checkpoint_and_source_post(durable_source, change, monkeypatch):
    state = durable_source
    box, session = state.fresh()
    box.stage({DESTINATION: [record()]}, message='Synthetic')
    original = state.get('v1_pending_records')
    def revoke():
        if change == 'account':
            monkeypatch.setattr(state.ctx, 'collect_token', lambda: token(OTHER_ACCOUNT))
        else:
            current = config.load()
            current.plugin_settings['synthetic'] = {'source': 'changed'}
            monkeypatch.setattr(config, 'load', lambda: current)
    state.on_schema = revoke
    with pytest.raises(RuntimeError, match='changed'):
        box.drain()
    assert not state.posts and state.get('v1_pending_records') == original
    session.close()
