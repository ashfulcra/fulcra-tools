"""Private account-scoped interactive preferences and durable timer state.

Storage does not grant account/epoch authority. The controller must hold the
current interactive session's mutation lease around every change. Legacy
unscoped favorites are never read, adopted, pruned or overwritten here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

from fulcra_common.v1 import _canonical_uuid, _destination

from .record_actions import ActionJournal, _hash, _json, _scope, _page_cursor, _cursor_after


def _instant(value):
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError("timer time must include a timezone")
    return value.astimezone(timezone.utc)


def _stored_time(value):
    try:
        return _instant(datetime.fromisoformat(value))
    except (ValueError, TypeError):
        raise ValueError("timer time is corrupt or unreadable") from None


def _choices(values):
    if not isinstance(values, list) or len(values) > 2048:
        raise ValueError("favorite changes require a bounded list of track UUIDs")
    for value in values:
        _canonical_uuid(value)
    if len(set(values)) != len(values):
        raise ValueError("favorite changes contain duplicate track UUIDs")
    return set(values)


def _choice(value):
    encoded = _json(value, limit=16384)
    if (set(value) != {"id", "name", "kind", "digest", "data_type", "tags"}
            or not isinstance(value["name"], str) or not value["name"].strip()
            or len(value["name"]) > 256 or value["kind"] != "duration"
            or not isinstance(value["digest"], str) or not 1 <= len(value["digest"]) <= 128):
        raise ValueError("timer choice is malformed")
    _canonical_uuid(value["id"])
    _destination(value["data_type"], "Event")
    if not isinstance(value["tags"], list) or len(value["tags"]) > 256:
        raise ValueError("timer choice tags are malformed")
    for tag in value["tags"]:
        _canonical_uuid(tag)
    return encoded


def _comment(value):
    _json({"comment": value}, limit=32 * 1024)
    if value is not None and (not isinstance(value, str) or len(value.encode()) > 16384):
        raise ValueError("timer comment exceeds its text limit")


class QuickRecordState:
    def __init__(self, journal: ActionJournal):
        self.journal = journal
        with journal._db(write=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            expected = {"quick_record_state_meta", "quick_record_preferences", "quick_record_timers"}
            if tables & expected:
                if not expected <= tables:
                    raise ValueError("interactive state schema is unreadable")
                versions = db.execute("SELECT version FROM quick_record_state_meta").fetchall()
                if len(versions) != 1 or versions[0][0] != 1:
                    raise ValueError("interactive state schema version is unsupported")
            else:
                db.execute("CREATE TABLE quick_record_state_meta(version INTEGER NOT NULL)")
                db.execute("INSERT INTO quick_record_state_meta VALUES (1)")
                db.execute("CREATE TABLE quick_record_preferences(account_id TEXT PRIMARY KEY, "
                           "revision INTEGER NOT NULL, choices_json TEXT NOT NULL)")
                db.execute("CREATE TABLE quick_record_timers(timer_id TEXT PRIMARY KEY, "
                           "account_id TEXT NOT NULL, epoch TEXT NOT NULL, choice_json TEXT NOT NULL, "
                           "choice_hash TEXT NOT NULL, started_at TEXT NOT NULL, stop_json TEXT NOT NULL, "
                           "stop_hash TEXT NOT NULL, status TEXT NOT NULL)")
                db.execute("CREATE INDEX quick_record_timers_by_account ON quick_record_timers(account_id)")

    def _favorites(self, db, account_id):
        row = db.execute("SELECT revision,choices_json FROM quick_record_preferences WHERE account_id=?",
                         (account_id,)).fetchone()
        if row is None:
            return {"revision": 0, "favorites": []}
        try:
            values = json.loads(row["choices_json"])
            if (type(row["revision"]) is not int or row["revision"] < 1
                    or sorted(_choices(values)) != values
                    or json.dumps(values) != row["choices_json"]):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("account favorites are corrupt or unreadable") from None
        return {"revision": row["revision"], "favorites": values}

    def favorites(self, account_id: str):
        _canonical_uuid(account_id)
        with self.journal._db() as db:
            return self._favorites(db, account_id)

    def change_favorites(self, account_id: str, *, expected_revision, add, remove):
        _canonical_uuid(account_id)
        if type(expected_revision) is not int or not 0 <= expected_revision < 2**63 - 1:
            raise ValueError("favorite revision is invalid")
        additions, removals = _choices(add), _choices(remove)
        if additions & removals:
            raise ValueError("the same favorite cannot be added and removed")
        with self.journal._db(write=True) as db:
            prior = self._favorites(db, account_id)
            if prior["revision"] != expected_revision:
                raise ValueError("favorite revision changed; refresh before editing")
            choices = (set(prior["favorites"]) | additions) - removals
            if len(choices) > 2048:
                raise ValueError("favorite count exceeds its limit")
            if choices == set(prior["favorites"]):
                return prior
            revision, values = prior["revision"] + 1, sorted(choices)
            db.execute("INSERT INTO quick_record_preferences VALUES (?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET revision=excluded.revision, choices_json=excluded.choices_json",
                (account_id, revision, json.dumps(values)))
            return {"revision": revision, "favorites": values}

    def _timer(self, db, timer_id, account_id, epoch=None):
        row = db.execute("SELECT * FROM quick_record_timers WHERE timer_id=?", (timer_id,)).fetchone()
        if row is None:
            return None
        if row["account_id"] != account_id or (epoch is not None and row["epoch"] != epoch):
            raise ValueError("timer belongs to a different account or consent epoch")
        try:
            _scope(timer_id, account_id, row["epoch"])
            choice, stop = json.loads(row["choice_json"]), json.loads(row["stop_json"])
            start = _stored_time(row["started_at"])
            if (_choice(choice) != row["choice_json"] or _hash(row["choice_json"]) != row["choice_hash"]
                    or _json(stop) != row["stop_json"] or _hash(row["stop_json"]) != row["stop_hash"]
                    or row["status"] not in {"running", "stopping", "verified", "excluded"}):
                raise ValueError
            if row["status"] == "running":
                if stop:
                    raise ValueError
            else:
                if set(stop) != {"action_id", "ended_at", "comment"}:
                    raise ValueError
                _canonical_uuid(stop["action_id"])
                _comment(stop["comment"])
                if _stored_time(stop["ended_at"]) <= start:
                    raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ValueError("timer is corrupt or unreadable") from None
        return {"timer_id": timer_id, "account_id": account_id, "epoch": row["epoch"],
                "choice": choice, "started_at": row["started_at"], "status": row["status"], **stop}

    def start_timer(self, *, timer_id, account_id, epoch, choice, started_at):
        _scope(timer_id, account_id, epoch)
        encoded = _choice(choice)
        start = _instant(started_at).isoformat()
        with self.journal._db(write=True) as db:
            prior = self._timer(db, timer_id, account_id, epoch)
            if prior is not None:
                if _choice(prior["choice"]) != encoded:
                    raise ValueError("timer choice changed; preserve the original timer")
                return prior
            db.execute("INSERT INTO quick_record_timers VALUES (?,?,?,?,?,?,?,?,?)",
                (timer_id, account_id, epoch, encoded, _hash(encoded), start, "{}", _hash("{}"), "running"))
            return self._timer(db, timer_id, account_id, epoch)

    def stop_timer(self, timer_id, *, account_id, epoch, ended_at, comment):
        _scope(timer_id, account_id, epoch)
        end = _instant(ended_at)
        _comment(comment)
        with self.journal._db(write=True) as db:
            prior = self._timer(db, timer_id, account_id, epoch)
            if prior is None:
                raise ValueError("timer is missing")
            if prior["status"] != "running":
                if prior["comment"] != comment:
                    raise ValueError("timer comment changed; preserve the original stop action")
                return prior
            if end <= _stored_time(prior["started_at"]):
                raise ValueError("timer end must be after its start")
            frozen = _json({"action_id": str(uuid4()), "ended_at": end.isoformat(), "comment": comment})
            db.execute("UPDATE quick_record_timers SET stop_json=?,stop_hash=?,status='stopping' WHERE timer_id=?",
                       (frozen, _hash(frozen), timer_id))
            return self._timer(db, timer_id, account_id, epoch)

    def finish_timer(self, timer_id, *, account_id, epoch):
        _scope(timer_id, account_id, epoch)
        with self.journal._db(write=True) as db:
            prior = self._timer(db, timer_id, account_id, epoch)
            if prior is None or prior["status"] == "running":
                raise ValueError("timer requires a verified or excluded stop action")
            # Reuse this transaction to avoid opening a second SQLite connection
            # while holding the timer write lock.
            action = self.journal._get(db, prior["action_id"], account_id, epoch)
            if (action is None or action.operation != "record" or action.status not in {"verified", "excluded"}
                    or action.request.get("timer_id") != timer_id):
                raise ValueError("timer requires its verified or excluded stop action")
            record = action.payload["record"]
            if ("data_type" in prior["choice"] and action.payload["data_type"] != prior["choice"]["data_type"]
                    or _stored_time(record.get("start_time")) != _stored_time(prior["started_at"])
                    or _stored_time(record.get("end_time")) != _stored_time(prior["ended_at"])):
                raise ValueError("timer stop action does not match its original target and times")
            db.execute("UPDATE quick_record_timers SET status=? WHERE timer_id=?", (action.status, timer_id))
            return self._timer(db, timer_id, account_id, epoch)

    def timers(self, account_id, *, include_finished=False):
        _canonical_uuid(account_id)
        if type(include_finished) is not bool:
            raise ValueError("timer read mode must be on or off")
        with self.journal._db() as db:
            self._classifications(db, account_id)
            selection = "" if include_finished else " AND status IN ('running','stopping')"
            rows = db.execute("SELECT timer_id FROM quick_record_timers WHERE account_id=?"
                              + selection + " ORDER BY rowid DESC LIMIT 1001", (account_id,)).fetchall()
            if len(rows) > 1000:
                raise ValueError("timer list exceeds its read bound; use paginated timer recovery")
            return [self._timer(db, row["timer_id"], account_id) for row in rows]

    def _classifications(self, db, account_id):
        if db.execute("SELECT 1 FROM quick_record_timers WHERE account_id=? "
                      "AND status NOT IN ('running','stopping','verified','excluded') LIMIT 1",
                      (account_id,)).fetchone():
            raise ValueError("timer classification is corrupt or unreadable")

    def timer_page(self, account_id, *, include_finished=True, limit=50, cursor=None):
        """Stable account/filter-bound history pages, never a lifetime cap."""
        _canonical_uuid(account_id)
        if type(include_finished) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("timer page mode or limit is invalid")
        scope = _hash(_json({"resource": "quick-record-timers-v1", "account_id": account_id,
                            "include_finished": include_finished}))
        after = _cursor_after(cursor, scope) if cursor is not None else None
        conditions, args = ["account_id=?"], [account_id]
        if not include_finished:
            conditions.append("status IN ('running','stopping')")
        with self.journal._db() as db:
            self._classifications(db, account_id)
            if after is not None:
                anchor = db.execute("SELECT rowid FROM quick_record_timers WHERE timer_id=? AND account_id=?",
                                    (after, account_id)).fetchone()
                if anchor is None:
                    raise ValueError("timer page anchor is unavailable; refresh")
                self._timer(db, after, account_id)
                conditions.append("rowid<?")
                args.append(anchor[0])
            rows = db.execute("SELECT timer_id,LENGTH(CAST(choice_json AS BLOB))+"
                "LENGTH(CAST(stop_json AS BLOB)) AS read_bytes FROM quick_record_timers WHERE "
                + " AND ".join(conditions) + " ORDER BY rowid DESC LIMIT ?", (*args, limit + 1)).fetchall()
            timers, used = [], 0
            for row in rows[:limit]:
                if used + row["read_bytes"] > 4 * 1024 * 1024:
                    break
                timers.append(self._timer(db, row["timer_id"], account_id))
                used += row["read_bytes"]
            if rows and not timers:
                raise ValueError("timer exceeds its page budget; inspect the original timer")
            next_cursor = _page_cursor(timers[-1]["timer_id"], scope) if len(rows) > len(timers) else None
            return {"timers": timers, "next_cursor": next_cursor}
