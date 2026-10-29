"""Owned v1 destinations with schema proof and durable creation intents.

Resolution has no cross-account cache. A lost creation response remains pending
in the journal until catalog/schema readback identifies the resulting type;
an empty listing on the next run never authorizes another creation attempt.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from dataclasses import dataclass, field
from decimal import Decimal, DecimalException
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from fulcra_api.core import FulcraAPI
from fulcra_api.credentials import FulcraCredentials
from jsonschema import Draft202012Validator, SchemaError

from .client import BaseFulcraClient
from .v1 import (_RESERVED, _destination, _json_equal, _canonical_uuid,
                 _unique_record_keys, _invalid_record_constant)
from .json_values import snapshot_json
from .record_observations import (
    EvidenceSink, ObservationContext, RequestBudget, RequestGuard, SchemaObservation,
    _PARTIAL_HEADERS, _canonical, _capture_response, _hooks, _now, _producer_scope, _retain,
)


def tag_create_name(label: str) -> str:
    """Keep valid names; derive a stable short tag for longer source labels.

    The API accepts at most 30 characters. A label's original text remains in
    its record; this helper only chooses the name of a newly created tag.
    Slashes cannot safely round-trip through the name lookup path.
    """
    if (not isinstance(label, str) or not label.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in label)):
        raise ValueError("tag label must be nonempty text without control characters")
    try:
        encoded = label.encode("utf-8")
    except UnicodeError:
        raise ValueError("tag label must contain valid Unicode") from None
    if len(label) <= 30 and "/" not in label:
        return label
    prefix = re.sub(r"[^a-z0-9]+", "-", label.lower())[:9].strip("-") or "source"
    return prefix + "-" + hashlib.sha256(encoded).hexdigest()[:20]


@dataclass(frozen=True)
class DataTypeSpec:
    key: str
    base_type: str
    name: str
    description: str
    fields: dict = field(default_factory=dict, repr=False, compare=False)
    required_fields: tuple[str, ...] = ()
    aggregation: str | None = None
    unit: str | None = None
    scale: dict[str, int] | None = field(default=None, repr=False, compare=False)
    value_map: dict[str, str] | None = field(default=None, repr=False, compare=False)
    _schema_json: str = field(init=False, repr=False)
    _metric_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.base_type not in {"Event", "Metric"}:
            raise ValueError("v1 destinations require Event or Metric")
        if any(not isinstance(s, str) or not s.strip() for s in (self.key, self.name, self.description)):
            raise ValueError("datatype key, name and description are required")
        snapshot_json([self.key, self.name, self.description])
        if not isinstance(self.fields, dict) or _RESERVED.intersection(self.fields):
            raise ValueError("datatype enrichment cannot redefine base fields")
        if not set(self.required_fields) <= set(self.fields):
            raise ValueError("required datatype fields must be declared")
        if self.aggregation not in {None, "discrete", "cumulative"}:
            raise ValueError("unsupported metric aggregation")
        if self.unit is not None and not isinstance(self.unit, str):
            raise ValueError("metric unit must be a string")
        if self.base_type == "Event" and any(v is not None for v in (self.aggregation, self.unit, self.scale, self.value_map)):
            raise ValueError("Event destinations cannot declare metric parameters")
        if self.scale is not None:
            if (not isinstance(self.scale, dict) or set(self.scale) != {"min", "max", "step"}
                    or any(type(v) is not int for v in self.scale.values())
                    or self.scale["min"] > self.scale["max"] or self.scale["step"] <= 0):
                raise ValueError("metric scale requires integer min/max and positive step")
        if self.value_map is not None and (not isinstance(self.value_map, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.value_map.items())):
            raise ValueError("metric value-map must contain string labels")
        fragment = {"type": "object", "properties": self.fields}
        if self.required_fields:
            fragment["required"] = list(self.required_fields)
        try:
            text = json.dumps(snapshot_json(fragment), sort_keys=True, allow_nan=False)
            Draft202012Validator.check_schema(json.loads(text))
        except (SchemaError, ValueError, TypeError):
            raise ValueError("invalid datatype field schema") from None
        object.__setattr__(self, "_schema_json", text)
        metric = {"aggregation": self.aggregation or "discrete"}
        for key in ("unit", "scale", "value_map"):
            value = getattr(self, key)
            if value is not None:
                metric[key] = value
        object.__setattr__(self, "_metric_json", json.dumps(snapshot_json(metric), sort_keys=True))

    def create_body(self) -> dict:
        record_spec = {"type": self.base_type.lower()}
        # The API rejects an empty fragment. Base-only types inherit their
        # base schema by omitting this optional input property entirely.
        if json.loads(self._schema_json)["properties"]:
            record_spec["schema"] = self._schema_json
        if self.base_type == "Metric":
            record_spec.update(json.loads(self._metric_json))
        return {"name": self.name, "description": self.description,
                "record_spec": record_spec,
                "metadata": {"fulcra_tools": {"key": self.key, "schema_version": 1}}}

    @property
    def fingerprint(self) -> str:
        body = self.create_body()
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ResolvedDataType:
    data_type: str
    account_id: str
    schema: dict
    schema_hash: str


@dataclass(frozen=True)
class CreationReservation:
    nonce: str
    account_id: str
    key: str
    fingerprint: str
    base_type: str


class CreationJournal:
    """Private SQLite intents: reserve before creation, reconcile after it."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self.path = Path(path)
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._check_file(create=not read_only)
        if not read_only:
            with self._connect(initialize=True) as db:
                db.execute("BEGIN IMMEDIATE")
                version = self._format(db)
                if version == 0:
                    db.execute("CREATE TABLE IF NOT EXISTS creations (account TEXT, key TEXT, fingerprint TEXT NOT NULL, status TEXT NOT NULL, destination TEXT, PRIMARY KEY(account,key))")
                    db.execute("CREATE TABLE IF NOT EXISTS tag_creations (account TEXT, name TEXT, status TEXT NOT NULL, destination TEXT, PRIMARY KEY(account,name))")
                    db.execute("CREATE TABLE creation_reservations (nonce TEXT PRIMARY KEY, account TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, base_type TEXT NOT NULL, active INTEGER NOT NULL CHECK(active IN (0,1)), has_receipt INTEGER NOT NULL CHECK(has_receipt IN (0,1)), integrity_hold INTEGER NOT NULL CHECK(integrity_hold IN (0,1)))")
                    db.execute("CREATE UNIQUE INDEX creation_active_reservation ON creation_reservations(account,key) WHERE active=1")
                    db.execute("CREATE TABLE creation_receipts (nonce TEXT NOT NULL, destination TEXT NOT NULL, PRIMARY KEY(nonce,destination))")
                    db.execute("CREATE TABLE creation_bindings (account TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL, kind TEXT NOT NULL, nonce TEXT, PRIMARY KEY(account,key))")
                    db.execute("INSERT INTO creation_bindings SELECT account,key,fingerprint,'legacy',NULL FROM creations")
                    db.execute("PRAGMA user_version=1")
                self._format(db)

    def _check_file(self, *, create: bool) -> None:
        flags = os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
        fd = os.open(self.path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("creation journal must be a private owned regular file")
        finally:
            os.close(fd)

    def _connect(self, *, initialize=False):
        self._check_file(create=False)
        mode = "ro" if self.read_only else "rw"
        db = sqlite3.connect("file:" + quote(str(self.path.absolute()), safe="/") + "?mode=" + mode, uri=True, timeout=5)
        try:
            if not initialize:
                self._format(db)
        except Exception:
            db.close()
            raise
        return db

    @staticmethod
    def _format(db) -> int:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        protocol = {"creation_reservations", "creation_receipts", "creation_bindings"}
        if version == 0 and not tables.intersection(protocol):
            return 0  # Five-column legacy intents remain readable without DDL.
        expected = {
            "creations": {"account", "key", "fingerprint", "status", "destination"},
            "tag_creations": {"account", "name", "status", "destination"},
            "creation_reservations": {"nonce", "account", "key", "fingerprint", "base_type", "active", "has_receipt", "integrity_hold"},
            "creation_receipts": {"nonce", "destination"},
            "creation_bindings": {"account", "key", "fingerprint", "kind", "nonce"},
        }
        if version != 1 or not set(expected) <= tables:
            raise ValueError("datatype journal metadata is unreadable; preserve the original evidence")
        for name, columns in expected.items():
            actual = {row[1] for row in db.execute(f"PRAGMA table_info({name})")}
            if actual != columns:
                raise ValueError("datatype journal metadata is unreadable; preserve the original evidence")
        index = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='creation_active_reservation'").fetchone()
        if not index or "UNIQUE" not in index[0].upper() or "WHERE active=1" not in index[0]:
            raise ValueError("datatype journal reservation index is unreadable; preserve the original evidence")
        return 1

    @staticmethod
    def _audit_row(db, account, spec, row) -> None:
        if CreationJournal._format(db) == 0:
            if row and row[1] == "submitted":
                raise ValueError("datatype receipt metadata is missing; preserve the original evidence")
            return
        history = db.execute("SELECT nonce,fingerprint,base_type,active,has_receipt,integrity_hold FROM creation_reservations WHERE account=? AND key=?", (account, spec.key)).fetchall()
        binding = db.execute("SELECT fingerprint,kind,nonce FROM creation_bindings WHERE account=? AND key=?", (account, spec.key)).fetchone()
        if row is None:
            if history or binding:
                raise ValueError("datatype original intent is missing; preserve reservation history")
            return
        if binding is None or binding[0] != row[0]:
            raise ValueError("datatype original binding provenance is missing or changed")
        active = [item for item in history if item[3] == 1]
        if binding[1] in {"legacy", "adopted"}:
            if binding[2] is not None or active or row[1] == "submitted":
                raise ValueError("datatype adoption provenance is unreadable; preserve the original evidence")
            return
        if (binding[1] != "created" or len(active) != 1 or active[0][0] != binding[2]):
            raise ValueError("datatype original reservation is missing or ambiguous; preserve the original evidence")
        nonce, fingerprint, base_type, _, seen, hold = active[0]
        _canonical_uuid(nonce)
        if (fingerprint != row[0] or base_type not in {"Event", "Metric"}
                or (row[0] == spec.fingerprint and base_type != spec.base_type)
                or seen not in (0, 1) or hold != 0):
            raise ValueError("datatype receipt metadata changed; preserve the original evidence")
        ids = tuple(item[0] for item in db.execute("SELECT destination FROM creation_receipts WHERE nonce=? ORDER BY destination", (nonce,)))
        for destination in ids:
            _destination(destination, base_type)
        if seen:
            if row[1] not in {"submitted", "resolved"} or ids != (row[2],):
                raise ValueError("datatype creation receipt audit is missing or conflicting")
        elif ids or row[1] == "submitted":
            raise ValueError("datatype creation receipt audit is inconsistent")

    def begin_tag(self, account: str, name: str) -> bool:
        """Reserve a tag name across all plugins before its one creation POST."""
        with self._connect() as db:
            return bool(db.execute("INSERT OR IGNORE INTO tag_creations VALUES (?,?,'pending',NULL)",
                                   (account, name)).rowcount)

    def read_tag(self, account: str, name: str) -> tuple | None:
        with self._connect() as db:
            row = db.execute("SELECT status,destination FROM tag_creations WHERE account=? AND name=?",
                             (account, name)).fetchone()
        if row:
            if row[0] == "resolved":
                _canonical_uuid(row[1])
            elif row[0] != "pending" or row[1] is not None:
                raise ValueError("tag binding is unreadable; reconcile the original intent")
        return row

    def resolved_tag(self, account: str, name: str, destination: str) -> None:
        with self._connect() as db:
            inserted = db.execute("INSERT OR IGNORE INTO tag_creations VALUES (?,?,'resolved',?)",
                                  (account, name, destination)).rowcount
            if not inserted:
                updated = db.execute("UPDATE tag_creations SET status='resolved',destination=? "
                                     "WHERE account=? AND name=? AND (destination IS NULL OR destination=?)",
                                     (destination, account, name, destination)).rowcount
                if not updated:
                    raise ValueError("tag destination changed; reconcile the existing mapping")

    def read(self, account: str, spec: DataTypeSpec) -> tuple | None:
        with self._connect() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT fingerprint,status,destination FROM creations WHERE account=? AND key=?", (account, spec.key)).fetchone()
            self._validate_row(row, spec)
            self._audit_row(db, account, spec, row)
            return row

    @staticmethod
    def _validate_row(row, spec):
        if row and row[1] == "conflict":
            raise ValueError("datatype creation receipts conflict; preserve the original evidence")
        if row and row[0] != spec.fingerprint and row[1] != "rejected:422":
            raise ValueError("datatype creation schema/metadata changed; reconcile the prior intent")
        if row:
            if row[1] in {"resolved", "submitted"}:
                _destination(row[2], spec.base_type)
            elif row[1] not in {"pending", "rejected:422"} or row[2] is not None:
                raise ValueError("datatype creation intent is unreadable; reconcile the original binding")

    def begin(self, account: str, spec: DataTypeSpec) -> bool:
        return self.reserve(account, spec) is not None

    def reserve(self, account: str, spec: DataTypeSpec) -> CreationReservation | None:
        """Return the original nonce in the transaction reserving its one POST."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,status,destination FROM creations WHERE account=? AND key=?", (account, spec.key)).fetchone()
            self._validate_row(row, spec)
            self._audit_row(db, account, spec, row)
            if row and not (row[1] == "rejected:422" and row[0] != spec.fingerprint):
                return None
            db.execute("INSERT INTO creations VALUES (?,?,?,'pending',NULL) ON CONFLICT(account,key) DO UPDATE SET fingerprint=excluded.fingerprint,status='pending',destination=NULL", (account, spec.key, spec.fingerprint))
            db.execute("UPDATE creation_reservations SET active=0 WHERE account=? AND key=? AND active=1", (account, spec.key))
            reservation = CreationReservation(str(uuid4()), account, spec.key, spec.fingerprint, spec.base_type)
            db.execute("INSERT INTO creation_reservations VALUES (?,?,?,?,?,1,0,0)", (reservation.nonce, account, spec.key, spec.fingerprint, spec.base_type))
            db.execute("INSERT INTO creation_bindings VALUES (?,?,?,'created',?) ON CONFLICT(account,key) DO UPDATE SET fingerprint=excluded.fingerprint,kind=excluded.kind,nonce=excluded.nonce", (account, spec.key, spec.fingerprint, reservation.nonce))
        return reservation

    def reservation(self, account: str, spec: DataTypeSpec) -> CreationReservation | None:
        with self._connect() as db:
            if self._format(db) == 0:
                return None
            row = db.execute("SELECT nonce,fingerprint,base_type FROM creation_reservations WHERE account=? AND key=? AND active=1", (account, spec.key)).fetchone()
        if row is None:
            return None
        if row[1:] != (spec.fingerprint, spec.base_type):
            raise ValueError("datatype reservation changed; retain the original intent")
        _canonical_uuid(row[0])
        return CreationReservation(row[0], account, spec.key, row[1], row[2])

    @staticmethod
    def _reservation_active(db, reservation: CreationReservation) -> bool:
        if not isinstance(reservation, CreationReservation):
            raise ValueError("datatype receipt requires its original reservation")
        row = db.execute("SELECT account,key,fingerprint,base_type,active FROM creation_reservations WHERE nonce=?", (reservation.nonce,)).fetchone()
        if row is None or row[:4] != (reservation.account_id, reservation.key, reservation.fingerprint, reservation.base_type):
            raise ValueError("datatype receipt requires its original reservation")
        return row[4] == 1

    def receipts(self, reservation: CreationReservation) -> tuple[str, ...]:
        with self._connect() as db:
            self._reservation_active(db, reservation)
            rows = db.execute("SELECT destination FROM creation_receipts WHERE nonce=? ORDER BY destination", (reservation.nonce,)).fetchall()
        return tuple(_destination(row[0], reservation.base_type) for row in rows)

    def retain_receipt(self, reservation: CreationReservation, destination: str) -> None:
        """Save original-account evidence, independently of current authority."""
        if not isinstance(reservation, CreationReservation):
            raise ValueError("datatype receipt requires its original reservation")
        destination = _destination(destination, reservation.base_type)
        failure = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = self._reservation_active(db, reservation)
            row = db.execute("SELECT fingerprint,status,destination FROM creations WHERE account=? AND key=?", (reservation.account_id, reservation.key)).fetchone()
            if not active or row is None or row[0] != reservation.fingerprint:
                failure = "datatype original reservation changed; receipt retained without authority"
            else:
                try:
                    self._validate_row(row, reservation)
                    self._audit_row(db, reservation.account_id, reservation, row)
                except ValueError:
                    failure = "datatype original intent or receipt audit is unreadable; receipt retained without authority"
            # This append is evidence only. A corrupt pre-existing audit must
            # remain held even if the new receipt happens to fill its hole.
            db.execute("INSERT OR IGNORE INTO creation_receipts VALUES (?,?)", (reservation.nonce, destination))
            db.execute("UPDATE creation_reservations SET has_receipt=1 WHERE nonce=?", (reservation.nonce,))
            if row and row[0] == reservation.fingerprint and row[2] is not None:
                try:
                    prior_destination = _destination(row[2], reservation.base_type)
                except ValueError:
                    pass
                else:
                    db.execute("INSERT OR IGNORE INTO creation_receipts VALUES (?,?)", (reservation.nonce, prior_destination))
            ids = {item[0] for item in db.execute("SELECT destination FROM creation_receipts WHERE nonce=?", (reservation.nonce,))}
            if failure is None:
                if len(ids) > 1:
                    db.execute("UPDATE creations SET status='conflict' WHERE account=? AND key=?", (reservation.account_id, reservation.key))
                    failure = "datatype creation receipts conflict; preserve the original evidence"
                elif row[1:] == ("pending", None):
                    db.execute("UPDATE creations SET status='submitted',destination=? WHERE account=? AND key=?", (destination, reservation.account_id, reservation.key))
                elif row[1] not in {"resolved", "submitted"} or row[2] != destination:
                    failure = "datatype original intent is unreadable; receipt retained without authority"
            if failure:
                db.execute("UPDATE creation_reservations SET integrity_hold=1 WHERE nonce=?", (reservation.nonce,))
        if failure:
            raise ValueError(failure)

    def _validation_rejected(self, account: str, spec: DataTypeSpec, *, reservation: CreationReservation | None = None) -> None:
        """Record a definitive HTTP 422, never a timeout or unknown outcome."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if reservation is not None:
                if ((reservation.account_id, reservation.key, reservation.fingerprint) != (account, spec.key, spec.fingerprint)
                        or not self._reservation_active(db, reservation)):
                    return
            row = db.execute("SELECT fingerprint,status,destination FROM creations WHERE account=? AND key=?", (account, spec.key)).fetchone()
            self._validate_row(row, spec)
            self._audit_row(db, account, spec, row)
            db.execute("UPDATE creations SET status='rejected:422' WHERE account=? AND key=? "
                       "AND fingerprint=? AND status='pending' AND destination IS NULL",
                       (account, spec.key, spec.fingerprint))

    def resolved(self, account: str, spec: DataTypeSpec, destination: str, *, reservation: CreationReservation | None = None) -> None:
        destination = _destination(destination, spec.base_type)
        prior = self.read(account, spec)
        if prior and prior[2] is not None and prior[2] != destination:
            raise ValueError("datatype destination changed; reconcile the existing mapping")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT fingerprint,status,destination FROM creations WHERE account=? AND key=?", (account, spec.key)).fetchone()
            self._validate_row(current, spec)
            self._audit_row(db, account, spec, current)
            if reservation is not None:
                if ((reservation.account_id, reservation.key, reservation.fingerprint) != (account, spec.key, spec.fingerprint)
                        or not self._reservation_active(db, reservation) or current is None
                        or current[0] != reservation.fingerprint or current[1] == "rejected:422"):
                    raise ValueError("datatype original reservation changed; retain pending work")
            changed = db.execute(
                "INSERT INTO creations VALUES (?,?,?,'resolved',?) ON CONFLICT(account,key) DO UPDATE "
                "SET fingerprint=excluded.fingerprint,status='resolved',destination=excluded.destination "
                "WHERE (creations.fingerprint=excluded.fingerprint AND "
                "((creations.status='pending' AND creations.destination IS NULL) OR "
                "(creations.status IN ('resolved','submitted') AND creations.destination=excluded.destination))) "
                "OR (creations.status='rejected:422' AND creations.destination IS NULL)",
                (account, spec.key, spec.fingerprint, destination),
            ).rowcount
            if not changed:
                raise ValueError("datatype destination changed; reconcile the existing mapping")
            if current is None or current[0] != spec.fingerprint:
                db.execute("UPDATE creation_reservations SET active=0 WHERE account=? AND key=? AND active=1", (account, spec.key))
                db.execute("INSERT INTO creation_bindings VALUES (?,?,?,'adopted',NULL) ON CONFLICT(account,key) DO UPDATE SET fingerprint=excluded.fingerprint,kind=excluded.kind,nonce=NULL", (account, spec.key, spec.fingerprint))


def _account_headers(client: BaseFulcraClient, account_id: str, *, request_guard=None,
                     request_budget=None, observation_context=None, _capture=None) -> dict:
    observed = _hooks(request_guard, request_budget, observation_context)
    if observed:
        request_guard(kind="account-proof", context=observation_context)
    token = client.get_token()
    try:
        api = FulcraAPI(credentials=FulcraCredentials(access_token=token))
    except Exception:
        raise ValueError("cannot establish the authenticated account") from None
    if observed:
        request_budget.charge_request("account-proof")
        request_guard(kind="account-proof", context=observation_context)
    account_started = _now() if _capture is not None else None
    try:
        actual = api.get_fulcra_userid()
    except Exception:
        raise ValueError("cannot establish the authenticated account") from None
    if _capture is not None:
        _capture.account_received(actual, started_at=account_started)
    if actual != account_id:
        raise ValueError("authenticated account changed; resolve destinations again")
    return {"Authorization": f"Bearer {token}"}


def _proof_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or Decimal(text) != Decimal(str(value)):
        raise ValueError("datatype authority number changed during parsing")
    return value


def _proof_json(body: bytes, *, limit: int):
    """Lossless bounded parsing of an already buffered authority response."""
    try:
        if len(body) > limit:
            raise ValueError
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_record_keys,
                           parse_constant=_invalid_record_constant, parse_float=_proof_float)
        pending, nodes = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            nodes += 1
            if depth > 64 or nodes > 100000:
                raise ValueError
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return snapshot_json(value)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError, DecimalException):
        raise ValueError("datatype authority JSON is unreadable; retain the original binding") from None


