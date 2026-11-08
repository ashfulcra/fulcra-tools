"""Real interactive authority/journal with synthetic HTTP ingestion delays."""
import base64
from datetime import datetime, timezone
import json
import re
from uuid import uuid4

import httpx
import pytest

from fulcra_collect.plugin import RunContext
from fulcra_collect.quick_record_v1 import QuickRecordSession, quick_record_context
from fulcra_collect.record_actions import ActionJournal
from fulcra_common.v1 import build_event_record
from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
DESTINATION = "Event/11111111-2222-4333-8444-555555555555"
CONTROL = "Event/11111111-2222-4333-8444-666666666666"
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def runtime(collect_home, monkeypatch):
    claims = base64.urlsafe_b64encode(json.dumps({"fulcradynamics.com/userid": ACCOUNT}).encode()).decode().rstrip("=")
    monkeypatch.setattr(RunContext, "collect_token", lambda _: "synthetic." + claims + ".signature")
    ctx, account = quick_record_context()
    journal = ActionJournal(collect_home / "quick-record-actions.sqlite")
    CreationJournal(collect_home / "v1-type-creations.sqlite").resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
    state = {"rows": {}, "posts": [], "delayed": [], "lose_reply": False, "delay": False,
             "bad_schema": False, "action_id": None, "guard_on_post": None}

    def handler(request):
        assert request.headers["authorization"].startswith("Bearer synthetic.")
        if request.url.path == "/input/v1/data_type":
            return httpx.Response(200, json=[{"id": CONTROL, "name": EXCLUSION_SPEC.name,
                "api_version": "v1", "recordable": True, "deprecated": False,
                "record_spec": {"type": "event"}}])
        if request.url.path == f"/data/v1/catalog/{CONTROL}/v1/schema":
            return httpx.Response(200, json={"properties": {
                **{key: {} for key in ("id", "start_time", "end_time", "sources", "tags")},
                **EXCLUSION_SPEC.fields}, "required": list(EXCLUSION_SPEC.required_fields)})
        if request.url.path == "/data/v1/records":
            query = request.url.params["q"]
            destination = json.loads(re.search(r'__name__=("[^"]+")', query).group(1))
            ids = re.findall(r',id="([^"]+)"', query)
            rows = [state["rows"][(destination, identity)] for identity in ids
                    if (destination, identity) in state["rows"]]
            return httpx.Response(200, text="\n".join(json.dumps(row) for row in rows))
        if request.method == "GET":
            properties = {key: {} for key in ("id", "start_time", "end_time", "sources", "tags",
                "note", "schema_version", "target_data_type", "target_record_id", "action_id",
                "reason", "evidence", "record_id", "data_type", "track_id", "track_name",
                "kind", "legacy_definition_id")}
            if state["bad_schema"]:
                properties.pop("note")
            return httpx.Response(200, json={"properties": properties})
        destination = request.url.path.removeprefix("/ingest/v1/record/")
        rows = [json.loads(line) for line in request.content.splitlines()]
        state["posts"].append((destination, rows))
        if destination == DESTINATION:
            pending = journal.inspect(state["action_id"], account_id=ACCOUNT)
            assert pending.status == "submitted" and pending.evidence["phase"] == "dispatch-intent"
            if state["guard_on_post"]:
                state["guard_on_post"]()
            for row in rows:
                if state["delay"]:
                    state["delayed"].append(row)
                else:
                    state["rows"][(destination, row["id"])] = row
            if state["lose_reply"]:
                raise httpx.ReadTimeout("synthetic lost response")
        elif destination == "DeletedRecord":
            for row in rows:
                state["rows"].pop((DESTINATION, row["record_id"]), None)
        else:
            for row in rows:
                state["rows"][(destination, row["id"])] = row
        return httpx.Response(201, json=state.get("target_receipt", {"upload_id": "synthetic-receipt"})
                              if destination == DESTINATION else {"upload_id": "synthetic-receipt"})

    with QuickRecordSession(ctx, expected_account=account, expected_epoch=ctx.config_epoch,
                            transport=httpx.MockTransport(handler)) as session:
        # The synthetic pin also has a real durable binding and current owned
        # catalog/schema proof; policy reads never bypass authority checks.
        session._exclusion_type = CONTROL
        yield session, journal, state


