"""Protocol2 controller shared by the daemon socket and authenticated HTTP."""
from datetime import datetime, timezone
import re

from fulcra_common.v1 import _canonical_uuid

from .config_leases import task_mutation_scope
from .quick_record_actor import QuickRecordActor
from .quick_record_choices import QuickRecordChoices, build_quick_record
from .quick_record_state import QuickRecordState
from .record_actions import _json, _hash

_SCOPE = {"protocol_version", "account_id", "config_epoch"}
_FIELDS = {
    "quick_record_list": set(), "get_quick_record_favorites": set(),
    "record_annotation": {"action_id", "definition_id", "choice_digest", "comment", "start_time", "end_time"},
    "delete_annotation": {"action_id", "record_action_id"},
    "set_quick_record_favorites": {"choice_id", "choice_digest", "pinned", "expected_revision"},
    "quick_record_action": {"action_id"}, "quick_record_reconcile": {"action_id"},
    "quick_record_timer_start": {"timer_id", "definition_id", "choice_digest", "start_time"},
    "quick_record_timer_stop": {"timer_id", "end_time", "comment"},
    "quick_record_timer_reconcile": {"timer_id"},
    "quick_record_timers": {"include_finished", "limit", "cursor"},
    "quick_record_actions": {"action_kind", "statuses", "limit", "cursor"},
}
COMMANDS = frozenset(_FIELDS)