def _read_schema(http, headers: dict, destination: str, account: str, *, timeout=None,
                 request_guard=None, request_budget=None, observation_context=None, _capture=None):
    observed = _hooks(request_guard, request_budget, observation_context)
    path = f"/data/v1/catalog/{destination}/v1/schema"
    options = {} if timeout is None else {"timeout": timeout}
    started_at = _now() if observed else None
    expected_origin = http.base_url if observed else None
    from ._historical_observed_transport import _closed, _send_prepared
    if observed and _closed(http):
        response = _send_prepared(http, method="GET", path=path,
            headers={**headers, "Accept-Encoding": "identity", "Cache-Control": "no-store"},
            kind="schema-get", context=observation_context, request_guard=request_guard,
            request_budget=request_budget, timeout=timeout)
    else:
        if observed:
            request_budget.charge_request("schema-get")
            request_guard(kind="schema-get", context=observation_context)
        response = http.get(path, headers={**headers, "Accept-Encoding": "identity", "Cache-Control": "no-store"},
                            follow_redirects=False, **options)
    response.raise_for_status()
    if observed:
        _capture.update(_capture_response(response, expected_origin, method="GET", path=path,
            query=[], context=observation_context, started_at=started_at))
    expected = http.base_url
    if (response.status_code != 200 or response.history or response.url.path != path or response.url.query
            or (response.url.scheme, response.url.host, response.url.port) != (expected.scheme, expected.host, expected.port)
            or any(name in response.headers for name in _PARTIAL_HEADERS)
            or response.headers.get("x-fulcra-userid", account) != account
            or response.headers.get("content-encoding", "identity").strip().lower() != "identity"):
        raise ValueError("datatype schema proof is partial or mismatched; retain the original binding")
    return _proof_json(response.content, limit=1024 * 1024)


