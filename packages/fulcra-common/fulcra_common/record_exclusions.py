"""Queryable, target-scoped replay exclusions, separate from native tombstones.

An exclusion is permanent within this protocol. There is no implicit restore or
"active" update that a racing source worker can use to revoke a deletion. The
caller supplies an account-bound store and a proven owned EXCLUSION_SPEC type.
Discovery uses the authoritative owned-type list and served schemas. Native
deletion and source writes remain separate caller duties.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import time

from jsonschema import Draft202012Validator, SchemaError

from .data_types import (DataTypeSpec, _account_headers, _owned_types,
                         _resolve_candidate, _schema_contains, _read_schema, resolve_data_type)
from .v1 import (_canonical_uuid, _destination, build_event_record,
                 record_id_for, record_matches)


# Package-owned contract is shared with browser writers. Required-field order
# participates in the existing creation fingerprint; never sort it implicitly.
_CONTRACT = json.loads(Path(__file__).with_name("record_exclusions_contract.json").read_text(encoding="utf-8"))
if _CONTRACT["version"] != 1 or _CONTRACT["base_type"] != "Event":
    raise ValueError("unsupported replay exclusion contract")
EXCLUSION_SPEC = DataTypeSpec(
    key=_CONTRACT["key"], base_type=_CONTRACT["base_type"],
    name=_CONTRACT["name"], description=_CONTRACT["description"],
    fields=_CONTRACT["fields"], required_fields=tuple(_CONTRACT["required_fields"]),
)


def find_exclusion_types(*, client, account_id: str, owned_types=None) -> dict:
    """Identify owned controls by served schema, including renamed controls.

    Creation metadata keys are not retained by the provider. Never assume a
    name-only miss means there is no policy. ``owned_types``, when supplied by
    the setup picker, is the validated direct-reader result, never an aggregate
    data-service catalog; its partial response cannot prove policy absence.
    A failed/malformed schema or a capped scan is unknown, not permission to
    create a new empty control. Bound sessions use their journal pin instead.
    """
    headers = _account_headers(client, account_id)
    http = client._client()
    rows = _owned_types(http, headers, account_id) if owned_types is None else owned_types
    candidates = [row for row in rows if row.get("fulcra_userid") == account_id
        and row.get("api_version") == "v1" and str(row.get("id", "")).startswith("Event/")]
    if len(candidates) > 64:
        raise ValueError("exclusion control discovery is capped; import the existing account binding")
    found = {}
    expected = json.loads(EXCLUSION_SPEC._schema_json)["properties"]
    deadline = time.monotonic() + 10
    for row in candidates:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("exclusion control discovery budget exceeded; import the existing account binding")
        destination = _destination(row.get("id"), "Event")
        schema = _read_schema(http, headers, destination, account_id, timeout=min(remaining, 8))
        try:
            if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
                raise ValueError
            Draft202012Validator.check_schema(schema)
        except (ValueError, SchemaError):
            raise ValueError("exclusion control discovery schema is unreadable") from None
        if _schema_contains(schema["properties"], expected):
            found[destination] = _resolve_candidate(http, headers, row, EXCLUSION_SPEC, account_id, served_schema=schema)
        elif row.get("name") == EXCLUSION_SPEC.name or set(expected) <= set(schema["properties"]):
            raise ValueError("the named exclusion control schema changed; reconcile the existing policy")
    return found


def resolve_exclusion_type(*, client, account_id: str, journal=None, allow_create=False):
    """Resolve a durable control pin or discover exactly one compatible type."""
    prior = journal.read(account_id, EXCLUSION_SPEC) if journal else None
    pin = prior[2] if prior and prior[1] == "resolved" else None
    if pin is None:
        found = find_exclusion_types(client=client, account_id=account_id)
        if len(found) > 1:
            raise ValueError("ambiguous exclusion controls; import the existing account binding")
        if found:
            result = next(iter(found.values()))
            _account_headers(client, account_id)
            if journal:
                journal.resolved(account_id, EXCLUSION_SPEC, result.data_type)
            return result
    return resolve_data_type(client=client, spec=EXCLUSION_SPEC, account_id=account_id,
        pinned_id=pin, allow_create=allow_create, journal=journal)


def verify_exclusion_type(*, client, account_id: str, journal, data_type: str | None = None):
    """Reprove an existing binding without discovery, adoption or journal writes."""
    return _verify_exclusion_type_core(client=client, account_id=account_id, journal=journal, data_type=data_type)


def _verify_exclusion_type_core(*, client, account_id, journal, data_type=None, _capture=None):
    hooks = {} if _capture is None else _capture.hooks
    prior = journal.read(account_id, EXCLUSION_SPEC)
    if prior is None or prior[1] != "resolved":
        raise ValueError("the original resolved exclusion binding is required")
    pin = prior[2]
    if data_type is not None and pin != _destination(data_type, "Event"):
        raise ValueError("the original exclusion binding changed")
    if _capture is not None:
        _capture.journal_received(prior, final=False)
    headers = _account_headers(client, account_id, **hooks)
    http = client._client()
    rows = _owned_types(http, headers, account_id, **hooks)
    candidates = [row for row in rows if row["id"] == pin]
    if len(candidates) != 1:
        raise ValueError("the bound exclusion control is missing")
    if _capture is None:
        result = _resolve_candidate(http, headers, candidates[0], EXCLUSION_SPEC, account_id)
    else:
        schema = _capture.read_schema()
        result = _resolve_candidate(http, headers, candidates[0], EXCLUSION_SPEC, account_id, served_schema=schema)
    headers = _account_headers(client, account_id, **hooks)
    final = [row for row in _owned_types(http, headers, account_id, **hooks) if row["id"] == pin]
    if final != candidates:
        raise ValueError("the bound exclusion control changed during verification")
    _account_headers(client, account_id, **hooks)
    final_journal = journal.read(account_id, EXCLUSION_SPEC)
    if _capture is not None:
        _capture.journal_received(final_journal, final=True)
    if final_journal != prior:
        raise ValueError("the original exclusion binding changed during verification")
    return result


def _source(destination: str, identifier: str) -> str:
    return f"collect:v1:excluded:{destination}:{identifier}"


def _record(control: str, destination: str, identifier: str, *, action_id: str,
            requested_at: datetime, reason: str, evidence: list[str]) -> dict:
    action = _canonical_uuid(action_id)
    if not isinstance(reason, str) or reason not in {"undo", "migration", "operator"}:
        raise ValueError("exclusion reason is unsupported")
    if not isinstance(evidence, list) or any(not isinstance(item, str) or not item for item in evidence):
        raise ValueError("exclusion evidence must contain nonempty references")
    return build_event_record(data_type=control, source_id=_source(destination, identifier),
        start_time=requested_at, fields={"schema_version": 1,
            "target_data_type": destination, "target_record_id": identifier,
            "action_id": action, "reason": reason, "evidence": list(evidence)})


def _validated_exclusion_rows(control_data_type, destination, lookup, rows, *, account_id):
    """Validate already-read full rows without performing another transport read."""
    if not isinstance(rows, dict) or rows.keys() - lookup.keys():
        raise ValueError("exclusion readback is malformed or mismatched")
    found = {}
    for identity, row in rows.items():
        try:
            if any(key in row and (account_id is None or row[key] != account_id)
                   for key in ("fulcra_userid", "userid")):
                raise ValueError
            expected = _record(control_data_type, destination, lookup[identity],
                action_id=row["action_id"], reason=row["reason"], evidence=row["evidence"],
                requested_at=datetime.fromisoformat(row["start_time"].replace("Z", "+00:00")))
            if not record_matches(row, expected):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError):
            raise ValueError("exclusion readback is malformed or mismatched") from None
        found[lookup[identity]] = row
    return found


class RecordExclusions:
    """Batch-read policy and establish a permanent target exclusion.

    ``store`` implements account-bound ``get_records(type, ids)`` and
    ``ingest(type, rows)`` (for example, CollectRecordSession). Unknown reads or
    malformed policy fail closed. A receipt alone never establishes exclusion.
    The action UUID, timestamp and evidence must already be durably captured.
    This primitive does not serialize writers on different hosts; all callers
    still need before/after-write checks and controlled old-writer cutover.
    Visible prior exclusions are retained. Concurrent first creates can race;
    callers retain each original action in their separate durable action audit.
    """

    def __init__(self, store, data_type: str):
        self.store = store
        self.data_type = _destination(data_type, "Event")
        account = getattr(store, "account_id", None)
        try:
            self._account_id = _canonical_uuid(account) if account is not None else None
        except ValueError:
            raise ValueError("exclusion store account is unreadable") from None

    def _target(self, data_type: str) -> str:
        destination = _destination(data_type)
        if destination == self.data_type:
            raise ValueError("exclusion control records cannot exclude themselves")
        return destination

    def _guard_account(self) -> None:
        if getattr(self.store, "account_id", None) != self._account_id:
            raise ValueError("exclusion store account changed; retain the original policy context")

    def _read(self, destination: str, identifiers: list[str]) -> dict[str, dict]:
        if not identifiers:
            return {}
        self._guard_account()
        lookup = {record_id_for(self.data_type, _source(destination, i)): i for i in identifiers}
        rows = self.store.get_records(self.data_type, list(lookup))
        self._guard_account()
        found = _validated_exclusion_rows(self.data_type, destination, lookup, rows,
                                         account_id=self._account_id)
        self._guard_account()
        return found

    def excluded_ids(self, data_type: str, record_ids: list[str]) -> set[str]:
        destination = self._target(data_type)
        identifiers = list(dict.fromkeys(_canonical_uuid(i) for i in record_ids))
        return set(self._read(destination, identifiers))

    def exclude(self, *, data_type: str, record_id: str, action_id: str,
                requested_at: datetime, reason: str, evidence: list[str]) -> dict:
        destination = self._target(data_type)
        identifier = _canonical_uuid(record_id)
        expected = _record(self.data_type, destination, identifier, action_id=action_id,
                           requested_at=requested_at, reason=reason, evidence=evidence)
        prior = self._read(destination, [identifier])
        if identifier in prior:
            return prior[identifier]
        self.store.ingest(self.data_type, [expected])
        actual = self._read(destination, [identifier])
        if identifier not in actual:
            raise RuntimeError("replay exclusion is not yet visible; retain the deletion action for reconciliation")
        # Another valid permanent exclusion for the same target is sufficient.
        return actual[identifier]
