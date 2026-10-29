"""Schema-checked records for actual v1 Event/Metric custom datatypes.

This is an opt-in boundary. The legacy annotation helpers remain available
until their consumers are migrated explicitly. An upload receipt represents
submission, not visibility; callers still own readback and uncertain retries.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from jsonschema import Draft202012Validator, SchemaError
from referencing import Registry
from referencing.exceptions import Unresolvable
from fulcra_api.record_validation import record_format_checker

from fulcra_common.client import BaseFulcraClient
from fulcra_common.json_values import snapshot_json
from .record_observations import (
    EvidenceSink, ExactReadObservation, ObservationContext, RequestBudget, RequestGuard,
    UploadReceiptObservation, _PARTIAL_HEADERS, _capture_response, _hooks, _now, _producer_scope,
    _ref_document, _retain, _schema_context,
)


_RESERVED = frozenset({"id", "sources", "tags", "start_time", "end_time",
                       "value", "unit"})
_INSTANT = re.compile(r'([0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2})'
                      r'(?:\.([0-9]+))?([Zz]|[+-][0-9]{2}:[0-9]{2})')


def _instant_key(value: str) -> tuple[datetime, str]:
    """Exact RFC3339 instant key without rounding arbitrary fractional digits.

    Normalize the whole second to UTC; compare the decimal fraction as text
    with insignificant trailing zeros removed. No float or datetime truncation
    participates in the fraction comparison.
    """
    if not isinstance(value, str) or (match := _INSTANT.fullmatch(value)) is None:
        raise ValueError('record timestamp must be an explicit RFC3339 instant')
    whole, fraction, offset = match.groups()
    # datetime accepts overflowing offset minutes by normalizing them. RFC3339
    # does not, so reject malformed components before parsing.
    if offset.upper() != 'Z' and (int(offset[1:3]) > 23 or int(offset[4:6]) > 59):
        raise ValueError('record timestamp has a malformed timezone offset')
    try:
        second = datetime.fromisoformat(whole.upper() + ('+00:00' if offset.upper() == 'Z' else offset))
        return second.astimezone(timezone.utc), (fraction or '').rstrip('0')
    except (ValueError, OverflowError):
        raise ValueError('record timestamp is outside the supported calendar') from None


def _destination(data_type: str, expected: str | None = None) -> str:
    if not isinstance(data_type, str):
        raise ValueError("a complete v1 Event/Metric destination is required")
    base, separator, identifier = data_type.partition("/")
    if base not in {"Event", "Metric"} or not separator or (expected and base != expected):
        raise ValueError("a complete matching v1 Event/Metric destination is required")
    try:
        parsed = UUID(identifier)
    except (ValueError, AttributeError) as exc:
        raise ValueError("v1 destination must contain a UUID") from exc
    if identifier.lower() != str(parsed):
        raise ValueError("v1 destination must use a canonical UUID")
    return f"{base}/{parsed}"


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("record timestamps must be timezone-aware datetimes")
    return value.astimezone(timezone.utc)


def _canonical_uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ValueError("an explicit canonical UUID is required") from None
    return value


def record_id_for(data_type: str, source_id: str) -> str:
    """The shared live/history identity mapping, without building a dummy row."""
    destination = _destination(data_type)
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("record source identity is required")
    return str(uuid5(NAMESPACE_URL, f"fulcra-tools:v1:{destination}:{source_id}"))


def _record(
    *, data_type: str, source_id: str, start_time: datetime,
    end_time: datetime | None, record_id: str | None,
    extra_sources: Sequence[str], tags: Sequence[str],
    fields: Mapping[str, object] | None,
) -> dict:
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("record source identity is required")
    start = _utc(start_time)
    end = start if end_time is None else _utc(end_time)
    if end < start:
        raise ValueError("record end precedes its start")
    if fields is None:
        fields = {}
    if not isinstance(fields, Mapping) or any(not isinstance(k, str) for k in fields):
        raise ValueError("record enrichment must be a JSON object")
    if _RESERVED.intersection(fields):
        raise ValueError("record enrichment cannot replace reserved fields")
    record = dict(fields)
    sources = [source_id]
    for source in extra_sources:
        if not isinstance(source, str):
            raise ValueError("record sources must be strings")
        if source and source not in sources:
            sources.append(source)
    if any(not isinstance(tag, str) for tag in tags):
        raise ValueError("record tags must be strings")
    if record_id is not None:
        try:
            identity = str(UUID(record_id))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("record ID must be a UUID") from exc
    else:
        identity = record_id_for(data_type, source_id)
    record.update(id=identity, start_time=start.isoformat().replace("+00:00", "Z"),
                  end_time=end.isoformat().replace("+00:00", "Z"),
                  sources=sources, tags=list(dict.fromkeys(tags)))
    return snapshot_json(record)


def build_event_record(
    *, data_type: str, source_id: str, start_time: datetime,
    end_time: datetime | None = None, record_id: str | None = None,
    extra_sources: Sequence[str] = (), tags: Sequence[str] = (),
    fields: Mapping[str, object] | None = None,
) -> dict:
    """Build a flat v1 Event, with explicit equal timestamps for moments.

    Both live import and migration must supply the same primary source identity.
    Extra sources are fingerprints/provenance, not alternate primary IDs.
    """
    return _record(data_type=_destination(data_type, "Event"), source_id=source_id,
                   start_time=start_time, end_time=end_time, record_id=record_id,
                   extra_sources=extra_sources, tags=tags, fields=fields)


def build_metric_record(
    *, data_type: str, source_id: str, start_time: datetime, value: float,
    unit: str | None = None, end_time: datetime | None = None,
    record_id: str | None = None, extra_sources: Sequence[str] = (),
    tags: Sequence[str] = (), fields: Mapping[str, object] | None = None,
) -> dict:
    """Build a v1 Metric without coercing bools, strings or non-finite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("metric value must be a finite number")
    if unit is not None and not isinstance(unit, str):
        raise ValueError("metric unit must be a string")
    record = _record(data_type=_destination(data_type, "Metric"), source_id=source_id,
                     start_time=start_time, end_time=end_time, record_id=record_id,
                     extra_sources=extra_sources, tags=tags, fields=fields)
    record["value"] = value
    if unit is not None:
        record["unit"] = unit
    return snapshot_json(record)