def read_served_schema(*, client: BaseFulcraClient, account_id: str,
                       data_type: str, timeout=None) -> dict:
    """Read one strict full schema using an explicitly captured token account.

    Returns a bounded JSON object, not a record-schema or owned-binding proof.
    The 1 MiB/depth64/100000-value caps apply after buffered HTTP loading.
    """
    destination = _destination(data_type)
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("served schema requires an explicit account_id")
    headers = _account_headers(client, account_id)
    schema = _read_schema(client._client(), headers, destination, account_id, timeout=timeout)
    if not isinstance(schema, dict):
        raise ValueError("datatype served schema must be a JSON object")
    return schema


def _observe_schema_core(*, client, account_id, data_type, context, on_observation,
                         request_budget, request_guard, timeout=None, _capture=None):
    destination = _destination(data_type)
    if destination != data_type:
        raise ValueError("observed schema requires canonical destination")
    _producer_scope(context, account=account_id, destination=destination, identifiers=[], purpose={"schema-read"},
                    sink=on_observation, guard=request_guard, budget=request_budget)
    reservation = request_budget.reserve_events(1)
    request_guard(kind="schema-get", context=context)
    reservation.start()
    headers = _account_headers(client, account_id, request_guard=request_guard,
        request_budget=request_budget, observation_context=context, _capture=_capture)
    capture = {}
    schema = _read_schema(client._client(), headers, destination, account_id, timeout=timeout,
        request_guard=request_guard, request_budget=request_budget, observation_context=context, _capture=capture)
    if not isinstance(schema, dict):
        raise ValueError("datatype served schema must be a JSON object")
    observation = _retain(SchemaObservation, context, capture["started_at"], capture["received_at"],
        {"request": capture["request"], "response": capture["response"], "schema": schema,
         "schema_sha256": hashlib.sha256(_canonical(schema)).hexdigest(),
         "schema_hash_algorithm": "fulcra-exact-schema-json-v1",
         "local_validation": "bounded-strict-schema-response-validation"}, reservation, on_observation)
    if _capture is not None:
        _capture.schema_received(observation)
    return schema, observation