def _time(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError
        return instant.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise ValueError("quick-record timestamp must include a timezone") from None


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("quick-record choice changed; refresh before recording")
    return value


def _note(value):
    if value is not None and (not isinstance(value, str) or len(value.encode("utf-8")) > 16384):
        raise ValueError("quick-record note must be bounded text")
    _json({"note": value}, limit=32768)
    return value


def action_view(action):
    payload = action.payload
    row = payload.get("record", {})
    sources = row.get("sources", [])
    return {"ok": action.status == "verified", "accepted": True,
            "action_id": action.action_id, "operation": action.operation, "status": action.status,
            "captured_epoch": action.epoch, "data_type": payload["data_type"],
            "record_id": row.get("id", payload.get("record_id")),
            "source_id": sources[0] if sources else None,
            "name": row.get("track_name", "Recorded event"), "created_at": action.created_at}


def timer_view(timer):
    return {"timer_id": timer["timer_id"], "captured_epoch": timer["epoch"],
            "definition_id": timer["choice"]["id"], "name": timer["choice"]["name"],
            "status": timer["status"], "start_time": timer["started_at"],
            "end_time": timer.get("ended_at"), "action_id": timer.get("action_id")}


class QuickRecordController:
    def __init__(self, session, journal):
        self.session, self.journal = session, journal
        self.state = QuickRecordState(journal)
        self.choices = QuickRecordChoices(session, journal)
        self.actor = QuickRecordActor(session, journal)

    def scope(self):
        return {"protocol_version": 2, "account_id": self.session.account_id, "config_epoch": self.session.epoch}

    def _check_scope(self, request):
        if (type(request.get("protocol_version")) is not int or request["protocol_version"] != 2
                or any(request.get(key) != value for key, value in self.scope().items())):
            raise ValueError("quick-record scope or protocol changed; refresh before continuing")

    def dispatch(self, request):
        if not isinstance(request, dict) or request.get("cmd") not in COMMANDS:
            raise ValueError("unknown quick-record command; refresh before continuing")
        command = request["cmd"]
        if set(request) - {"cmd"} - _SCOPE - _FIELDS[command]:
            raise ValueError("quick-record request has unsupported fields; refresh before continuing")
        readonly = command in {"quick_record_list", "get_quick_record_favorites", "quick_record_action",
                               "quick_record_timers", "quick_record_actions"}
        if command not in {"quick_record_list", "get_quick_record_favorites"} or set(request) & _SCOPE:
            self._check_scope(request)
        with task_mutation_scope() if readonly else self.session.record_write_scope():
            self.session.guard(mutating=not readonly)
            if command == "quick_record_list":
                choices = self.choices.list()
                favorites = self.state.favorites(self.session.account_id)
                pinned = set(favorites["favorites"])
                definitions = [{key: value for key, value in choice.items() if key != "tags"}
                               | {"pinned": choice["id"] in pinned} for choice in choices]
                reply = {"ok": True, "definitions": definitions, "favorites": favorites["favorites"],
                         "favorites_revision": favorites["revision"], "recent": self._recent(),
                         "timers": [timer_view(timer) for timer in self.state.timers(self.session.account_id)]}
            elif command == "get_quick_record_favorites":
                reply = {"ok": True, **self.state.favorites(self.session.account_id)}
            elif command == "set_quick_record_favorites":
                identity = _canonical_uuid(request.get("choice_id"))
                if type(request.get("pinned")) is not bool:
                    raise ValueError("quick-record favorite must be on or off")
                if request["pinned"]:
                    self._select(identity, _digest(request.get("choice_digest")))
                reply = {"ok": True, **self.session.commit(lambda: self.state.change_favorites(
                    self.session.account_id, expected_revision=request.get("expected_revision"),
                    add=[identity] if request["pinned"] else [], remove=[] if request["pinned"] else [identity]))}
            elif command == "record_annotation":
                reply = action_view(self._record(request))
            elif command == "delete_annotation":
                reply = action_view(self.actor.undo(action_id=_canonical_uuid(request.get("action_id")),
                    record_action_id=_canonical_uuid(request.get("record_action_id"))))
            elif command == "quick_record_action":
                action = self.journal.inspect(_canonical_uuid(request.get("action_id")), account_id=self.session.account_id)
                if action is None:
                    raise ValueError("quick-record action is missing from this account")
                reply = action_view(action) | {"needs_reauthorization": action.epoch != self.session.epoch
                                               and action.status in {"pending", "submitted"}}
            elif command == "quick_record_actions":
                page = self.journal.list_actions(account_id=self.session.account_id,
                    operation=request.get("action_kind"), statuses=request.get("statuses"),
                    limit=request.get("limit", 50), cursor=request.get("cursor"))
                reply = {"ok": True, "actions": [action_view(action) | {
                    "needs_reauthorization": action.epoch != self.session.epoch and action.status in {"pending", "submitted"}}
                    for action in page.actions], "next_cursor": page.next_cursor}
            elif command == "quick_record_reconcile":
                action = self.actor.reconcile(_canonical_uuid(request.get("action_id")))
                reply = action_view(action)
                if (action.operation == "record" and action.status in {"verified", "excluded"}
                        and "timer_id" in action.request):
                    timer_id = _canonical_uuid(action.request["timer_id"])
                    with self.journal._db() as db:
                        timer = self.state._timer(db, timer_id, self.session.account_id, self.session.epoch)
                    if timer is None or timer.get("action_id") != action.action_id:
                        raise ValueError("timer does not reference the reconciled action")
                    timer = self.session.commit(lambda: self.state.finish_timer(timer_id,
                        account_id=self.session.account_id, epoch=self.session.epoch))
                    reply["timer"] = timer_view(timer)
            elif command == "quick_record_timer_start":
                reply = self._start_timer(request)
            elif command == "quick_record_timers":
                page = self.state.timer_page(self.session.account_id,
                    include_finished=request.get("include_finished", True), limit=request.get("limit", 50),
                    cursor=request.get("cursor"))
                reply = {"ok": True, "timers": [timer_view(timer) for timer in page["timers"]],
                         "next_cursor": page["next_cursor"]}
            elif command == "quick_record_timer_reconcile":
                timer_id = _canonical_uuid(request.get("timer_id"))
                with self.journal._db() as db:
                    timer = self.state._timer(db, timer_id, self.session.account_id, self.session.epoch)
                if timer is None or timer["status"] == "running":
                    raise ValueError("timer has no frozen stop to reconcile")
                reply = self._submit_timer(timer)
            else:
                reply = self._stop_timer(request)
            self.session.guard(mutating=not readonly)
            return reply | self.scope()

    def _select(self, identity, digest):
        choice = next((item for item in self.choices.list() if item["id"] == identity), None)
        if choice is None or choice["digest"] != digest:
            raise ValueError("quick-record choice changed; refresh before recording")
        return choice

    def _record(self, request, *, prepared=None, timer_id=None):
        action_id = _canonical_uuid(request.get("action_id"))
        identity = _canonical_uuid(request.get("definition_id"))
        digest = _digest(request.get("choice_digest"))
        comment = _note(request.get("comment"))
        start = _time(request.get("start_time"))
        end = None if request.get("end_time") is None else _time(request["end_time"])
        if end is not None and end <= start:
            raise ValueError("quick-record end must be after its start")
        frozen = {"definition_id": identity, "choice_digest": digest, "comment": comment,
                  "start_time": start.isoformat(), "end_time": end.isoformat() if end else None}
        if timer_id is not None:
            frozen["timer_id"] = _canonical_uuid(timer_id)
        prior = self.journal.get(action_id, account_id=self.session.account_id, epoch=self.session.epoch)
        if prior is None and prepared is None:
            selected = self._select(identity, digest)
            if (selected["kind"] == "duration") != (end is not None):
                raise ValueError("quick-record times do not match the selected track kind")
            prepared = self.choices.prepare(identity, expected_digest=digest)

        def build(snapshot):
            return build_quick_record(prepared, action_id=action_id, started_at=_time(snapshot["start_time"]),
                ended_at=_time(snapshot["end_time"]) if snapshot["end_time"] else None, comment=snapshot["comment"])

        return self.actor.record(action_id=action_id, request=frozen, build_payload=build)

    def _start_timer(self, request):
        identity = _canonical_uuid(request.get("definition_id"))
        timer_id = _canonical_uuid(request.get("timer_id"))
        digest, start = _digest(request.get("choice_digest")), _time(request.get("start_time"))
        with self.journal._db() as db:
            prior = self.state._timer(db, timer_id, self.session.account_id, self.session.epoch)
        if prior is not None:
            if prior["choice"]["id"] != identity or prior["choice"]["digest"] != digest:
                raise ValueError("timer choice changed; preserve the original timer")
            return {"ok": True, "timer": timer_view(prior)}
        if self._select(identity, digest)["kind"] != "duration":
            raise ValueError("timers require a duration track")
        prepared = self.choices.prepare(identity, expected_digest=digest)
        choice = {key: prepared[key] for key in ("id", "name", "kind", "digest", "data_type", "tags")}
        timer = self.session.commit(lambda: self.state.start_timer(timer_id=timer_id,
            account_id=self.session.account_id, epoch=self.session.epoch, choice=choice, started_at=start))
        return {"ok": True, "timer": timer_view(timer)}

    def _stop_timer(self, request):
        timer_id = _canonical_uuid(request.get("timer_id"))
        end, note = _time(request.get("end_time")), _note(request.get("comment"))
        timer = self.session.commit(lambda: self.state.stop_timer(timer_id, account_id=self.session.account_id,
            epoch=self.session.epoch, ended_at=end, comment=note))
        return self._submit_timer(timer)

    def _submit_timer(self, timer):
        timer_id = timer["timer_id"]
        prepared = timer["choice"]
        action = self._record({"action_id": timer["action_id"], "definition_id": prepared["id"],
            "choice_digest": prepared["digest"], "comment": timer["comment"],
            "start_time": timer["started_at"], "end_time": timer["ended_at"]}, prepared=prepared, timer_id=timer_id)
        if action.status in {"verified", "excluded"}:
            timer = self.session.commit(lambda: self.state.finish_timer(timer_id,
                account_id=self.session.account_id, epoch=self.session.epoch))
        return action_view(action) | {"timer": timer_view(timer)}

    def _recent(self):
        page = self.journal.list_actions(account_id=self.session.account_id, operation="record", limit=20)
        recent = []
        with self.journal._db() as db:
            for original in page.actions:
                rows = db.execute("SELECT action_id,epoch FROM actions WHERE account_id=? AND operation='delete' "
                    "AND request_hash=? ORDER BY rowid DESC LIMIT 51", (self.session.account_id,
                    _hash(_json({"record_action_id": original.action_id})))).fetchall()
                if len(rows) > 50:
                    raise ValueError("quick-record Undo history exceeds its recovery bound")
                deletions = [self.journal._get(db, row["action_id"], self.session.account_id, row["epoch"]) for row in rows]
                verified = next((action for action in deletions if action.status == "verified"), None)
                latest = verified or (deletions[0] if deletions else None)
                recent.append(action_view(original) | {"undone": verified is not None,
                    "undo_action_id": latest.action_id if latest else None,
                    "undo_status": latest.status if latest else None,
                    "needs_reauthorization": original.epoch != self.session.epoch
                    and original.status in {"pending", "submitted"}})
        return recent