def _json_equal(left, right) -> bool:
    # JSON has separate boolean and number types; Python's False == 0 does not.
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_json_equal(a, b) for a, b in zip(left, right))
    return left == right


def record_matches(actual: dict | None, expected: dict) -> bool:
    """Compare every submitted field, allowing equivalent UTC time spellings.

    Server-added fields do not erase a match. A missing submitted field, naive
    timestamp, altered source/tag list or changed enrichment prevents a match.
    """
    if not isinstance(actual, dict):
        return False
    for key, value in expected.items():
        if key not in actual:
            return False
        if key in {"start_time", "end_time"}:
            try:
                left = _instant_key(actual[key])
                right = _instant_key(value)
            except (ValueError, TypeError, AttributeError):
                return False
            if left != right:
                return False
        elif not _json_equal(actual[key], value):
            return False
    return True


def _unique_record_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate record JSON key")
        result[key] = value
    return result


def _invalid_record_constant(_value):
    raise ValueError("nonfinite record JSON value")


def _check_record_schema(schema: dict) -> None:
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        raise ValueError("v1 destination schema is unavailable or malformed")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise ValueError("v1 destination schema is unavailable or malformed") from None


def _capture_expected_schema(expected_schema: dict | None, account_id: str | None) -> dict | None:
    if expected_schema is None:
        return None
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("exact schema validation requires an explicit account_id")
    if not isinstance(expected_schema, dict):
        raise ValueError("expected schema must be a finite JSON object")
    # Lazy same-package reuse avoids data_types -> v1's import dependency.
    from .data_types import _proof_json
    # fulcra-exact-schema-json-v1: representation identity, not JSON equality.
    body = json.dumps(snapshot_json(expected_schema), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    schema = _proof_json(body, limit=1024 * 1024)
    _check_record_schema(schema)
    return schema


def _capture_records(records: Sequence[dict]) -> list[dict]:
    if not records:
        return []
    try:
        return snapshot_json(list(records))
    except (TypeError, ValueError) as exc:
        raise ValueError("v1 records must contain finite JSON values") from exc


def _exact_query(destination: str, requested: list[str]) -> str:
    return "{" + " or ".join('__name__=' + json.dumps(destination) + ',id=' + json.dumps(identifier)
                              for identifier in requested) + "}"


def _exact_rows(rows, requested: list[str], found=None) -> dict[str, dict]:
    if not isinstance(rows, list):
        raise ValueError("v1 record readback is ambiguous or mismatched")
    found = {} if found is None else found
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in requested or row["id"] in found:
            raise ValueError("v1 record readback is ambiguous or mismatched")
        found[row["id"]] = row
    return found


class V1DataClient:
    """Use an existing account-bound client for v1 schema validation and ingest.

    No schema-read fallback, implicit annotation destination, token substitution,
    automatic POST retry, or claim of landed data is made at this boundary.
    """

    def __init__(self, client: BaseFulcraClient) -> None:
        self.client = client

    def _read_rows(self, query: str, account_id: str, *, label: str, max_rows: int,
                   request_guard=None, request_budget=None, observation_context=None,
                   _capture=None) -> list[dict]:
        """Check buffered JSONL response scope/completeness before using rows.

        The 32 MiB cap bounds decoded buffered parsing only: httpx.get has
        already loaded/decompressed the response. It is not a raw-wire,
        streaming allocation or absolute whole-request deadline guarantee.
        """
        observed = _hooks(request_guard, request_budget, observation_context)
        if observed:
            reservation = request_budget.reserve_events(1)
            request_guard(kind="record-get", context=observation_context)
            reservation.start()
        http = self.client._client()
        if observed:
            from .data_types import _account_headers
            headers = _account_headers(self.client, account_id, request_guard=request_guard,
                request_budget=request_budget, observation_context=observation_context)
        else:
            headers = self.client._authed_headers()
        params = {"q": query, "fulcra_userid": account_id}
        started_at = _now() if observed else None
        expected_origin = http.base_url if observed else None
        from ._historical_observed_transport import _closed, _send_prepared
        if observed and _closed(http):
            response = _send_prepared(http, method="GET", path="/data/v1/records", params=params,
                headers={**headers, "Accept": "application/x-jsonl", "Accept-Encoding": "identity", "Cache-Control": "no-store"},
                kind="record-get", context=observation_context, request_guard=request_guard, request_budget=request_budget)
        else:
            if observed:
                request_budget.charge_request("record-get")
                request_guard(kind="record-get", context=observation_context)
            response = http.get("/data/v1/records", params=params,
                headers={**headers, "Accept": "application/x-jsonl", "Accept-Encoding": "identity", "Cache-Control": "no-store"},
                follow_redirects=False)
        response.raise_for_status()
        if observed:
            facts = _capture_response(response, expected_origin, method="GET", path="/data/v1/records",
                query=list(params.items()), context=observation_context, started_at=started_at)
        expected = http.base_url
        partial = _PARTIAL_HEADERS
        failure = f"v1 {label} is partial, unreadable or mismatched; pending data retained"
        if (response.status_code != 200 or response.history or response.url.path != "/data/v1/records"
                or (response.url.scheme, response.url.host, response.url.port) != (expected.scheme, expected.host, expected.port)
                or list(response.url.params.multi_items()) != list(params.items())
                or any(name in response.headers for name in partial)
                or response.headers.get("x-fulcra-userid", account_id) != account_id
                or response.headers.get("content-encoding", "identity").strip().lower() != "identity"):
            raise ValueError(failure)
        try:
            body = response.content
            if len(body) > 32 * 1024 * 1024:
                raise ValueError
            text = body.decode("utf-8")
            rows, nodes = [], 0
            # Split only LF. splitlines/strip can erase malformed VT/FF and
            # Unicode whitespace, incorrectly manufacturing an empty result.
            offset, line_count = 0, 0
            while offset < len(text):
                line_count += 1
                if line_count > max(4096, max_rows * 2):
                    raise ValueError
                newline = text.find("\n", offset)
                end = len(text) if newline < 0 else newline
                line, offset = text[offset:end], end + 1
                if re.fullmatch(r"[ \t\r]*", line):
                    continue
                if len(rows) >= max_rows:
                    raise ValueError
                row = json.loads(line, object_pairs_hook=_unique_record_keys,
                                 parse_constant=_invalid_record_constant)
                pending = [(row, 0)]
                while pending:
                    item, depth = pending.pop()
                    nodes += 1
                    if depth > 64 or nodes > 100000:
                        raise ValueError
                    if isinstance(item, dict):
                        pending.extend((child, depth + 1) for child in item.values())
                    elif isinstance(item, list):
                        pending.extend((child, depth + 1) for child in item)
                rows.append(snapshot_json(row))
            for header in ("x-total-count", "x-total"):
                total = response.headers.get(header)
                if total is not None and (not re.fullmatch(r"[0-9]{1,10}", total) or int(total) != len(rows)):
                    raise ValueError
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError):
            raise ValueError(failure) from None
        if observed:
            _capture.update(facts, reservation=reservation)
        return rows

    def observe_records(self, data_type: str, record_ids: tuple[str, ...], *, account_id: str,
                        context: ObservationContext, on_observation: EvidenceSink,
                        request_budget: RequestBudget, request_guard: RequestGuard) -> ExactReadObservation:
        """Retain one complete locally validated exact response; no coverage claim."""
        destination = _destination(data_type)
        if destination != data_type or type(record_ids) is not tuple or not 1 <= len(record_ids) <= 32:
            raise ValueError("observed exact read requires 1..32 canonical IDs")
        identifiers = [_canonical_uuid(identifier) for identifier in record_ids]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("observed exact read requires distinct IDs")
        _producer_scope(context, account=account_id, destination=destination, identifiers=identifiers,
            purpose={"target-read", "policy-read"}, sink=on_observation, guard=request_guard, budget=request_budget)
        capture = {}
        rows = self._read_rows(_exact_query(destination, identifiers), account_id, label="record readback", max_rows=32,
            request_guard=request_guard, request_budget=request_budget, observation_context=context, _capture=capture)
        _exact_rows(rows, identifiers)
        return _retain(ExactReadObservation, context, capture["started_at"], capture["received_at"],
            {"request": capture["request"], "response": capture["response"], "rows": rows,
             "local_validation": "complete-local-framing-and-selector-validation",
             "parent_target_ref": None if context.purpose == "target-read" else {"sha256": context.original_target.sha256}},
            capture["reservation"], on_observation)

    def get_record(self, data_type: str, record_id: str, *, account_id: str) -> dict | None:
        """Read one exact ID in the captured account; an unknown read raises.

        This instant ID selector has no historical-window or aggregate-count
        shortcut. It verifies one identity, not coverage of an entire history.
        """
        identifier = str(UUID(record_id))
        return self.get_records(data_type, [identifier], account_id=account_id).get(identifier)

    def get_latest(self, data_type: str, *, account_id: str) -> dict | None:
        """One native last-over-time preview, bounded to the past 30 days."""
        destination = _destination(data_type)
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("preview requires the captured account")
        rows = self._read_rows('last_over_time({__name__=' + json.dumps(destination) + '}[30d])',
                               account_id, label="record preview", max_rows=1)
        try:
            if not rows:
                return None
            if len(rows) != 1 or not isinstance(rows[0], dict):
                raise ValueError
            record = rows[0]
            if str(UUID(record["id"])) != record["id"]:
                raise ValueError
            start = _utc(datetime.fromisoformat(record["start_time"].replace("Z", "+00:00")))
            end = _utc(datetime.fromisoformat(record["end_time"].replace("Z", "+00:00")))
            if end < start:
                raise ValueError
        except (ValueError, TypeError, AttributeError, KeyError):
            raise ValueError("v1 record preview is malformed or ambiguous") from None
        return record

    def get_records(self, data_type: str, record_ids: Sequence[str], *, account_id: str) -> dict[str, dict]:
        """Read exact IDs in bounded OR selectors, with no partial-success result.

        The API supports equality and OR, not regular-expression matchers. Each
        group repeats the complete custom destination. At most 32 instant-ID
        selectors (~4 KiB query) fit one request, well below the historical row
        ceiling. Duplicate/unrequested rows fail closed instead of hiding loss.
        """
        destination = _destination(data_type)
        identifiers = list(dict.fromkeys(str(UUID(identifier)) for identifier in record_ids))
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("readback requires the captured account")
        found = {}
        for offset in range(0, len(identifiers), 32):
            requested = identifiers[offset:offset + 32]
            rows = self._read_rows(_exact_query(destination, requested), account_id, label="record readback", max_rows=32)
            _exact_rows(rows, requested, found)
        return found

    def get_source_records(self, data_type: str, sources: Sequence[str], *, start: datetime,
                           end: datetime, account_id: str) -> list[dict]:
        """Read a bounded source-matcher window for cross-source twin policy.

        This is not a historical-coverage reader. A saturated response fails
        visibly; callers must split the window rather than trust a partial fold.
        """
        destination = _destination(data_type)
        start, end = _utc(start), _utc(end)
        if end <= start:
            raise ValueError("source readback requires a positive window")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("source readback requires the captured account")
        if any(not isinstance(source, str) or not source for source in sources):
            raise ValueError("source readback requires nonempty source IDs")
        sources = list(dict.fromkeys(sources))
        duration = math.ceil((end - start).total_seconds())
        found = {}
        for offset in range(0, len(sources), 32):
            requested = sources[offset:offset + 32]
            query = "{" + " or ".join('__name__=' + json.dumps(destination) + ',source=' + json.dumps(source)
                                      for source in requested) + f"}}[{duration}s] @ {end.timestamp()}"
            rows = self._read_rows(query, account_id, label="source readback", max_rows=4000)
            if len(rows) >= 4000:
                raise RuntimeError("v1 source readback coverage unknown; split the selected window")
            for row in rows:
                try:
                    if not isinstance(row, dict) or not isinstance(row.get("sources"), list):
                        raise ValueError
                    identity = str(UUID(row.get("id")))
                    if identity != row["id"] or any(not isinstance(s, str) for s in row["sources"]):
                        raise ValueError
                    if not set(row["sources"]).intersection(requested):
                        raise ValueError
                    if _utc(datetime.fromisoformat(row["end_time"].replace("Z", "+00:00"))) < _utc(datetime.fromisoformat(row["start_time"].replace("Z", "+00:00"))):
                        raise ValueError
                    if identity in found and not _json_equal(found[identity], row):
                        raise ValueError
                except (ValueError, TypeError, AttributeError, KeyError):
                    raise ValueError("v1 source readback is malformed or mismatched") from None
                found[identity] = row
        return list(found.values())

    def ingest(self, data_type: str, records: Sequence[dict], *, before_submit=None,
               expected_schema: dict | None = None, account_id: str | None = None) -> dict | None:
        """Validate and submit once, optionally persisting a dispatch intent.

        ``before_submit`` runs after schema/JSON validation and serialization,
        immediately before POST. Raising prevents the POST. It marks permission
        to attempt dispatch, not proof the server received or indexed anything.
        A supplied ``expected_schema`` requires an explicit captured account and
        must equal the final strict served schema used to validate this batch.
        ``None`` preserves the default read; only pin mode uses ``account_id``.
        """
        destination = _destination(data_type)
        expected = _capture_expected_schema(expected_schema, account_id)
        batch = _capture_records(records)
        return self._submit(destination, batch, before_submit=before_submit,
                            expected_schema=expected, account_id=account_id)

    def ingest_observed(self, data_type: str, records: Sequence[dict], *, account_id: str,
                        context: ObservationContext, on_observation: EvidenceSink, expected_schema: dict,
                        before_submit: Callable[[], None], request_budget: RequestBudget,
                        request_guard: RequestGuard) -> UploadReceiptObservation | None:
        """One pinned submission with retained schema/receipt; never retry POST."""
        destination = _destination(data_type)
        expected = _capture_expected_schema(expected_schema, account_id)
        batch = _capture_records(records)
        if expected is None or destination != data_type or not callable(before_submit):
            raise ValueError("observed ingest requires full schema and before_submit")
        _producer_scope(context, account=account_id, destination=destination,
            identifiers=[row.get("id") if isinstance(row, dict) else None for row in batch] if batch else list(map(str, context.requested_target_ids)),
            purpose={"upload-receipt"}, sink=on_observation, guard=request_guard, budget=request_budget)
        return self._submit(destination, batch, before_submit=before_submit, expected_schema=expected,
            account_id=account_id, request_guard=request_guard, request_budget=request_budget,
            observation_context=context, on_observation=on_observation)

    def delete_records(self, data_type: str, record_ids: Sequence[str], *,
                       action_id: str) -> dict | None:
        """Submit native v1 tombstones for a durably captured deletion action.

        Persist the action UUID before calling. Reusing it keeps tombstone IDs
        stable, but a receipt is not absence proof and a later replay can revive
        the target. Callers must establish replay exclusions first and verify
        target absence afterward. There is no automatic POST retry here.
        """
        destination = _destination(data_type)
        action = _canonical_uuid(action_id)
        identifiers = list(dict.fromkeys(_canonical_uuid(i) for i in record_ids))
        tombstones = [{"id": str(uuid5(NAMESPACE_URL,
            f"fulcra-tools:v1:delete:{action}:{destination}:{identifier}")),
            "record_id": identifier, "data_type": destination.split("/")[0]}
            for identifier in identifiers]
        return self._submit("DeletedRecord", tombstones)

    def _submit(self, destination: str, records: list[dict], *, before_submit=None,
                expected_schema: dict | None = None, account_id: str | None = None,
                request_guard=None, request_budget=None, observation_context=None, on_observation=None):
        observed = _hooks(request_guard, request_budget, observation_context)
        schema_capture = {}
        if observed:
            batch = self._validate(destination, records, expected_schema=expected_schema, account_id=account_id,
                request_guard=request_guard, request_budget=request_budget, observation_context=observation_context,
                on_observation=on_observation, _schema_capture=schema_capture)
        else:
            batch = self._validate(destination, records, expected_schema=expected_schema, account_id=account_id)
        if not batch:
            return None
        if observed:
            reservation = request_budget.reserve_events(1)
            request_guard(kind="record-post", context=observation_context)
            reservation.start()
            http = self.client._client()
            from .data_types import _account_headers
            headers = _account_headers(self.client, account_id, request_guard=request_guard,
                request_budget=request_budget, observation_context=observation_context)
        else:
            http, headers = self.client._client(), self.client._authed_headers()
        body = b"\n".join(json.dumps(r, sort_keys=True, allow_nan=False).encode() for r in batch)
        if observed:
            headers = {**headers, "Accept-Encoding": "identity", "Cache-Control": "no-store"}
        from ._historical_observed_transport import _closed, _send_prepared
        prepared = None
        if observed and _closed(http):
            prepared = http.prepare("POST", f"/ingest/v1/record/{destination}", params={"api_version": "v1"},
                content=body, headers={**headers, "content-type": "application/x-jsonl"})
        if before_submit is not None:
            before_submit()
        started_at = _now() if observed else None
        expected_origin = http.base_url if observed else None
        if observed and _closed(http):
            response = _send_prepared(http, method="POST", path=f"/ingest/v1/record/{destination}",
                headers=headers, kind="record-post", context=observation_context,
                request_guard=request_guard, request_budget=request_budget, prepared=prepared)
        else:
            if observed:
                request_budget.charge_request("record-post")
                request_guard(kind="record-post", context=observation_context)
            response = http.post(
                f"/ingest/v1/record/{destination}", params={"api_version": "v1"},
                content=body, headers={**headers, "content-type": "application/x-jsonl"},
                follow_redirects=False,
            )
        response.raise_for_status()
        if observed:
            from .data_types import _proof_json
            facts = _capture_response(response, expected_origin, method="POST", path=f"/ingest/v1/record/{destination}",
                query=[("api_version", "v1")], context=observation_context, started_at=started_at)
            receipt = _proof_json(response.content, limit=2 * 1024 * 1024)
            binding = {**facts["request"], "account_id": account_id, "attempt_id": str(observation_context.attempt_id),
                "original_hash": observation_context.original_hash, "body_sha256": hashlib.sha256(body).hexdigest(),
                "schema_ref": _ref_document(schema_capture["observation"].ref),
                "parent_target_context_hash": observation_context.original_target.sha256}
            return _retain(UploadReceiptObservation, observation_context, facts["started_at"], facts["received_at"],
                {"request_binding": binding, "response": facts["response"], "receipt": receipt,
                 "receipt_validation": "upload-receipt-only"}, reservation, on_observation)
        try:
            receipt = response.json()
        except ValueError as exc:
            raise RuntimeError("v1 upload receipt unavailable; reconcile IDs before retry") from exc
        if not isinstance(receipt, dict) or not receipt.get("upload_id"):
            raise RuntimeError("v1 upload receipt unavailable; reconcile IDs before retry")
        return receipt

    def validate_records(self, data_type: str, records: Sequence[dict], *,
                         expected_schema: dict | None = None, account_id: str | None = None) -> list[dict]:
        """Snapshot and validate without POST; optional pin mode matches ingest.

        Pin/account arguments are checked locally even for an empty batch,
        which performs no I/O and makes no remote schema equality claim.
        """
        destination = _destination(data_type)
        expected = _capture_expected_schema(expected_schema, account_id)
        batch = _capture_records(records)
        return self._validate(destination, batch, expected_schema=expected, account_id=account_id)

    def _validate(self, destination: str, batch: list[dict], *,
                  expected_schema: dict | None = None, account_id: str | None = None,
                  request_guard=None, request_budget=None, observation_context=None,
                  on_observation=None, _schema_capture=None) -> list[dict]:
        observed = _hooks(request_guard, request_budget, observation_context)
        # Public callers capture both inputs before any client/auth calls;
        # native tombstones are private, freshly built records.
        if not batch:
            return []
        if expected_schema is None:
            http = self.client._client()
            headers = self.client._authed_headers()
            response = http.get(f"/data/v1/catalog/{destination}/v1/schema", headers=headers)
            response.raise_for_status()
            schema = response.json()
        else:
            from .data_types import read_served_schema, _observe_schema_core
            if observed:
                schema, observation = _observe_schema_core(client=self.client, account_id=account_id,
                    data_type=destination, context=_schema_context(observation_context), on_observation=on_observation,
                    request_guard=request_guard, request_budget=request_budget)
                _schema_capture["observation"] = observation
            else:
                schema = read_served_schema(client=self.client, account_id=account_id, data_type=destination)
            if not _json_equal(schema, expected_schema):
                raise ValueError("v1 destination schema differs from the expected full schema")
        _check_record_schema(schema)
        # jsonschema's optional RFC3339 dependency is not always installed, and
        # strict RFC3339 differs from Fulcra ETL parsing. Use the SDK's checker.
        # A served snapshot is the complete validation authority. Unresolved
        # references must not fetch an unfrozen schema from another host.
        validator = Draft202012Validator(schema, format_checker=record_format_checker("v1"), registry=Registry())
        declared = set(schema["properties"])
        identities: dict[str, dict] = {}
        for index, record in enumerate(batch):
            if not isinstance(record, dict):
                raise ValueError(f"record {index}: expected a JSON object")
            try:
                identifier = _canonical_uuid(record.get("id"))
            except ValueError:
                raise ValueError(f"record {index}: an explicit canonical UUID is required") from None
            if set(record) - declared:
                raise ValueError(f"record {index}: undeclared fields would be lost")
            try:
                invalid = next(validator.iter_errors(record), None) is not None
            except Unresolvable:
                raise ValueError('v1 destination schema reference is unavailable') from None
            if invalid:
                # jsonschema error messages can contain private field VALUES.
                raise ValueError(f"record {index}: destination schema validation failed")
            if identifier in identities and not _json_equal(identities[identifier], record):
                raise ValueError(f"record {index}: conflicting payloads for the same ID")
            identities[identifier] = record
        return batch