def observe_served_schema(*, client: BaseFulcraClient, account_id: str, data_type: str,
                          context: ObservationContext, on_observation: EvidenceSink,
                          request_budget: RequestBudget, request_guard: RequestGuard,
                          timeout: float | None = None) -> SchemaObservation:
    """Retain the actual strict served snapshot; no owned binding or coverage."""
    return _observe_schema_core(client=client, account_id=account_id, data_type=data_type,
        context=context, on_observation=on_observation, request_budget=request_budget,
        request_guard=request_guard, timeout=timeout)[1]


def _owned_types(http, headers: dict, account: str, *, request_guard=None,
                 request_budget=None, observation_context=None, _capture=None) -> list[dict]:
    """Read owned custom types from their authority, never the aggregate catalog.

    The aggregate can silently omit input-service failures or conversion errors.
    The input endpoint lists the authenticated caller's own types, without an
    owner override. Retired types remain visible so a name miss cannot silently
    create a replacement. Its normal response has no owner field; contradictory
    ownership, malformed entries or duplicate identities make the WHOLE read
    unknown, not a partial list that can authorize creation.
    """
    path = "/input/v1/data_type"
    observed = _hooks(request_guard, request_budget, observation_context)
    from ._historical_observed_transport import _closed, _send_prepared
    if observed and _closed(http):
        response = _send_prepared(http, method="GET", path=path, params={"include_deprecated": "true"},
            headers={**headers, "Accept-Encoding": "identity", "Cache-Control": "no-store"},
            kind="owned-list", context=observation_context, request_guard=request_guard,
            request_budget=request_budget)
    else:
        if observed:
            request_budget.charge_request("owned-list")
            request_guard(kind="owned-list", context=observation_context)
        response = http.get(path, params={"include_deprecated": "true"},
            headers={**headers, "Accept-Encoding": "identity", "Cache-Control": "no-store"}, follow_redirects=False)
    if observed:
        _capture_response(response, http.base_url, method="GET", path=path,
            query=[("include_deprecated", "true")], context=observation_context, started_at=_now())
    response.raise_for_status()
    expected = http.base_url
    if (response.status_code != 200 or response.history or response.url.path != path
            or (response.url.scheme, response.url.host, response.url.port) != (expected.scheme, expected.host, expected.port)
            or list(response.url.params.multi_items()) != [("include_deprecated", "true")]
            or any(name in response.headers for name in _PARTIAL_HEADERS)
            or response.headers.get("x-fulcra-userid", account) != account
            or response.headers.get("content-encoding", "identity").strip().lower() != "identity"):
        raise ValueError("owned datatype list is partial or redirected; no creation permitted")
    return _parse_owned_types(response.content, account=account,
        totals={name: response.headers.get(name) for name in ("x-total-count", "x-total")})


