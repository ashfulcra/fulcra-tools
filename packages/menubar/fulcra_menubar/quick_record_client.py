"""Portable native capture workflow; GUI dispatch/threading is separate.

Call capture synchronously before acknowledging a click or starting a worker,
then retry the saved request in that worker. A timeout leaves the same private
request pending. This client never supplies new consent for an old epoch.
"""
import json
import re

from fulcra_common.v1 import _canonical_uuid
from fulcra_collect.record_actions import _json


def _scope(value):
    if not isinstance(value, dict) or type(value.get("protocol_version")) is not int or value["protocol_version"] != 2:
        raise ValueError("quick-record protocol scope changed; refresh before continuing")
    account = _canonical_uuid(value.get("account_id"))
    epoch = value.get("config_epoch")
    if not isinstance(epoch, str) or not 1 <= len(epoch) <= 256:
        raise ValueError("quick-record consent epoch changed; refresh before continuing")
    return {"protocol_version": 2, "account_id": account, "config_epoch": epoch}


def _matches(body, current):
    if any(body.get(key) != expected for key, expected in current.items()):
        raise ValueError("saved request account or epoch differs from the current scope")


def _favorites(value, revision):
    if (type(revision) is not int or not 0 <= revision < 2**63
            or not isinstance(value, list) or len(value) > 2048):
        raise ValueError("favorite response is malformed; refresh before editing")
    identities = [_canonical_uuid(identity) for identity in value]
    if len(set(identities)) != len(identities):
        raise ValueError("favorite response contains duplicate choices")


def _view(value):
    _scope(value)
    if value.get("ok") is not True:
        raise ValueError("quick-record handshake failed; refresh or reconnect in Collect")
    _favorites(value.get("favorites"), value.get("favorites_revision"))
    choices = value.get("definitions")
    if not isinstance(choices, list) or len(choices) > 2048:
        raise ValueError("quick-record choice handshake is incomplete")
    seen = set()
    for choice in choices:
        if (not isinstance(choice, dict) or _canonical_uuid(choice.get("id")) in seen
                or choice.get("annotation_type") not in {"moment", "duration"}
                or not isinstance(choice.get("digest"), str) or not re.fullmatch("[a-f0-9]{64}", choice["digest"])
                or type(choice.get("pinned")) is not bool
                or choice["pinned"] != (choice["id"] in value["favorites"])
                or not isinstance(choice.get("name"), str) or not 1 <= len(choice["name"]) <= 256):
            raise ValueError("quick-record choice handshake is malformed")
        seen.add(choice["id"])
    for key, bound in (("recent", 20), ("timers", 1000)):
        if not isinstance(value.get(key), list) or len(value[key]) > bound:
            raise ValueError("quick-record handshake is incomplete")
    from .quick_record_requests import _proof, _receipt, _time
    scope = _scope(value)
    recent, timers, seen_actions, seen_timers = [], [], set(), set()
    for row in value["recent"]:
        if not isinstance(row, dict):
            raise ValueError("recent history response is malformed")
        observed_scope = _scope({**scope, "config_epoch": row.get("captured_epoch")})
        identity = _canonical_uuid(row.get("action_id"))
        if (identity in seen_actions or row.get("operation") != "record"
                or not isinstance(row.get("name"), str) or not 1 <= len(row["name"]) <= 256
                or type(row.get("undone", False)) is not bool
                or row.get("undo_status") not in {None, "pending", "submitted", "verified", "excluded", "cancelled"}):
            raise ValueError("recent history response proof is malformed")
        _time(row.get("created_at"))
        _proof({**observed_scope, "operation": "record_annotation", "action_id": identity},
               {**observed_scope, **row}, identity)
        if row.get("undo_action_id") is not None:
            _canonical_uuid(row["undo_action_id"])
        recent.append(_receipt(row) | {"undone": row.get("undone", False), "undo_status": row.get("undo_status"), "undo_action_id": row.get("undo_action_id")}
                      | {"needs_reauthorization": row["captured_epoch"] != scope["config_epoch"]
                         and row["status"] in {"pending", "submitted"}})
        seen_actions.add(identity)
    for row in value["timers"]:
        if not isinstance(row, dict):
            raise ValueError("timer history response is malformed")
        identity = _canonical_uuid(row.get("timer_id"))
        _canonical_uuid(row.get("definition_id"))
        _scope({**scope, "config_epoch": row.get("captured_epoch")})
        start = _time(row.get("start_time"))
        if (identity in seen_timers or row.get("status") not in {"running", "stopping"}
                or not isinstance(row.get("name"), str) or not 1 <= len(row["name"]) <= 256):
            raise ValueError("timer history response proof is malformed")
        if row["status"] == "running":
            if row.get("end_time") is not None or row.get("action_id") is not None:
                raise ValueError("running timer response has a contradictory stop proof")
        else:
            _canonical_uuid(row.get("action_id"))
            if _time(row.get("end_time")) <= start:
                raise ValueError("timer response stop proof is malformed")
        timers.append(_receipt({"timer": row})["timer"])
        seen_timers.add(identity)
    return json.loads(_json({**value, "recent": recent, "timers": timers}, limit=8 * 1024 * 1024))