def record(actor, state, *, action_id=None, note="Synthetic note"):
    state["action_id"] = action_id or str(uuid4())
    return actor.record(action_id=state["action_id"], request={"note": note},
        build_payload=lambda request: {"data_type": DESTINATION, "record": build_event_record(
            data_type=DESTINATION, source_id="synthetic.quick." + state["action_id"],
            start_time=NOW, fields={"note": request["note"]})})


def actor_for(runtime):
    from fulcra_collect.quick_record_actor import QuickRecordActor
    session, journal, _ = runtime
    return QuickRecordActor(session, journal)


def test_verified_action_survives_restart_without_a_second_record_post(runtime):
    actor = actor_for(runtime)
    session, journal, state = runtime
    first = record(actor, state)
    assert first.status == "verified"
    fresh = type(actor)(session, ActionJournal(journal.path))
    again = record(fresh, state, action_id=first.action_id)
    assert again.payload == first.payload and again.created_at == first.created_at
    assert [destination for destination, _ in state["posts"]] == [DESTINATION]


@pytest.mark.parametrize("landed", [True, False])
def test_lost_post_response_reconciles_exact_id_and_never_blindly_resubmits(runtime, landed):
    actor = actor_for(runtime)
    session, journal, state = runtime
    state.update(lose_reply=True, delay=not landed)
    with pytest.raises(httpx.ReadTimeout):
        record(actor, state)
    pending = journal.inspect(state["action_id"], account_id=ACCOUNT)
    assert pending.status == "submitted" and pending.evidence["phase"] == "dispatch-intent"
    recovered = type(actor)(session, ActionJournal(journal.path)).reconcile(pending.action_id)
    assert recovered.status == ("verified" if landed else "submitted")
    assert len(state["posts"]) == 1
    if not landed:
        state["rows"][(DESTINATION, pending.payload["record"]["id"])] = state["delayed"][0]
        assert actor.reconcile(pending.action_id).status == "verified"
        assert len(state["posts"]) == 1


def test_receipt_with_delayed_index_stays_submitted_then_verifies_without_resend(runtime):
    actor = actor_for(runtime)
    _, _, state = runtime
    state["delay"] = True
    pending = record(actor, state)
    assert pending.status == "submitted" and pending.evidence["upload_id"] == "synthetic-receipt"
    assert actor.reconcile(pending.action_id).status == "submitted"
    state["rows"][(DESTINATION, pending.payload["record"]["id"])] = state["delayed"][0]
    assert actor.reconcile(pending.action_id).status == "verified"
    assert len(state["posts"]) == 1


def test_schema_failure_does_not_claim_record_dispatch_and_can_retry_same_intent(runtime):
    actor = actor_for(runtime)
    _, journal, state = runtime
    state["bad_schema"] = True
    with pytest.raises(ValueError, match="undeclared"):
        record(actor, state)
    pending = journal.inspect(state["action_id"], account_id=ACCOUNT)
    assert pending.status == "pending" and state["posts"] == []
    state["bad_schema"] = False
    assert record(actor, state, action_id=pending.action_id).status == "verified"


def test_native_undo_establishes_exclusion_before_deletion_and_never_creates_sentinel(runtime):
    actor = actor_for(runtime)
    _, journal, state = runtime
    original = record(actor, state)
    deletion = actor.undo(action_id=str(uuid4()), record_action_id=original.action_id)
    assert deletion.status == "verified" and deletion.operation == "delete"
    assert deletion.payload == {"data_type": DESTINATION, "record_id": original.payload["record"]["id"]}
    assert [destination for destination, _ in state["posts"]] == [DESTINATION, CONTROL, "DeletedRecord"]
    assert (DESTINATION, deletion.payload["record_id"]) not in state["rows"]
    assert journal.inspect(original.action_id, account_id=ACCOUNT).status == "verified"
    assert actor.undo(action_id=deletion.action_id, record_action_id=original.action_id) == deletion
    assert len(state["posts"]) == 3