def _parse_owned_types(body, *, account, totals):
    """Shared full-list parser for actual reads and untrusted byte inspection."""
    rows = _proof_json(body, limit=16 * 1024 * 1024)
    if not isinstance(rows, list) or len(rows) > 4096:
        raise ValueError("owned datatype list is unreadable or exceeds its limit; no creation permitted")
    for header in ("x-total-count", "x-total"):
        total = totals.get(header)
        if total is not None and (not re.fullmatch(r"[0-9]{1,10}", total) or int(total) != len(rows)):
            raise ValueError("owned datatype total is incomplete; no creation permitted")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("owned datatype entry is unreadable; no creation permitted")
        destination = _destination(row.get("id"))
        if (destination in seen or any(key in row and row[key] != account for key in ("fulcra_userid", "userid"))
                or row.get("api_version") != "v1" or row.get("recordable") is not True
                or type(row.get("deprecated")) is not bool
                or not isinstance(row.get("name"), str) or not row["name"].strip()
                or not isinstance(row.get("record_spec"), dict)
                or row["record_spec"].get("type") != destination.split("/")[0].lower()):
            raise ValueError("owned datatype identity or metadata is unreadable; no creation permitted")
        seen.add(destination)
        result.append({**row, "id": destination, "fulcra_userid": account})
    return result


