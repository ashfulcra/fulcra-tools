"""Private durable intents for interactive v1 records and native Undo.

This journal performs no HTTP or credential lookup. Callers establish account
and consent authority, capture before effects, and supply readback evidence
before marking verified. Payload builders receive a frozen request snapshot,
run once under a short SQL write transaction, and must be pure. The application
actor/UI is wired separately.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import stat

from fulcra_common.v1 import _canonical_uuid, _destination
from fulcra_common.json_values import snapshot_json

_MAX_JSON = 1024 * 1024
_MAX_EVIDENCE = 64 * 1024
_MAX_PAGE_BYTES = 4 * 1024 * 1024
_STATUSES = {"pending", "submitted", "verified", "excluded", "cancelled"}
_TERMINAL = {"verified", "excluded", "cancelled"}


def _json(value, *, limit=_MAX_JSON) -> str:
    if not isinstance(value, dict):
        raise ValueError("record action requires a JSON object")
    try:
        text = json.dumps(snapshot_json(value), sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("record action requires finite JSON") from None
    if len(text.encode()) > limit:
        raise ValueError("record action JSON exceeds its size limit")
    return text


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _scope(action_id, account_id, epoch):
    _canonical_uuid(action_id)
    _canonical_uuid(account_id)
    if not isinstance(epoch, str) or not epoch:
        raise ValueError("record action requires a captured consent epoch")


def _payload(operation: str, value) -> str:
    text = _json(value)
    _destination(value.get("data_type"))
    if operation == "record":
        if set(value) != {"data_type", "record"} or not isinstance(value["record"], dict):
            raise ValueError("record action payload is malformed")
        _canonical_uuid(value["record"].get("id"))
    elif operation == "delete":
        if set(value) != {"data_type", "record_id"}:
            raise ValueError("delete action payload is malformed")
        _canonical_uuid(value["record_id"])
    else:
        raise ValueError("record action operation is unsupported")
    return text


@dataclass(frozen=True)
class RecordAction:
    action_id: str
    account_id: str
    epoch: str
    operation: str
    request: dict
    payload: dict
    status: str
    evidence: dict
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ActionPage:
    actions: tuple[RecordAction, ...]
    next_cursor: str | None


def _page_cursor(action_id: str, scope: str) -> str:
    value = _json({"after": action_id, "scope": scope})
    return "actions-v1." + base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _cursor_after(cursor: str, scope: str) -> str:
    try:
        if (not isinstance(cursor, str) or len(cursor) > 2048
                or not cursor.startswith("actions-v1.")):
            raise ValueError
        encoded = cursor[len("actions-v1."):]
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", encoded):
            raise ValueError
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
        value = json.loads(raw)
        if (not isinstance(value, dict) or set(value) != {"after", "scope"}
                or value["scope"] != scope or _json(value) != raw):
            raise ValueError
        return _canonical_uuid(value["after"])
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise ValueError("record action page cursor changed or is unreadable; refresh") from None


class ActionJournal:
    """Global action UUIDs cannot become new intents after an account switch.

    Retain this private SQLite file through restart/cutover. A submitted receipt
    is distinct from verified readback. Status updates append an audit event and
    compare the expected prior state. Terminal proof cannot be replaced. Epoch
    recovery/cancellation authorization and remote verification belong to the
    caller; this storage helper never restores or deletes a remote record.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("record action journal must be a private owned regular file")
        finally:
            os.close(fd)
        with self._db(write=True) as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                if not {"action_journal_meta", "actions", "action_events"} <= tables:
                    raise ValueError("record action journal schema is unreadable")
                versions = db.execute("SELECT version FROM action_journal_meta").fetchall()
                if len(versions) != 1 or versions[0][0] != 1:
                    raise ValueError("record action journal schema version is unsupported")
            else:
                db.execute("CREATE TABLE action_journal_meta (version INTEGER NOT NULL)")
                db.execute("INSERT INTO action_journal_meta VALUES (1)")
                db.execute("""CREATE TABLE actions (
                    action_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, epoch TEXT NOT NULL,
                    operation TEXT NOT NULL, request_json TEXT NOT NULL, request_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
                db.execute("""CREATE TABLE action_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    action_id TEXT NOT NULL REFERENCES actions(action_id),
                    status TEXT NOT NULL, evidence_json TEXT NOT NULL, at TEXT NOT NULL)""")
                db.execute("CREATE INDEX action_events_by_action ON action_events(action_id,sequence)")
            # SQLite's account index retains rowid ordering within each account.
            # Reads page by stable insertion order, independent of clock changes.
            db.execute("CREATE INDEX IF NOT EXISTS actions_by_account ON actions(account_id)")

    @contextmanager
    def _db(self, *, write=False):
        fd = os.open(self.path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("record action journal must remain a private owned regular file")
        finally:
            os.close(fd)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _get(self, db, action_id, account_id, epoch) -> RecordAction | None:
        row = db.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            return None
        if row["account_id"] != account_id or row["epoch"] != epoch:
            raise ValueError("record action belongs to a different account or consent epoch")
        try:
            request, payload, evidence = (json.loads(row[k]) for k in ("request_json", "payload_json", "evidence_json"))
            if (_hash(row["request_json"]) != row["request_hash"]
                    or _hash(row["payload_json"]) != row["payload_hash"]
                    or _json(request) != row["request_json"]
                    or _payload(row["operation"], payload) != row["payload_json"]
                    or _json(evidence, limit=_MAX_EVIDENCE) != row["evidence_json"]
                    or row["status"] not in _STATUSES):
                raise ValueError
            latest = db.execute("SELECT status,evidence_json,at FROM action_events WHERE action_id=? "
                                "ORDER BY sequence DESC LIMIT 1", (action_id,)).fetchone()
            if (latest is None or latest["status"] != row["status"]
                    or latest["evidence_json"] != row["evidence_json"] or latest["at"] != row["updated_at"]):
                raise ValueError
            for key in ("created_at", "updated_at"):
                instant = datetime.fromisoformat(row[key])
                if instant.tzinfo is None or instant.utcoffset() is None:
                    raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ValueError("record action is corrupt or unreadable") from None
        return RecordAction(row["action_id"], account_id, epoch, row["operation"],
            request, payload, row["status"], evidence, row["created_at"], row["updated_at"])

    def get(self, action_id: str, *, account_id: str, epoch: str) -> RecordAction | None:
        _scope(action_id, account_id, epoch)
        with self._db() as db:
            return self._get(db, action_id, account_id, epoch)

    def inspect(self, action_id: str, *, account_id: str) -> RecordAction | None:
        """Read an owned old epoch without authorizing replay or advancement."""
        _canonical_uuid(action_id)
        _canonical_uuid(account_id)
        with self._db() as db:
            row = db.execute("SELECT epoch FROM actions WHERE action_id=? AND account_id=?",
                             (action_id, account_id)).fetchone()
            if row is None:
                return None
            _scope(action_id, account_id, row["epoch"])
            return self._get(db, action_id, account_id, row["epoch"])

    def list_actions(self, *, account_id: str, epoch: str | None = None,
                     statuses=None, operation: str | None = None,
                     limit: int = 50, cursor: str | None = None) -> ActionPage:
        """Bounded, newest-created first recovery reads for one account.

        A cursor binds account and filters, not mutation authority. New captures
        appear on refresh; a cursor's anchor remains usable after status changes.
        Stored JSON has a 4-MiB page budget in addition to the 100-row cap.
        Corrupt selected evidence fails the whole page, never a partial success.
        """
        _canonical_uuid(account_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("record action page limit must be between 1 and 100")
        if epoch is not None and (not isinstance(epoch, str) or not epoch or len(epoch) > 256):
            raise ValueError("record action epoch filter is invalid")
        if operation is not None and (not isinstance(operation, str) or operation not in {"record", "delete"}):
            raise ValueError("record action operation filter is invalid")
        if statuses is not None and (not isinstance(statuses, (list, tuple, set, frozenset)) or not statuses
                or any(not isinstance(status, str) or status not in _STATUSES for status in statuses)
                or len(set(statuses)) != len(statuses)):
            raise ValueError("record action status filter is invalid")
        if statuses is not None:
            statuses = sorted(statuses)
        scope = _hash(_json({"account_id": account_id, "epoch": epoch,
                            "operation": operation, "statuses": statuses}))
        after = _cursor_after(cursor, scope) if cursor is not None else None
        conditions, args = ["account_id=?"], [account_id]
        if statuses is not None:
            conditions.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
        if epoch is not None:
            conditions.append("epoch=?")
            args.append(epoch)
        if operation is not None:
            conditions.append("operation=?")
            args.append(operation)
        with self._db() as db:
            # Classification corruption must not disappear behind a status or
            # operation filter and masquerade as an empty unfinished-work view.
            if db.execute("SELECT 1 FROM actions WHERE account_id=? AND "
                    "(status NOT IN ('pending','submitted','verified','excluded','cancelled') "
                    "OR operation NOT IN ('record','delete')) LIMIT 1", (account_id,)).fetchone():
                raise ValueError("record action classification is corrupt or unreadable")
            if after is not None:
                anchor = db.execute("SELECT rowid AS ordinal,epoch FROM actions WHERE action_id=? AND account_id=?",
                                    (after, account_id)).fetchone()
                if anchor is None:
                    raise ValueError("record action page cursor anchor is unavailable; refresh")
                _scope(after, account_id, anchor["epoch"])
                self._get(db, after, account_id, anchor["epoch"])
                conditions.append("rowid<?")
                args.append(anchor["ordinal"])
            rows = db.execute("SELECT action_id,epoch, "
                "LENGTH(CAST(request_json AS BLOB))+LENGTH(CAST(payload_json AS BLOB))+"
                "LENGTH(CAST(evidence_json AS BLOB)) AS read_bytes FROM actions WHERE "
                + " AND ".join(conditions) + " ORDER BY rowid DESC LIMIT ?", (*args, limit + 1)).fetchall()
            actions, used = [], 0
            for row in rows[:limit]:
                if used + row["read_bytes"] > _MAX_PAGE_BYTES:
                    break
                _scope(row["action_id"], account_id, row["epoch"])
                actions.append(self._get(db, row["action_id"], account_id, row["epoch"]))
                used += row["read_bytes"]
            if rows and not actions:
                raise ValueError("record action exceeds the recovery page budget; inspect directly")
            next_cursor = _page_cursor(actions[-1].action_id, scope) if len(rows) > len(actions) else None
            return ActionPage(tuple(actions), next_cursor)

    def capture(self, *, action_id: str, account_id: str, epoch: str, operation: str,
                request: dict, build_payload) -> RecordAction:
        _scope(action_id, account_id, epoch)
        if operation not in {"record", "delete"}:
            raise ValueError("record action operation is unsupported")
        request_json = _json(request)
        with self._db(write=True) as db:
            prior = self._get(db, action_id, account_id, epoch)
            if prior is not None:
                if prior.operation != operation or _json(prior.request) != request_json:
                    raise ValueError("record action request changed; preserve the original intent")
                return prior
            payload_json = _payload(operation, build_payload(json.loads(request_json)))
            now = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (action_id, account_id, epoch, operation, request_json, _hash(request_json),
                 payload_json, _hash(payload_json), "pending", "{}", now, now))
            db.execute("INSERT INTO action_events(action_id,status,evidence_json,at) VALUES (?,'pending','{}',?)",
                       (action_id, now))
            return self._get(db, action_id, account_id, epoch)

    def advance(self, action_id: str, *, account_id: str, epoch: str,
                expected_status: str, status: str, evidence: dict) -> RecordAction:
        _scope(action_id, account_id, epoch)
        evidence_json = _json(evidence, limit=_MAX_EVIDENCE)
        if status not in _STATUSES or expected_status not in _STATUSES:
            raise ValueError("record action status is unsupported")
        with self._db(write=True) as db:
            prior = self._get(db, action_id, account_id, epoch)
            if prior is None:
                raise ValueError("record action is missing")
            if prior.status == status and _json(prior.evidence, limit=_MAX_EVIDENCE) == evidence_json:
                return prior
            if (prior.status != expected_status or prior.status in _TERMINAL
                    or (prior.status == "submitted" and status == "pending")
                    or (prior.operation == "delete" and status == "excluded")):
                raise ValueError("record action state changed; reconcile the original intent")
            now = datetime.now(timezone.utc).isoformat()
            db.execute("UPDATE actions SET status=?,evidence_json=?,updated_at=? WHERE action_id=?",
                       (status, evidence_json, now, action_id))
            db.execute("INSERT INTO action_events(action_id,status,evidence_json,at) VALUES (?,?,?,?)",
                       (action_id, status, evidence_json, now))
            return self._get(db, action_id, account_id, epoch)

    def history(self, action_id: str, *, account_id: str, epoch: str) -> list[dict]:
        _scope(action_id, account_id, epoch)
        with self._db() as db:
            if self._get(db, action_id, account_id, epoch) is None:
                raise ValueError("record action is missing")
            rows = db.execute("SELECT status,evidence_json,at FROM action_events WHERE action_id=? "
                              "ORDER BY sequence LIMIT 1001", (action_id,)).fetchall()
            if not rows or len(rows) > 1000:
                raise ValueError("record action audit is missing or exceeds the read bound")
            try:
                result = [{"status": r["status"], "evidence": json.loads(r["evidence_json"]), "at": r["at"]}
                          for r in rows]
                if any(r["status"] not in _STATUSES or not _json(r["evidence"], limit=_MAX_EVIDENCE) for r in result):
                    raise ValueError
            except (ValueError, TypeError):
                raise ValueError("record action audit is corrupt or unreadable") from None
            return result
