"""Inert, bounded transport facts and retained-store contracts.

No constructor, reference, retention mode or local validation establishes
provider coverage, owned catalog provenance or production replay authority.
Persistence/allowance protocols require a separately verified implementation.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import ClassVar, Literal, Protocol
from uuid import UUID, uuid4

from .json_values import snapshot_json

_OBSERVATION_LIMIT = 2 * 1024 * 1024
_PARTIAL_HEADERS = ("content-range", "link", "x-next-cursor", "x-next-page", "x-has-more",
                    "x-continuation-token", "next-cursor", "next-page", "x-page", "x-page-size", "x-limit", "x-offset")
_HEADERS = _PARTIAL_HEADERS + ("content-length", "content-type", "content-encoding", "transfer-encoding",
                               "x-total-count", "x-total", "x-fulcra-userid")
RequestKind = Literal["account-proof", "owned-list", "record-get", "schema-get", "record-post"]
EventKind = Literal["observation", "assessment", "issued-checkpoint", "outcome"]
ControlProjection = tuple[tuple[UUID, UUID], ...]


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError("observation fields are malformed")


def _hash(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("observation digest is malformed")


def _uuid(value):
    if not isinstance(value, UUID):
        raise ValueError("observation identity must be a UUID")


def _wire_uuid(value):
    if not isinstance(value, str):
        raise ValueError("observation UUID is malformed")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("observation UUID is malformed") from None
    if str(parsed) != value:
        raise ValueError("observation UUID is not canonical")
    return parsed


def _integer(value, minimum=0, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError("observation count is malformed")


def _account(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("observation account is required")


def _destination(value):
    from .v1 import _destination as validate
    if validate(value) != value:
        raise ValueError("observation destination is not canonical")


def _ids(value, *, empty=False):
    if not isinstance(value, list) or not (0 if empty else 1) <= len(value) <= 32:
        raise ValueError("observation ID count is invalid")
    for identifier in value:
        _wire_uuid(identifier)
    if len(set(value)) != len(value):
        raise ValueError("observation IDs must be distinct")


def _time(value):
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value) is None:
        raise ValueError("observation time is not canonical UTC")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("observation time is malformed") from None


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical(value):
    return json.dumps(snapshot_json(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _document(body, *, limit=_OBSERVATION_LIMIT):
    from .data_types import _proof_json
    if type(body) is not bytes:
        raise ValueError("observation requires canonical bytes")
    value = _proof_json(body, limit=limit)
    if _canonical(value) != body:
        raise ValueError("observation bytes are not canonical")
    return value


def _ref_document(value):
    if isinstance(value, ArtifactRef):
        return {"artifact_id": str(value.artifact_id), "sha256": value.sha256}
    if isinstance(value, EvidenceRef):
        return {"observation_id": str(value.observation_id), "sha256": value.sha256}
    if isinstance(value, DecisionRef):
        return {"event_id": str(value.event_id), "sha256": value.sha256}
    if isinstance(value, TargetContextRef):
        return {"sha256": value.sha256}
    raise ValueError("observation reference is malformed")


def _check_ref(value, kind, *, nullable=False):
    if value is None and nullable:
        return
    names = {"artifact": "artifact_id sha256", "evidence": "observation_id sha256",
             "decision": "event_id sha256", "target": "sha256"}
    _fields(value, names[kind])
    _hash(value["sha256"])
    if kind != "target":
        _wire_uuid(value[names[kind].split()[0]])


@dataclass(frozen=True)
class EvidenceRef:
    observation_id: UUID
    sha256: str

    def __post_init__(self):
        _uuid(self.observation_id)
        _hash(self.sha256)


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: UUID
    sha256: str

    def __post_init__(self):
        _uuid(self.artifact_id)
        _hash(self.sha256)


@dataclass(frozen=True)
class DecisionRef:
    event_id: UUID
    sha256: str

    def __post_init__(self):
        _uuid(self.event_id)
        _hash(self.sha256)


@dataclass(frozen=True)
class TargetContextRef:
    sha256: str

    def __post_init__(self):
        _hash(self.sha256)


@dataclass(frozen=True)
class OriginalTargetContext:
    canonical: bytes

    def __post_init__(self):
        doc = _document(self.canonical)
        _fields(doc, "version attempt_id original_hash account_id destination_data_type requested_target_ids read_contract_ref")
        if type(doc["version"]) is not int or doc["version"] != 1:
            raise ValueError("unsupported original target version")
        _wire_uuid(doc["attempt_id"])
        _hash(doc["original_hash"])
        _account(doc["account_id"])
        _destination(doc["destination_data_type"])
        _ids(doc["requested_target_ids"])
        _check_ref(doc["read_contract_ref"], "artifact", nullable=True)

    @property
    def document(self):
        return json.loads(self.canonical)

    @property
    def sha256(self):
        return hashlib.sha256(self.canonical).hexdigest()


@dataclass(frozen=True, kw_only=True)
class ObservationContext:
    version: int
    observation_id: UUID
    attempt_id: UUID
    original_hash: str
    account_id: str
    destination_data_type: str
    requested_target_ids: tuple[UUID, ...]
    purpose: Literal["target-read", "policy-read", "schema-read", "upload-receipt"]
    read_contract_ref: ArtifactRef | None
    original_target: OriginalTargetContext
    control_projection: ControlProjection

    def __post_init__(self):
        if type(self.version) is not int or self.version != 1 or not isinstance(self.original_target, OriginalTargetContext):
            raise ValueError("observation context version/anchor is invalid")
        if not isinstance(self.purpose, str):
            raise ValueError("observation purpose is malformed")
        _uuid(self.observation_id)
        _uuid(self.attempt_id)
        _hash(self.original_hash)
        _account(self.account_id)
        _destination(self.destination_data_type)
        if type(self.requested_target_ids) is not tuple or type(self.control_projection) is not tuple:
            raise ValueError("observation context collections must be immutable")
        for identifier in self.requested_target_ids:
            _uuid(identifier)
        _ids([str(i) for i in self.requested_target_ids], empty=self.purpose == "schema-read")
        if self.read_contract_ref is not None and not isinstance(self.read_contract_ref, ArtifactRef):
            raise ValueError("observation contract reference is invalid")
        original = self.original_target.document
        if (str(self.attempt_id) != original["attempt_id"] or self.original_hash != original["original_hash"]
                or self.account_id != original["account_id"]
                or (None if self.read_contract_ref is None else _ref_document(self.read_contract_ref)) != original["read_contract_ref"]):
            raise ValueError("observation retargets original provenance")
        pairs = []
        for pair in self.control_projection:
            if type(pair) is not tuple or len(pair) != 2:
                raise ValueError("control projection is malformed")
            for identifier in pair:
                _uuid(identifier)
            pairs.append(tuple(map(str, pair)))
        if pairs:
            if [p[0] for p in pairs] != original["requested_target_ids"] or len({p[1] for p in pairs}) != len(pairs):
                raise ValueError("control projection must cover original IDs exactly")
        identifiers = list(map(str, self.requested_target_ids))
        if self.purpose in {"target-read", "upload-receipt"}:
            if self.destination_data_type != original["destination_data_type"] or identifiers != original["requested_target_ids"] or pairs:
                raise ValueError("target observation scope changed")
        elif self.purpose == "schema-read":
            if identifiers:
                raise ValueError("schema observation IDs must be empty")
            if pairs:
                self._check_projection(pairs)
        elif self.purpose == "policy-read":
            if not pairs:
                raise ValueError("policy observation requires explicit projection")
            if self.destination_data_type == original["destination_data_type"] and identifiers == original["requested_target_ids"]:
                return  # policy result; its linked control read has its own strict scope
            if identifiers != [p[1] for p in pairs]:
                raise ValueError("control request IDs differ from projection")
            self._check_projection(pairs)
        else:
            raise ValueError("unsupported observation purpose")

    def _check_projection(self, pairs):
        from .record_exclusions import _source
        from .v1 import record_id_for
        if not self.destination_data_type.startswith("Event/") or any(
                record_id_for(self.destination_data_type, _source(self.original_target.document["destination_data_type"], target)) != control
                for target, control in pairs):
            raise ValueError("control projection identity is not deterministic")

    @property
    def document(self):
        return {"version": self.version, "observation_id": str(self.observation_id), "attempt_id": str(self.attempt_id),
                "original_hash": self.original_hash, "account_id": self.account_id,
                "destination_data_type": self.destination_data_type,
                "requested_target_ids": list(map(str, self.requested_target_ids)), "purpose": self.purpose,
                "read_contract_ref": None if self.read_contract_ref is None else _ref_document(self.read_contract_ref),
                "original_target": self.original_target.document,
                "control_projection": [[str(a), str(b)] for a, b in self.control_projection]}


def _context_from_document(doc):
    if not isinstance(doc["purpose"], str):
        raise ValueError("observation purpose is malformed")
    _ids(doc["requested_target_ids"], empty=doc["purpose"] == "schema-read")
    projection = doc["control_projection"]
    if not isinstance(projection, list) or any(not isinstance(pair, list) or len(pair) != 2 for pair in projection):
        raise ValueError("observation projection is malformed")
    contract = doc["read_contract_ref"]
    _check_ref(contract, "artifact", nullable=True)
    return ObservationContext(version=doc["version"], observation_id=_wire_uuid(doc["observation_id"]),
        attempt_id=_wire_uuid(doc["attempt_id"]), original_hash=doc["original_hash"], account_id=doc["account_id"],
        destination_data_type=doc["destination_data_type"], requested_target_ids=tuple(_wire_uuid(i) for i in doc["requested_target_ids"]),
        purpose=doc["purpose"], read_contract_ref=None if contract is None else ArtifactRef(_wire_uuid(contract["artifact_id"]), contract["sha256"]),
        original_target=OriginalTargetContext(_canonical(doc["original_target"])),
        control_projection=tuple(tuple(_wire_uuid(i) for i in pair) for pair in doc["control_projection"]))


def _origin(value):
    _fields(value, "scheme host port")
    if value["scheme"] not in {"http", "https"} or not isinstance(value["host"], str) or not value["host"]:
        raise ValueError("observation origin is malformed")
    if value["port"] is not None:
        _integer(value["port"], 1, 65535)


def _query(value):
    if not isinstance(value, list) or any(not isinstance(pair, list) or len(pair) != 2
            or any(not isinstance(item, str) for item in pair) for pair in value):
        raise ValueError("observation ordered query is malformed")


def _request(value, context):
    _fields(value, "method origin path ordered_query destination_data_type requested_target_ids")
    _origin(value["origin"])
    _query(value["ordered_query"])
    if value["method"] != "GET" or not isinstance(value["path"], str) or not value["path"].startswith("/"):
        raise ValueError("observation read request is malformed")
    if value["destination_data_type"] != context.destination_data_type or value["requested_target_ids"] != list(map(str, context.requested_target_ids)):
        raise ValueError("observation request/context differ")


def _response(value, *, origin, path, query, account, status):
    _fields(value, "status origin path ordered_query redirect_count body_byte_length body_sha256 headers")
    _origin(value["origin"])
    _query(value["ordered_query"])
    _integer(value["status"], 100, 599)
    _integer(value["redirect_count"])
    _integer(value["body_byte_length"])
    _hash(value["body_sha256"])
    if value["origin"] != origin or value["path"] != path or value["ordered_query"] != query or value["redirect_count"] or value["status"] not in status:
        raise ValueError("observation response scope is invalid")
    headers = value["headers"]
    _fields(headers, " ".join(_HEADERS))
    if any(item is not None and not isinstance(item, str) for item in headers.values()):
        raise ValueError("observation headers are malformed")
    if (any(headers[name] is not None for name in _PARTIAL_HEADERS)
            or headers["content-encoding"] is not None and headers["content-encoding"].strip().lower() != "identity"
            or headers["x-fulcra-userid"] is not None and headers["x-fulcra-userid"] != account):
        raise ValueError("observation framing/account is invalid")
    length = headers["content-length"]
    if length is not None and (re.fullmatch(r"[0-9]+", length) is None or int(length) != value["body_byte_length"]):
        raise ValueError("observation buffered length is invalid")
    if length is not None and headers["transfer-encoding"] is not None:
        raise ValueError("observation framing is contradictory")


@dataclass(frozen=True)
class _Observation:
    canonical: bytes
    kind: ClassVar[str]

    def __post_init__(self):
        doc = _document(self.canonical)
        _fields(doc, "version kind observation_id attempt_id original_hash account_id destination_data_type requested_target_ids purpose read_contract_ref original_target control_projection started_at received_at details")
        if doc["kind"] != self.kind or _time(doc["received_at"]) < _time(doc["started_at"]):
            raise ValueError("observation kind/time is invalid")
        try:
            context = _context_from_document(doc)
            self._validate_details(doc["details"], context)
        except (TypeError, KeyError, AttributeError, OverflowError):
            raise ValueError("observation field shape is invalid") from None

    @property
    def document(self):
        return json.loads(self.canonical)

    @property
    def sha256(self):
        return hashlib.sha256(self.canonical).hexdigest()

    @property
    def ref(self):
        return EvidenceRef(_wire_uuid(self.document["observation_id"]), self.sha256)


@dataclass(frozen=True)
class ExactReadObservation(_Observation):
    kind: ClassVar[str] = "exact-read"

    def _validate_details(self, details, context):
        _fields(details, "request response rows local_validation parent_target_ref")
        if context.purpose not in {"target-read", "policy-read"}:
            raise ValueError("exact read purpose is invalid")
        _request(details["request"], context)
        request = details["request"]
        from .v1 import _exact_query
        if request["path"] != "/data/v1/records" or request["ordered_query"] != [["q", _exact_query(context.destination_data_type, list(map(str, context.requested_target_ids)))], ["fulcra_userid", context.account_id]]:
            raise ValueError("exact read selector is invalid")
        _response(details["response"], origin=request["origin"], path=request["path"], query=request["ordered_query"], account=context.account_id, status={200})
        from .v1 import _exact_rows
        _exact_rows(details["rows"], list(map(str, context.requested_target_ids)))
        if details["local_validation"] != "complete-local-framing-and-selector-validation":
            raise ValueError("exact read validation is invalid")
        parent = details["parent_target_ref"]
        _check_ref(parent, "target", nullable=True)
        if parent != (None if context.purpose == "target-read" else {"sha256": context.original_target.sha256}):
            raise ValueError("exact read parent is invalid")
        if context.purpose == "policy-read" and context.destination_data_type == context.original_target.document["destination_data_type"]:
            raise ValueError("control read cannot impersonate policy result")
        for name in ("x-total-count", "x-total"):
            value = details["response"]["headers"][name]
            if value is not None and (re.fullmatch(r"[0-9]{1,10}", value) is None or int(value) != len(details["rows"])):
                raise ValueError("exact read total is invalid")

    @property
    def records(self):
        return tuple(self.document["details"]["rows"])


@dataclass(frozen=True)
class SchemaObservation(_Observation):
    kind: ClassVar[str] = "served-schema"

    def _validate_details(self, details, context):
        _fields(details, "request response schema schema_sha256 schema_hash_algorithm local_validation")
        if context.purpose != "schema-read":
            raise ValueError("schema purpose is invalid")
        _request(details["request"], context)
        request = details["request"]
        if request["path"] != f"/data/v1/catalog/{context.destination_data_type}/v1/schema" or request["ordered_query"]:
            raise ValueError("schema request is invalid")
        _response(details["response"], origin=request["origin"], path=request["path"], query=[], account=context.account_id, status={200})
        from .data_types import _proof_json
        if not isinstance(details["schema"], dict):
            raise ValueError("schema observation requires an object")
        body = _canonical(details["schema"])
        _proof_json(body, limit=1024 * 1024)
        if (details["schema_sha256"] != hashlib.sha256(body).hexdigest()
                or details["schema_hash_algorithm"] != "fulcra-exact-schema-json-v1"
                or details["local_validation"] != "bounded-strict-schema-response-validation"):
            raise ValueError("schema representation/validation is invalid")

    @property
    def schema(self):
        return self.document["details"]["schema"]


@dataclass(frozen=True)
class PolicyBindingProof(_Observation):
    kind: ClassVar[str] = "policy-binding-proof"

    def _validate_details(self, details, context):
        _fields(details, "control_data_type control_schema_ref owned_binding_ref phase parent_target_context_hash")
        _destination(details["control_data_type"])
        if (not details["control_data_type"].startswith("Event/") or details["control_data_type"] != context.destination_data_type
                or details["phase"] not in {"before", "after"} or details["parent_target_context_hash"] != context.original_target.sha256):
            raise ValueError("policy binding scope/phase is invalid")
        _check_ref(details["control_schema_ref"], "evidence")
        _check_ref(details["owned_binding_ref"], "artifact")


@dataclass(frozen=True)
class PolicyObservation(_Observation):
    kind: ClassVar[str] = "policy"

    def _validate_details(self, details, context):
        _fields(details, "target_to_control_ids valid_exclusion_rows excluded_target_ids record_read_ref binding_before_ref binding_after_ref validation")
        if (context.purpose != "policy-read" or context.destination_data_type != context.original_target.document["destination_data_type"]
                or details["target_to_control_ids"] != [[str(a), str(b)] for a, b in context.control_projection]
                or details["validation"] != "validated-permanent-policy-rows"):
            raise ValueError("policy observation scope is invalid")
        for name in ("record_read_ref", "binding_before_ref", "binding_after_ref"):
            _check_ref(details[name], "evidence")
        if details["binding_before_ref"]["observation_id"] == details["binding_after_ref"]["observation_id"]:
            raise ValueError("policy phases require distinct observations")
        _ids(details["excluded_target_ids"], empty=True)
        if not set(details["excluded_target_ids"]) <= set(map(str, context.requested_target_ids)) or not isinstance(details["valid_exclusion_rows"], list):
            raise ValueError("policy rows/IDs are invalid")
        # Full shared row validation and retained-chain resolution belong to B2,
        # not this inert constructor or an unverified production policy producer.
        if any(not isinstance(row, dict) for row in details["valid_exclusion_rows"]):
            raise ValueError("policy rows must be full JSON objects")


@dataclass(frozen=True)
class UploadReceiptObservation(_Observation):
    kind: ClassVar[str] = "upload-receipt"

    def _validate_details(self, details, context):
        _fields(details, "request_binding response receipt receipt_validation")
        if context.purpose != "upload-receipt":
            raise ValueError("receipt purpose is invalid")
        binding = details["request_binding"]
        _fields(binding, "method origin path ordered_query account_id attempt_id original_hash destination_data_type requested_target_ids body_sha256 schema_ref parent_target_context_hash")
        _origin(binding["origin"])
        _hash(binding["body_sha256"])
        _check_ref(binding["schema_ref"], "evidence")
        if (binding["method"] != "POST" or binding["path"] != f"/ingest/v1/record/{context.destination_data_type}"
                or binding["ordered_query"] != [["api_version", "v1"]] or binding["account_id"] != context.account_id
                or binding["attempt_id"] != str(context.attempt_id) or binding["original_hash"] != context.original_hash
                or binding["destination_data_type"] != context.destination_data_type
                or binding["requested_target_ids"] != list(map(str, context.requested_target_ids))
                or binding["parent_target_context_hash"] != context.original_target.sha256):
            raise ValueError("receipt request binding is invalid")
        _response(details["response"], origin=binding["origin"], path=binding["path"], query=binding["ordered_query"], account=context.account_id, status=range(200, 300))
        receipt = details["receipt"]
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("upload_id"), str) or not receipt["upload_id"]
                or details["receipt_validation"] != "upload-receipt-only"):
            raise ValueError("receipt is unavailable; reconcile before retry")


Observation = ExactReadObservation | SchemaObservation | PolicyBindingProof | PolicyObservation | UploadReceiptObservation


@dataclass(frozen=True)
class EvidenceCommit:
    ref: EvidenceRef
    original_hash: str
    ledger_revision: int
    retention_mode: Literal["audit-only", "decision-eligible"]

    def __post_init__(self):
        if not isinstance(self.ref, EvidenceRef):
            raise ValueError("evidence commit reference is invalid")
        _hash(self.original_hash)
        _integer(self.ledger_revision)
        if not isinstance(self.retention_mode, str) or self.retention_mode not in {"audit-only", "decision-eligible"}:
            raise ValueError("evidence retention mode is invalid")


EvidenceSink = Callable[[Observation], EvidenceCommit]


@dataclass(frozen=True)
class ReadAssessment:
    observation_ref: EvidenceRef
    scope_hash: str
    read_contract_ref: ArtifactRef
    deployment_binding_ref: ArtifactRef
    assessed_at: str
    validity_end: str
    coverage: Literal["authoritative-for-requested-ids", "unknown"]

    def __post_init__(self):
        if not isinstance(self.observation_ref, EvidenceRef) or not isinstance(self.read_contract_ref, ArtifactRef) or not isinstance(self.deployment_binding_ref, ArtifactRef):
            raise ValueError("assessment reference is invalid")
        _hash(self.scope_hash)
        if _time(self.validity_end) < _time(self.assessed_at) or not isinstance(self.coverage, str) or self.coverage not in {"authoritative-for-requested-ids", "unknown"}:
            raise ValueError("assessment validity/coverage is invalid")


@dataclass(frozen=True)
class ReadAssessmentEvent:
    canonical: bytes

    def __post_init__(self):
        doc = _document(self.canonical)
        _fields(doc, "version kind event_id attempt_id original_hash assessment")
        if type(doc["version"]) is not int or doc["version"] != 1 or doc["kind"] != "read-assessment":
            raise ValueError("assessment event version/kind is invalid")
        _wire_uuid(doc["event_id"])
        _wire_uuid(doc["attempt_id"])
        _hash(doc["original_hash"])
        a = doc["assessment"]
        _fields(a, "observation_ref scope_hash read_contract_ref deployment_binding_ref assessed_at validity_end coverage")
        _check_ref(a["observation_ref"], "evidence")
        for name in ("read_contract_ref", "deployment_binding_ref"):
            _check_ref(a[name], "artifact")
        ReadAssessment(EvidenceRef(_wire_uuid(a["observation_ref"]["observation_id"]), a["observation_ref"]["sha256"]),
            a["scope_hash"], ArtifactRef(_wire_uuid(a["read_contract_ref"]["artifact_id"]), a["read_contract_ref"]["sha256"]),
            ArtifactRef(_wire_uuid(a["deployment_binding_ref"]["artifact_id"]), a["deployment_binding_ref"]["sha256"]),
            a["assessed_at"], a["validity_end"], a["coverage"])

    @property
    def document(self):
        return json.loads(self.canonical)

    @property
    def sha256(self):
        return hashlib.sha256(self.canonical).hexdigest()

    @property
    def ref(self):
        return DecisionRef(_wire_uuid(self.document["event_id"]), self.sha256)


@dataclass(frozen=True)
class DecisionCommit:
    ref: DecisionRef
    original_hash: str
    ledger_revision: int
    retention_mode: Literal["audit-only", "decision-eligible"]

    def __post_init__(self):
        if not isinstance(self.ref, DecisionRef):
            raise ValueError("decision commit reference is invalid")
        _hash(self.original_hash)
        _integer(self.ledger_revision)
        if not isinstance(self.retention_mode, str) or self.retention_mode not in {"audit-only", "decision-eligible"}:
            raise ValueError("decision retention mode is invalid")


DecisionSink = Callable[[ReadAssessmentEvent], DecisionCommit]


@dataclass(frozen=True)
class RetainedObservation:
    observation: Observation
    commit: EvidenceCommit

    def __post_init__(self):
        _ack(self.observation, self.commit)


@dataclass(frozen=True)
class RetainedAssessment:
    event: ReadAssessmentEvent
    commit: DecisionCommit

    def __post_init__(self):
        if (not isinstance(self.event, ReadAssessmentEvent) or not isinstance(self.commit, DecisionCommit)
                or self.event.ref != self.commit.ref or self.event.document["original_hash"] != self.commit.original_hash):
            raise ValueError("retained assessment acknowledgement is invalid")


@dataclass(frozen=True)
class HistoricalReplayEvidence:
    original_hash: str
    target_record_id: UUID
    ever_positive: bool
    issued_seen: bool
    evidence_refs: tuple[EvidenceRef, ...]
    evidence_revision: int

    def __post_init__(self):
        _hash(self.original_hash)
        _uuid(self.target_record_id)
        _integer(self.evidence_revision)
        if type(self.ever_positive) is not bool or type(self.issued_seen) is not bool or type(self.evidence_refs) is not tuple or any(not isinstance(ref, EvidenceRef) for ref in self.evidence_refs):
            raise ValueError("historical evidence shape is invalid")


@dataclass(frozen=True)
class HistoricalDelivery:
    verified_ids: tuple[UUID, ...]
    excluded_ids: tuple[UUID, ...]
    held_ids: tuple[UUID, ...]
    conflict_ids: tuple[UUID, ...]
    unknown_ids: tuple[UUID, ...]
    evidence_refs: tuple[EvidenceRef, ...]
    assessment_refs: tuple[DecisionRef, ...]
    historical_complete: Literal[False] = False

    def __post_init__(self):
        seen = set()
        for group in (self.verified_ids, self.excluded_ids, self.held_ids, self.conflict_ids, self.unknown_ids):
            if type(group) is not tuple:
                raise ValueError("historical outcome IDs must be tuples")
            for identifier in group:
                _uuid(identifier)
                if identifier in seen:
                    raise ValueError("historical outcomes overlap")
                seen.add(identifier)
        if (self.historical_complete is not False or type(self.evidence_refs) is not tuple or type(self.assessment_refs) is not tuple
                or any(not isinstance(ref, EvidenceRef) for ref in self.evidence_refs)
                or any(not isinstance(ref, DecisionRef) for ref in self.assessment_refs)):
            raise ValueError("historical outcome evidence is invalid")


class RequestGuard(Protocol):
    def __call__(self, *, kind: RequestKind, context: ObservationContext) -> None: ...


class EventReservation(Protocol):
    def start(self) -> None: ...
    def consume(self, kind: EventKind, *, event_id: UUID, event_sha256: str) -> None: ...
    def release_unused(self) -> None: ...


class AttemptEventAllowance(Protocol):
    def reserve_events(self, count: int) -> EventReservation: ...


class RetainedEvidence(Protocol):
    def lookup(self, ref: EvidenceRef) -> RetainedObservation: ...
    def lookup_assessment(self, ref: DecisionRef) -> RetainedAssessment: ...
    def reload_history(self, original: OriginalTargetContext) -> HistoricalReplayEvidence: ...
    def bind_event_allowance(self, original: OriginalTargetContext, *, event_limit: int) -> AttemptEventAllowance: ...


class HistoricalReadAssessor(Protocol):
    def assess(self, observation: ExactReadObservation, *, authority: bytes, ownership_proof: bytes) -> ReadAssessment: ...
    def check_current(self, *, authority: bytes, ownership_proof: bytes, original: OriginalTargetContext,
                      stage: Literal["before-read", "before-issued", "before-post", "before-verified"]) -> None: ...


class RequestBudget:
    """Request counter delegates all event capacity to the verified retained store."""
    def __init__(self, request_limit: int, *, retained: RetainedEvidence,
                 original: OriginalTargetContext, event_limit: int = 128):
        _integer(request_limit)
        _integer(event_limit, 1, 128)
        if not isinstance(original, OriginalTargetContext):
            raise ValueError("budget original target is invalid")
        self._remaining = request_limit
        self._retained, self._original = retained, original
        self._allowance = retained.bind_event_allowance(original, event_limit=event_limit)

    def assert_bound(self, retained: RetainedEvidence, original: OriginalTargetContext) -> None:
        if retained is not self._retained or not isinstance(original, OriginalTargetContext) or original.canonical != self._original.canonical:
            raise ValueError("budget retained/original binding differs")

    def charge_request(self, kind: RequestKind) -> None:
        if kind not in {"account-proof", "owned-list", "record-get", "schema-get", "record-post"} or self._remaining <= 0:
            raise ValueError("request budget exhausted or kind invalid")
        self._remaining -= 1

    def reserve_events(self, count: int) -> EventReservation:
        _integer(count, 1)
        return self._allowance.reserve_events(count)


def _hooks(request_guard, request_budget, observation_context):
    values = request_guard, request_budget, observation_context
    if all(value is None for value in values):
        return False
    if not callable(request_guard) or not isinstance(request_budget, RequestBudget) or not isinstance(observation_context, ObservationContext):
        raise ValueError("observed request hooks must be supplied together")
    request_budget.assert_bound(request_budget._retained, observation_context.original_target)
    return True


def _producer_scope(context, *, account, destination, purpose, identifiers, sink, guard, budget):
    if not _hooks(guard, budget, context) or not callable(sink):
        raise ValueError("observed producer requires hooks and sink")
    if (context.account_id != account or context.destination_data_type != destination or context.purpose not in purpose
            or list(map(str, context.requested_target_ids)) != list(identifiers)):
        raise ValueError("observed producer arguments differ from context")


def _schema_context(context):
    return replace(context, observation_id=uuid4(), purpose="schema-read", requested_target_ids=())


def _ack(observation, commit):
    if (not isinstance(observation, (ExactReadObservation, SchemaObservation, PolicyBindingProof, PolicyObservation, UploadReceiptObservation))
            or not isinstance(commit, EvidenceCommit) or commit.ref != observation.ref
            or commit.original_hash != observation.document["original_hash"]):
        raise ValueError("evidence acknowledgement does not match exact observation")


def _retain(cls, context, started_at, received_at, details, reservation, sink):
    observation = cls(_canonical({**context.document, "kind": cls.kind,
        "started_at": started_at, "received_at": received_at, "details": details}))
    reservation.consume("observation", event_id=context.observation_id, event_sha256=observation.sha256)
    _ack(observation, sink(observation))
    return observation


def _url_origin(url):
    return {"scheme": url.scheme, "host": url.host, "port": url.port}


def _capture_response(response, expected, *, method, path, query, context, started_at):
    """Capture facts only from a complete buffered response; never fresh authority."""
    received_at = _now()
    origin = _url_origin(expected)
    if (response.request.method != method or _url_origin(response.request.url) != origin
            or response.request.url.path != path or list(response.request.url.params.multi_items()) != query):
        raise ValueError("observed request scope changed")
    # Inspect raw repetitions before httpx's combined header view loses them.
    for name in _HEADERS:
        if len(response.headers.get_list(name)) > 1:
            raise ValueError("observed response header is repeated")
    facts = {"status": response.status_code, "origin": _url_origin(response.url), "path": response.url.path,
             "ordered_query": [list(pair) for pair in response.url.params.multi_items()], "redirect_count": len(response.history),
             "body_byte_length": len(response.content), "body_sha256": hashlib.sha256(response.content).hexdigest(),
             "headers": {name: response.headers.get(name) for name in _HEADERS}}
    _response(facts, origin=origin, path=path, query=[list(pair) for pair in query], account=context.account_id,
              status={200} if method == "GET" else range(200, 300))
    request = {"method": method, "origin": origin, "path": path, "ordered_query": [list(pair) for pair in query],
               "destination_data_type": context.destination_data_type, "requested_target_ids": list(map(str, context.requested_target_ids))}
    return {"request": request, "response": facts, "started_at": started_at, "received_at": received_at}