def _schema_contains(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and _schema_contains(actual[k], v) for k, v in expected.items())
    return _json_equal(actual, expected)


def _validate_type_schema(schema, spec: DataTypeSpec) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("datatype destination schema is malformed") from None
    expected_fields = json.loads(spec._schema_json)["properties"]
    if not isinstance(schema, dict) or not _schema_contains(schema.get("properties"), expected_fields):
        raise ValueError("datatype destination field schema is incompatible")
    supported = _RESERVED | set(expected_fields)
    if not set(schema.get("required", [])) <= supported:
        raise ValueError("datatype destination has unsupported required schema fields")
    if not set(spec.required_fields) <= set(schema.get("required", [])):
        raise ValueError("datatype destination required schema differs")


def _resolve_candidate(http, headers: dict, row: dict, spec: DataTypeSpec, account: str,
                       *, served_schema=None) -> ResolvedDataType:
    destination = _destination(row.get("id"), spec.base_type)
    record_spec = row.get("record_spec") or {}
    if row.get("deprecated") or record_spec.get("type") != spec.base_type.lower():
        raise ValueError("datatype destination is deprecated or has the wrong base type")
    if spec.base_type == "Metric":
        expected = spec.create_body()["record_spec"]
        if any(record_spec.get(k) != expected.get(k) for k in ("aggregation", "unit", "scale", "value_map")):
            raise ValueError("datatype metric schema is incompatible")
    if "schema" in record_spec:
        text = record_spec["schema"]
        if text is not None or json.loads(spec._schema_json)["properties"]:
            if not isinstance(text, str):
                raise ValueError("datatype input schema is unreadable")
            try:
                input_schema = _proof_json(text.encode("utf-8"), limit=1024 * 1024)
            except UnicodeError:
                raise ValueError("datatype input schema is unreadable") from None
            _validate_type_schema(input_schema, spec)
    if served_schema is None:
        schema = _read_schema(http, headers, destination, account)
    else:
        schema = served_schema
    _validate_type_schema(schema, spec)
    return ResolvedDataType(destination, account, schema,
                            hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest())


