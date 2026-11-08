"""Private native-client requests, captured before the first socket attempt.

This file contains no GUI, credentials or network calls. Account/epoch scope is
immutable. A missing response leaves the same pending request; only a bound,
consistent backend proof can settle it. The server remains the record authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re

from fulcra_common.v1 import _canonical_uuid, _destination
from fulcra_collect.record_actions import ActionJournal, _scope, _json, _hash, _page_cursor, _cursor_after

_TERMINAL = {"verified", "excluded", "started"}
_STATUSES = _TERMINAL | {"queued", "pending"}
_FIELDS = {
    "record_annotation": {"action_id", "definition_id", "choice_digest", "start_time", "end_time", "comment"},
    "delete_annotation": {"action_id", "record_action_id"},
    "quick_record_timer_start": {"timer_id", "definition_id", "choice_digest", "start_time"},
    "quick_record_timer_stop": {"timer_id", "end_time", "comment"},
}


def _time(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise ValueError("client capture time requires an explicit timezone") from None


def _body(identity, value):
    encoded = _json(value, limit=32768)
    operation = value.get("operation")
    if not isinstance(operation, str) or operation not in _FIELDS:
        raise ValueError("client capture operation is unsupported")
    common = {"operation", "protocol_version", "account_id", "config_epoch"}
    if (not common <= set(value) or set(value) - common - _FIELDS[operation]
            or type(value["protocol_version"]) is not int or value["protocol_version"] != 2):
        raise ValueError("client request scope or fields are malformed")
    _scope(identity, value["account_id"], value["config_epoch"])
    if len(value["config_epoch"]) > 256:
        raise ValueError("client capture consent epoch is invalid")
    if operation in {"record_annotation", "delete_annotation"}:
        if _canonical_uuid(value.get("action_id")) != identity:
            raise ValueError("client request action identity changed")
    else:
        _canonical_uuid(value.get("timer_id"))
        if operation == "quick_record_timer_start" and value["timer_id"] != identity:
            raise ValueError("client timer start identity changed")
    if operation == "delete_annotation":
        _canonical_uuid(value.get("record_action_id"))
    if operation in {"record_annotation", "quick_record_timer_start"}:
        _canonical_uuid(value.get("definition_id"))
        digest = value.get("choice_digest")
        if not isinstance(digest, str) or not re.fullmatch("[0-9a-f]{64}", digest):
            raise ValueError("client choice digest is invalid")
        start = _time(value.get("start_time"))
        if value.get("end_time") is not None and _time(value["end_time"]) <= start:
            raise ValueError("client duration end must follow its start")
    elif operation == "quick_record_timer_stop":
        _time(value.get("end_time"))
    note = value.get("comment")
    if note is not None and (not isinstance(note, str) or len(note.encode()) > 16384):
        raise ValueError("client note must be bounded text")
    return encoded


def _proof(body, reply, prior_action, prior_reply=None):
    _json(reply, limit=65536)
    if (type(reply.get("protocol_version")) is not int or reply["protocol_version"] != 2
            or reply.get("account_id") != body["account_id"]
            or reply.get("config_epoch") != body["config_epoch"] or type(reply.get("ok")) is not bool):
        raise ValueError("response scope changed; original client request retained")
    operation = body["operation"]
    timer = reply.get("timer")
    if operation.startswith("quick_record_timer_"):
        if (not isinstance(timer, dict) or timer.get("timer_id") != body["timer_id"]
                or timer.get("captured_epoch") != body["config_epoch"]
                or timer.get("status") not in {"running", "stopping", "verified", "excluded"}):
            raise ValueError("response timer proof is missing or changed")
        if operation == "quick_record_timer_start":
            if (timer.get("definition_id") != body["definition_id"]
                    or _time(timer.get("start_time")) != _time(body["start_time"])):
                raise ValueError("response timer start proof changed")
            return ("started" if reply["ok"] else "pending"), None
        end = _time(timer.get("end_time"))
        expected_timer_status = reply.get("status") if reply.get("status") in {"verified", "excluded"} else "stopping"
        if (end != _time(body["end_time"]) or _time(timer.get("start_time")) >= end
                or timer["status"] != expected_timer_status):
            raise ValueError("response timer stop proof changed")
    expected_operation = "delete" if operation == "delete_annotation" else "record"
    action_id = _canonical_uuid(reply.get("action_id"))
    if (reply.get("operation") != expected_operation or reply.get("accepted") is not True
            or reply.get("captured_epoch") != body["config_epoch"]
            or (operation in {"record_annotation", "delete_annotation"} and action_id != body["action_id"])
            or (prior_action is not None and action_id != prior_action)
            or (operation == "quick_record_timer_stop" and timer.get("action_id") != action_id)):
        raise ValueError("response action proof is missing or changed")
    _destination(reply.get("data_type"), "Event")
    _canonical_uuid(reply.get("record_id"))
    if prior_reply and any(reply.get(key) != prior_reply.get(key) for key in ("data_type", "record_id")):
        raise ValueError("response target proof changed; original request retained")
    status = reply.get("status")
    if status == "verified" and reply["ok"]:
        return "verified", action_id
    if status == "excluded" and not reply["ok"] and expected_operation == "record":
        return "excluded", action_id
    if status in {"pending", "submitted"} and not reply["ok"]:
        return "pending", action_id
    raise ValueError("response status contradicts its proof; original request retained")


def _receipt(reply):
    # Keep result metadata; do not copy an exception message/payload to storage
    # or later UI logs. The original note remains in its private frozen request.
    keys = {"protocol_version", "account_id", "config_epoch", "ok", "accepted", "status", "action_id",
            "operation", "captured_epoch", "record_id", "data_type", "source_id", "name", "created_at",
            "timer", "code", "needs_reauthorization"}
    projected = {key: value for key, value in reply.items() if key in keys}
    if isinstance(projected.get("timer"), dict):
        timer_keys = {"timer_id", "captured_epoch", "definition_id", "name", "status",
                      "start_time", "end_time", "action_id"}
        projected["timer"] = {key: value for key, value in projected["timer"].items() if key in timer_keys}
    # 100 maximum-size requests + these receipts stay below a 4 MiB page.
    _json(projected, limit=4096)
    return projected


@dataclass(frozen=True)
class ClientRequest:
    request_id: str
    account_id: str
    epoch: str
    operation: str
    body: dict
    status: str
    reply: dict
    server_action_id: str | None


class ClientRequestJournal:
    def __init__(self, journal: ActionJournal):
        self.journal = journal
        with journal._db(write=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {"quick_record_client_meta", "quick_record_client_requests", "quick_record_client_timer_stops"}
            if tables & expected:
                if not expected <= tables:
                    raise ValueError("native client request schema is incomplete; preserve the original file")
                versions = db.execute("SELECT version FROM quick_record_client_meta").fetchall()
                if len(versions) != 1 or versions[0][0] != 1:
                    raise ValueError("native client request schema version is unsupported")
            else:
                db.execute("CREATE TABLE quick_record_client_meta(version INTEGER NOT NULL)")
                db.execute("INSERT INTO quick_record_client_meta VALUES (1)")
                db.execute("CREATE TABLE quick_record_client_requests ("
                    "request_id TEXT PRIMARY KEY,account_id TEXT NOT NULL,epoch TEXT NOT NULL,operation TEXT NOT NULL,"
                    "body_json TEXT NOT NULL,body_hash TEXT NOT NULL,status TEXT NOT NULL,"
                    "reply_json TEXT NOT NULL,reply_hash TEXT NOT NULL,server_action_id TEXT)")
                db.execute("CREATE INDEX client_requests_by_account ON quick_record_client_requests(account_id)")
                db.execute("CREATE TABLE quick_record_client_timer_stops ("
                           "timer_id TEXT PRIMARY KEY,request_id TEXT UNIQUE NOT NULL)")

    def _get(self, db, identity, account_id, epoch=None):
        row = db.execute("SELECT * FROM quick_record_client_requests WHERE request_id=?", (identity,)).fetchone()
        if row is None:
            return None
        if row["account_id"] != account_id or (epoch is not None and row["epoch"] != epoch):
            raise ValueError("client request belongs to a different account or consent epoch")
        try:
            body, reply = json.loads(row["body_json"]), json.loads(row["reply_json"])
            if (_body(identity, body) != row["body_json"] or _hash(row["body_json"]) != row["body_hash"]
                    or _json(reply, limit=4096) != row["reply_json"] or _hash(row["reply_json"]) != row["reply_hash"]
                    or row["status"] not in _STATUSES or body["account_id"] != account_id
                    or body["config_epoch"] != row["epoch"] or body["operation"] != row["operation"]):
                raise ValueError
            if body["operation"] == "quick_record_timer_stop":
                owner = db.execute("SELECT request_id FROM quick_record_client_timer_stops WHERE timer_id=?",
                                   (body["timer_id"],)).fetchone()
                if owner is None or owner["request_id"] != identity:
                    raise ValueError
            if reply:
                status, action = _proof(body, reply, row["server_action_id"])
                if status != row["status"] or action != row["server_action_id"] or _receipt(reply) != reply:
                    raise ValueError
            elif row["status"] in _TERMINAL or row["server_action_id"] is not None:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("client request state or response proof is corrupt or unreadable") from None
        return ClientRequest(identity, account_id, row["epoch"], row["operation"], body,
                             row["status"], reply, row["server_action_id"])

    def capture(self, *, request_id, body):
        encoded = _body(request_id, body)
        with self.journal._db(write=True) as db:
            prior = self._get(db, request_id, body["account_id"], body["config_epoch"])
            if prior is not None:
                if _json(prior.body) != encoded:
                    raise ValueError("client request changed; preserve its original intent")
                return prior
            if body["operation"] == "quick_record_timer_stop" and db.execute(
                    "SELECT 1 FROM quick_record_client_timer_stops WHERE timer_id=?", (body["timer_id"],)).fetchone():
                raise ValueError("timer already has a saved stop; retry its original request")
            db.execute("INSERT INTO quick_record_client_requests VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                (request_id, body["account_id"], body["config_epoch"], body["operation"], encoded,
                 _hash(encoded), "queued", "{}", _hash("{}")))
            if body["operation"] == "quick_record_timer_stop":
                db.execute("INSERT INTO quick_record_client_timer_stops VALUES (?,?)", (body["timer_id"], request_id))
            return self._get(db, request_id, body["account_id"], body["config_epoch"])

    def get(self, request_id, *, account_id):
        _canonical_uuid(request_id)
        _canonical_uuid(account_id)
        with self.journal._db() as db:
            if db.execute("SELECT 1 FROM quick_record_client_requests WHERE request_id=? AND account_id=?",
                          (request_id, account_id)).fetchone() is None:
                return None
            return self._get(db, request_id, account_id)

    def timer_stop(self, timer_id, *, account_id):
        """Find the first local stop for recovery; never grant a new epoch."""
        _canonical_uuid(timer_id)
        _canonical_uuid(account_id)
        with self.journal._db() as db:
            row = db.execute("SELECT stops.request_id FROM quick_record_client_timer_stops stops "
                "JOIN quick_record_client_requests requests ON requests.request_id=stops.request_id "
                "WHERE stops.timer_id=? AND requests.account_id=?", (timer_id, account_id)).fetchone()
            return self._get(db, row["request_id"], account_id) if row else None

    def mark_attempt(self, request_id, *, account_id, epoch):
        _scope(request_id, account_id, epoch)
        with self.journal._db(write=True) as db:
            prior = self._get(db, request_id, account_id, epoch)
            if prior is None:
                raise ValueError("client request is missing")
            if prior.status == "queued":
                db.execute("UPDATE quick_record_client_requests SET status='pending' WHERE request_id=?", (request_id,))
            return self._get(db, request_id, account_id, epoch)

    def accept_reply(self, request_id, *, account_id, epoch, reply):
        _scope(request_id, account_id, epoch)
        with self.journal._db(write=True) as db:
            prior = self._get(db, request_id, account_id, epoch)
            if prior is None or prior.status == "queued":
                raise ValueError("response has no saved dispatch intent")
            status, action = _proof(prior.body, reply, prior.server_action_id, prior.reply)
            saved = _json(_receipt(reply), limit=4096)
            if prior.status in _TERMINAL:
                if status != prior.status or saved != _json(prior.reply, limit=4096):
                    raise ValueError("settled client request proof cannot be replaced")
                return prior
            db.execute("UPDATE quick_record_client_requests SET status=?,reply_json=?,reply_hash=?,server_action_id=? "
                       "WHERE request_id=?", (status, saved, _hash(saved), action, request_id))
            return self._get(db, request_id, account_id, epoch)

    def pending(self, *, account_id, limit=50, cursor=None):
        _canonical_uuid(account_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("client request page limit must be between 1 and 100")
        scope = _hash(_json({"resource": "quick-record-client-pending-v1", "account_id": account_id}))
        after = _cursor_after(cursor, scope) if cursor is not None else None
        conditions, args = ["account_id=?", "status IN ('queued','pending')"], [account_id]
        with self.journal._db() as db:
            if db.execute("SELECT 1 FROM quick_record_client_requests WHERE account_id=? AND "
                "status NOT IN ('queued','pending','verified','excluded','started') LIMIT 1", (account_id,)).fetchone():
                raise ValueError("client request classification is corrupt or unreadable")
            if after is not None:
                row = db.execute("SELECT rowid FROM quick_record_client_requests WHERE request_id=? AND account_id=?",
                                 (after, account_id)).fetchone()
                if row is None:
                    raise ValueError("client request page cursor is unavailable; refresh")
                self._get(db, after, account_id)
                conditions.append("rowid<?")
                args.append(row[0])
            rows = db.execute("SELECT request_id FROM quick_record_client_requests WHERE "
                + " AND ".join(conditions) + " ORDER BY rowid DESC LIMIT ?", (*args, limit + 1)).fetchall()
            requests = tuple(self._get(db, row["request_id"], account_id) for row in rows[:limit])
            next_cursor = _page_cursor(requests[-1].request_id, scope) if len(rows) > limit else None
            return {"requests": requests, "next_cursor": next_cursor}