def test_unknown_original_submission_cannot_be_reported_undone(runtime):
    actor = actor_for(runtime)
    _, _, state = runtime
    state["delay"] = True
    pending = record(actor, state)
    with pytest.raises(ValueError, match="verified"):
        actor.undo(action_id=str(uuid4()), record_action_id=pending.action_id)
    assert [destination for destination, _ in state["posts"]] == [DESTINATION]


def test_old_epoch_original_can_be_undone_only_by_new_current_authority(runtime):
    from fulcra_collect import config
    actor = actor_for(runtime)
    session, journal, state = runtime
    original = record(actor, state)
    cfg = config.load()
    cfg.rotate_plugin_epoch("quick-record")
    config.save(cfg)
    with pytest.raises(RuntimeError, match="changed"):
        actor.undo(action_id=str(uuid4()), record_action_id=original.action_id)
    ctx, account = quick_record_context()
    with QuickRecordSession(ctx, expected_account=account, expected_epoch=ctx.config_epoch,
                            transport=session.client._client()._transport) as current:
        current._exclusion_type = CONTROL
        deletion = type(actor)(current, journal).undo(action_id=str(uuid4()), record_action_id=original.action_id)
    assert deletion.status == "verified" and deletion.epoch != original.epoch


def test_changed_payload_after_submission_is_never_overwritten_or_verified(runtime):
    actor = actor_for(runtime)
    _, _, state = runtime
    state["delay"] = True
    pending = record(actor, state)
    row = pending.payload["record"]
    state["rows"][(DESTINATION, row["id"])] = row | {"note": "Changed elsewhere"}
    with pytest.raises(ValueError, match="mismatch"):
        actor.reconcile(pending.action_id)
    assert len(state["posts"]) == 1


def test_revoke_after_dispatch_preserves_unknown_intent_without_claiming_success(runtime, monkeypatch):
    from fulcra_collect import config
    actor = actor_for(runtime)
    _, journal, state = runtime

    def revoke():
        cfg = config.load()
        cfg.rotate_plugin_epoch("quick-record")
        # Compliant local settings writes wait for the actor's shared lease.
        # Model an external/out-of-band change visible to the final guard.
        monkeypatch.setattr(config, "load", lambda: cfg)

    state["guard_on_post"] = revoke
    with pytest.raises(RuntimeError, match="changed"):
        record(actor, state)
    pending = journal.inspect(state["action_id"], account_id=ACCOUNT)
    assert pending.status == "submitted" and pending.evidence == {"phase": "dispatch-intent"}
    assert [destination for destination, _ in state["posts"]] == [DESTINATION]


def test_preview_refuses_to_capture_an_action_or_issue_http(runtime):
    from fulcra_collect import config
    actor = actor_for(runtime)
    _, journal, state = runtime
    cfg = config.load()
    cfg.plugin_settings["quick-record"] = {"preview_only": True}
    config.save(cfg)
    with pytest.raises(RuntimeError):
        record(actor, state)
    assert journal.list_actions(account_id=ACCOUNT).actions == ()
    assert state["posts"] == []


def test_permanent_exclusion_before_first_dispatch_returns_excluded_without_create(runtime):
    actor = actor_for(runtime)
    session, journal, state = runtime
    identity = str(uuid4())
    row = build_event_record(data_type=DESTINATION, source_id="synthetic.quick." + identity,
                            start_time=NOW, fields={"note": "Synthetic note"})
    journal.capture(action_id=identity, account_id=ACCOUNT, epoch=session.epoch,
        operation="record", request={"note": "Synthetic note"},
        build_payload=lambda _: {"data_type": DESTINATION, "record": row})
    session.exclude_action_record(data_type=DESTINATION, record_id=row["id"],
        action_id=str(uuid4()), requested_at=NOW, evidence=["synthetic-operator-exclusion"])
    excluded = record(actor, state, action_id=identity)
    assert excluded.status == "excluded" and excluded.evidence["never_submitted"] is True
    assert [destination for destination, _ in state["posts"]] == [CONTROL]