def _created_destination(http, headers, account, spec, journal, reservation) -> str:
    """Capture a known UUID before later proof, without following redirects."""
    path = f"/input/v1/data_type/{spec.base_type}"
    with http.stream("POST", path, json=spec.create_body(), follow_redirects=False,
                     headers={**headers, "Accept-Encoding": "identity", "Cache-Control": "no-store"}) as response:
        expected = http.base_url
        if (response.history or response.url.path != path or response.url.query
                or (response.url.scheme, response.url.host, response.url.port) != (expected.scheme, expected.host, expected.port)
                or any(name in response.headers for name in _PARTIAL_HEADERS)
                or response.headers.get("x-fulcra-userid", account) != account):
            raise ValueError("datatype creation response scope is unknown; retain the original intent")
        if response.status_code == 422:
            journal._validation_rejected(account, spec, reservation=reservation)
            response.raise_for_status()
        if response.status_code == 303:
            locations = response.headers.get_list("location")
            if len(locations) != 1 or len(locations[0]) > 4096:
                raise ValueError("datatype creation Location is unreadable; retain the original intent")
            location = response.url.join(locations[0])
            prefix = f"/input/v1/data_type/{spec.base_type}/"
            if ((location.scheme, location.host, location.port) != (expected.scheme, expected.host, expected.port)
                    or location.query or location.fragment or location.username or location.password
                    or not location.path.startswith(prefix)):
                raise ValueError("datatype creation Location is mismatched; retain the original intent")
            destination = _destination(spec.base_type + "/" + location.path[len(prefix):], spec.base_type)
            if location.raw_path != f"/input/v1/data_type/{destination}".encode():
                raise ValueError("datatype creation Location is noncanonical; retain the original intent")
        else:
            response.raise_for_status()
            if response.status_code not in {200, 201, 202} or response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                raise ValueError("datatype creation receipt is unreadable; retain the original intent")
            limit = 2 * 1024 * 1024
            if response.is_stream_consumed:
                body = response.content
            else:
                body = bytearray()
                for chunk in response.iter_raw(chunk_size=65536):
                    if len(body) + len(chunk) > limit:
                        raise ValueError("datatype creation receipt exceeds its limit; retain the original intent")
                    body.extend(chunk)
            receipt = _proof_json(body, limit=limit)
            if not isinstance(receipt, dict) or any(key in receipt and receipt[key] != account for key in ("fulcra_userid", "userid")):
                raise ValueError("datatype creation receipt owner is unreadable; retain the original intent")
            destination = _destination(receipt.get("id"), spec.base_type)
        # This writes original-account evidence only. Current auth may already
        # have changed; later verification still has to prove authority.
        journal.retain_receipt(reservation, destination)
        return destination