class QuickRecordClient:
    def __init__(self, daemon, journal):
        self.daemon, self.journal = daemon, journal

    def bootstrap(self):
        """One complete account-scoped view; failure never means no choices."""
        return _view(self.daemon.quick_record_v2("quick_record_list"))

    def change_pin(self, choice_id, *, pinned, view):
        captured = _view(view)
        scope = _scope(captured)
        _canonical_uuid(choice_id)
        if type(pinned) is not bool:
            raise ValueError("favorite intent must be on or off")
        fields = {**scope, "choice_id": choice_id, "pinned": pinned,
                  "expected_revision": captured["favorites_revision"]}
        if pinned:
            choice = next((choice for choice in captured["definitions"] if choice["id"] == choice_id), None)
            if choice is None:
                raise ValueError("choice is absent from the captured view; refresh")
            fields["choice_digest"] = choice["digest"]
        reply = self.daemon.quick_record_v2("set_quick_record_favorites", **fields)
        if _scope(reply) != scope or reply.get("ok") is not True:
            raise ValueError("favorite response failed or changed scope; refresh")
        _favorites(reply.get("favorites"), reply.get("revision"))
        expected = set(captured["favorites"])
        expected.add(choice_id) if pinned else expected.discard(choice_id)
        expected_revision = captured["favorites_revision"] + (expected != set(captured["favorites"]))
        if set(reply["favorites"]) != expected or reply["revision"] != expected_revision:
            raise ValueError("favorite response does not prove the captured pin change")
        return json.loads(_json(reply, limit=65536))

    def pending_actions(self, *, view, cursor=None):
        """Bounded saved server work beyond the recent display; read authority only."""
        from .quick_record_requests import _proof, _receipt
        scope = _scope(view)
        if cursor is not None and (not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048):
            raise ValueError("pending action page cursor is invalid")
        fields = {**scope, "statuses": ["pending", "submitted"], "limit": 50}
        if cursor is not None:
            fields["cursor"] = cursor
        response = self.daemon.quick_record_v2("quick_record_actions", **fields)
        if (_scope(response) != scope or response.get("ok") is not True
                or not isinstance(response.get("actions"), list) or len(response["actions"]) > 50):
            raise ValueError("pending action page response is incomplete or changed scope")
        following = response.get("next_cursor")
        if following is not None and (not isinstance(following, str) or not 1 <= len(following) <= 2048):
            raise ValueError("pending action page cursor is malformed")
        actions, seen = [], set()
        for row in response["actions"]:
            if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                    or not 1 <= len(row["name"]) <= 256):
                raise ValueError("pending action page response name is malformed")
            identity = _canonical_uuid(row.get("action_id"))
            observed_scope = _scope({**scope, "config_epoch": row.get("captured_epoch")})
            if identity in seen or row.get("operation") not in {"record", "delete"} or row.get("status") not in {"pending", "submitted"}:
                raise ValueError("pending action page response has an unknown or repeated row")
            predicate = {**observed_scope, "action_id": identity,
                         "operation": "delete_annotation" if row["operation"] == "delete" else "record_annotation"}
            _proof(predicate, {**observed_scope, **row}, identity)
            actions.append(_receipt(row) | {"needs_reauthorization": row["captured_epoch"] != scope["config_epoch"]})
            seen.add(identity)
        return {"actions": actions, "next_cursor": following}

    def reconcile(self, *, view, action=None, timer=None):
        """Validate a read/reconcile result against already observed metadata.

        This builds only a proof predicate. It never captures, changes or sends
        a fabricated record body; the daemon resumes its original saved intent.
        """
        from .quick_record_requests import _proof, _receipt, _time
        scope = _scope(view)
        if (action is None) == (timer is None):
            raise ValueError("reconcile needs exactly one observed action or timer")
        expected = action if action is not None else timer
        if expected.get("captured_epoch") != scope["config_epoch"]:
            raise ValueError("response proof requires explicit old-epoch consent recovery")
        if action is not None:
            identity = _canonical_uuid(action.get("action_id"))
            operation = action.get("operation")
            if operation not in {"record", "delete"}:
                raise ValueError("response operation proof is unavailable")
            predicate = {**scope, "action_id": identity,
                         "operation": "delete_annotation" if operation == "delete" else "record_annotation"}
            local = self.journal.get(identity, account_id=scope["account_id"])
            response = self.retry(identity, current_scope=view) if local is not None else (
                self.daemon.quick_record_v2("quick_record_reconcile", **scope, action_id=identity))
            _proof(predicate, response, identity, action)
        else:
            identity = _canonical_uuid(timer.get("timer_id"))
            action_id = _canonical_uuid(timer.get("action_id"))
            if timer.get("status") not in {"stopping", "verified", "excluded"}:
                raise ValueError("timer has no observed stop proof")
            predicate = {**scope, "operation": "quick_record_timer_stop", "timer_id": identity,
                         "end_time": timer["end_time"]}
            local = self.journal.timer_stop(identity, account_id=scope["account_id"])
            response = self.retry(local.request_id, current_scope=view) if local is not None else (
                self.daemon.quick_record_v2("quick_record_timer_reconcile", **scope, timer_id=identity))
            _proof(predicate, response, action_id)
            observed = response["timer"]
            if (observed.get("definition_id") != timer["definition_id"]
                    or _time(observed.get("start_time")) != _time(timer["start_time"])):
                raise ValueError("response timer proof changed its original track or start")
        return _receipt(response)

    def capture(self, *, request_id, body, current_scope):
        current = _scope(current_scope)
        _matches(body, current)
        return self.journal.capture(request_id=request_id, body=body)

    def submit(self, *, request_id, body, current_scope):
        self.capture(request_id=request_id, body=body, current_scope=current_scope)
        return self.retry(request_id, current_scope=current_scope)

    def retry(self, request_id, *, current_scope):
        current = _scope(current_scope)
        saved = self.journal.get(request_id, account_id=current["account_id"])
        if saved is None:
            raise ValueError("saved client request is missing from the current account scope")
        _matches(saved.body, current)
        if saved.status in {"verified", "excluded", "started"}:
            return saved.reply
        # Commit before connect/send, including a failed connection or reply
        # timeout. There is no background retry loop or action-ID generation.
        saved = self.journal.mark_attempt(request_id, account_id=current["account_id"], epoch=current["config_epoch"])
        body = dict(saved.body)
        operation = body.pop("operation")
        reply = self.daemon.quick_record_v2(operation, **body)
        settled = self.journal.accept_reply(request_id, account_id=current["account_id"],
                                            epoch=current["config_epoch"], reply=reply)
        return settled.reply


def native_client(daemon):
    """Open separate private client intent storage; no server DB/auth access."""
    from fulcra_collect.config import config_dir
    from fulcra_collect.record_actions import ActionJournal
    from .quick_record_requests import ClientRequestJournal
    return QuickRecordClient(daemon, ClientRequestJournal(ActionJournal(config_dir() / "quick-record-client.sqlite")))