def resolve_data_type(
    *, client: BaseFulcraClient, spec: DataTypeSpec, account_id: str,
    pinned_id: str | None = None, allow_create: bool = False,
    journal: CreationJournal | None = None,
) -> ResolvedDataType:
    """Resolve a proven owned compatible v1 type, optionally creating once.

    ``account_id`` must match the token captured for this operation. Creation
    requires a durable journal, including when the previous process crashed.
    This function never posts records or silently modifies an existing type.
    """
    if allow_create and journal is None:
        raise ValueError("datatype creation requires a durable journal")
    if pinned_id:
        pinned_id = _destination(pinned_id, spec.base_type)
    headers = _account_headers(client, account_id)
    http = client._client()
    prior = journal.read(account_id, spec) if journal else None
    reservation = journal.reservation(account_id, spec) if journal and prior and prior[0] == spec.fingerprint else None
    bound = prior[2] if prior and prior[1] in {"resolved", "submitted"} else None
    explicit = pinned_id is not None
    unbound = not explicit and not (prior and prior[1] == "resolved")
    if bound:
        if pinned_id and pinned_id != bound:
            raise ValueError("datatype destination changed; retain the original binding")
        pinned_id = bound
    rows = _owned_types(http, headers, account_id)

    def matches(rows):
        if unbound and pinned_id and any(row.get("name") == spec.name and row.get("id") != pinned_id for row in rows):
            raise ValueError("ambiguous owned v1 destinations; retain the original creation receipt")
        return [row for row in rows if row.get("api_version") == "v1"
                and row.get("fulcra_userid") == account_id
                and row.get("recordable") is True
                and (row.get("id") == pinned_id if pinned_id else row.get("name") == spec.name)]

    candidates = matches(rows)
    if len(candidates) > 1:
        raise ValueError("ambiguous owned v1 destinations; configure an explicit pin")
    if not candidates:
        if pinned_id or not allow_create:
            raise ValueError("no owned v1 destination configured")
        assert journal is not None
        # A local account-check failure is not a possibly executed POST. Capture
        # the intended account before reserving an intent for the actual effect.
        headers = _account_headers(client, account_id)
        reservation = journal.reserve(account_id, spec)
        if reservation is None:
            prior = journal.read(account_id, spec)
            if prior and prior[1] == "rejected:422":
                raise RuntimeError("prior datatype creation was rejected; correct the specification before retry")
            raise RuntimeError("prior datatype creation is unresolved; reconcile catalog/readback before retry")
        pinned_id = _created_destination(http, headers, account_id, spec, journal, reservation)
        headers = _account_headers(client, account_id)
        candidates = matches(_owned_types(http, headers, account_id))
        if len(candidates) != 1:
            raise RuntimeError("created datatype is not uniquely verified; reconcile before retry")
    result = _resolve_candidate(http, headers, candidates[0], spec, account_id)
    _account_headers(client, account_id)
    if journal:
        journal.resolved(account_id, spec, result.data_type, reservation=reservation)
    return result
