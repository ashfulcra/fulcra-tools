"""Reconcile a source batch against permanent replay policy and exact readback."""
from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from collections.abc import Callable
import hashlib
import json
from uuid import UUID, uuid4

from .json_values import snapshot_json
from . import record_observations as _observations
from .record_observations import (DecisionCommit, DecisionSink, EvidenceRef, EvidenceSink,
    ExactReadObservation, HistoricalDelivery, HistoricalReadAssessor, HistoricalReplayEvidence,
    ObservationContext, PolicyBindingProof, PolicyObservation, ReadAssessment, ReadAssessmentEvent,
    RequestBudget, RetainedAssessment, RetainedEvidence, RetainedObservation, SchemaObservation,
    UploadReceiptObservation)
from .v1 import _canonical_uuid, _destination, record_id_for, record_matches


@dataclass(frozen=True)
class RecordDelivery:
    verified_ids: tuple[str, ...]
    excluded_ids: tuple[str, ...]


def _dispatch_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _source_body_digest(records):
    return hashlib.sha256(b"\n".join(json.dumps(row, sort_keys=True, allow_nan=False).encode()
                                    for row in records)).hexdigest()


def prepared_source_dispatch(binding, records):
    """Bind one new source attempt to its immutable scope and original payload."""
    return {"version": 1, "binding": _dispatch_digest([binding, records]), "phase": "prepared",
            "issued_records": [], "body_sha256": None, "positive_ids": []}


class SourceDispatch:
    """Durable monotonic source dispatch; missing legacy evidence is UNKNOWN.

    ``persist`` must compare/read back the entire original pending envelope and
    guard its captured account/settings while holding the source mutation lease.
    """
    def __init__(self, binding, records, state, persist):
        self.records = snapshot_json(records)
        self.state = snapshot_json(state) if state is not None else None
        self.persist = persist
        if state is None:
            return
        original = {r["id"]: r for r in self.records}
        try:
            if (set(state) != {"version", "binding", "phase", "issued_records", "body_sha256", "positive_ids"}
                    or type(state["version"]) is not int or state["version"] != 1
                    or state["binding"] != _dispatch_digest([binding, self.records])
                    or state["phase"] not in {"prepared", "issued"}
                    or not isinstance(state["issued_records"], list)
                    or not isinstance(state["positive_ids"], list)
                    or len(set(state["positive_ids"])) != len(state["positive_ids"])
                    or not set(state["positive_ids"]) <= original.keys()):
                raise ValueError
            issued = state["issued_records"]
            ids = [r["id"] for r in issued]
            if len(set(ids)) != len(ids) or any(r != original.get(r["id"]) for r in issued):
                raise ValueError
            if state["phase"] == "prepared":
                if issued or state["body_sha256"] is not None:
                    raise ValueError
            elif not issued or state["body_sha256"] != _source_body_digest(issued):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise RuntimeError("source dispatch state UNKNOWN; retain original pending batch for reconciliation") from None

    def observe(self, identifiers):
        if self.state is None:
            return
        positive = set(self.state["positive_ids"]) | set(identifiers)
        if positive != set(self.state["positive_ids"]):
            updated = {**self.state, "positive_ids": sorted(positive)}
            self.persist(updated)
            self.state = updated

    def issue(self, records):
        if self.state is None or self.state["phase"] != "prepared":
            raise RuntimeError("source dispatch UNKNOWN; original pending batch must not be resubmitted")
        original = {r["id"]: r for r in self.records}
        subset = snapshot_json(records)
        if (not subset or len({r["id"] for r in subset}) != len(subset)
                or any(r != original.get(r["id"]) for r in subset)):
            raise RuntimeError("source dispatch subset differs from original pending payload")
        updated = {**self.state, "phase": "issued", "issued_records": subset,
                   "body_sha256": _source_body_digest(records)}
        self.persist(updated)
        self.state = updated

    def require_dispatchable(self):
        if self.state is None or self.state["phase"] != "prepared":
            raise RuntimeError("source dispatch UNKNOWN: uploaded v1 records are not yet visible; retain original pending batch")

    def require_exclusion_evidence(self, blocked, current_positive):
        if self.state is None:
            uncertain = set(blocked) - set(current_positive)
        else:
            issued = {r["id"] for r in self.state["issued_records"]}
            uncertain = set(blocked) & issued - set(self.state["positive_ids"])
        if uncertain:
            raise RuntimeError("source dispatch UNKNOWN under exclusion policy; retain original pending batch")


def _repair(store, exclusions, destination: str, blocked: set[str], actual: dict):
    revived = [identifier for identifier in blocked if identifier in actual]
    if revived:
        action = record_id_for(exclusions.data_type, "collect:v1:repair:" + destination)
        store.delete_records(destination, revived, action_id=action)
        if store.get_records(destination, revived):
            raise RuntimeError("native exclusion deletion is not yet visible; pending batch retained")


def enforce_exclusions(store, exclusions, data_type: str, record_ids: list[str]) -> set[str]:
    """Check policy and repair any visible revival under the caller's lease."""
    destination = _destination(data_type)
    identifiers = list(dict.fromkeys(_canonical_uuid(i) for i in record_ids))
    blocked = exclusions.excluded_ids(destination, identifiers)
    if blocked:
        _repair(store, exclusions, destination, blocked, store.get_records(destination, list(blocked)))
    return blocked


def reconcile_records(store, exclusions, data_type: str, records: list[dict], *, dispatch=None) -> RecordDelivery:
    """Reconcile one immutable batch while the caller holds its mutation lease.

    The account-bound store supplies get_records/ingest/delete_records. Callers
    validate the entire batch against its served schema before policy creation.
    Native tombstones repair excluded rows that an old writer already revived.
    Unknown responses/indexing delays raise, leaving the caller's outbox intact.
    There is no global compare-and-swap across hosts; controlled writer cutoff
    and the deletion actor's own verified completion remain required.
    """
    destination = _destination(data_type)
    try:
        batch = snapshot_json(records)
        if not isinstance(batch, list):
            raise ValueError
        unique = {}
        for row in batch:
            if not isinstance(row, dict):
                raise ValueError
            identifier = _canonical_uuid(row.get("id"))
            if identifier in unique and not (record_matches(row, unique[identifier])
                                             and record_matches(unique[identifier], row)):
                raise ValueError
            unique[identifier] = row
    except (TypeError, ValueError, AttributeError):
        raise ValueError("v1 delivery requires finite records with unambiguous stable IDs") from None
    if not unique:
        return RecordDelivery((), ())
    if dispatch is not None and dispatch.records != batch:
        raise RuntimeError("source dispatch original payload mismatch; retain pending batch")
    identifiers = list(unique)
    before = exclusions.excluded_ids(destination, identifiers)
    actual = store.get_records(destination, identifiers)
    positive = {i for i, row in unique.items() if record_matches(actual.get(i), row)}
    if dispatch is not None:
        dispatch.observe(positive)
    missing = [row for identifier, row in unique.items()
               if identifier not in before and not record_matches(actual.get(identifier), row)]
    if missing:
        if dispatch is None:
            store.ingest(destination, missing)
        else:
            dispatch.require_dispatchable()
            store.ingest(destination, missing, before_submit=lambda: dispatch.issue(missing))
    after = exclusions.excluded_ids(destination, identifiers)
    if before - after:
        raise RuntimeError("permanent replay policy changed unexpectedly; pending batch retained")
    actual = store.get_records(destination, identifiers)
    positive = {i for i, row in unique.items() if record_matches(actual.get(i), row)}
    if dispatch is not None:
        dispatch.observe(positive)
        dispatch.require_exclusion_evidence(after, positive)
    _repair(store, exclusions, destination, after, actual)
    allowed = [identifier for identifier in identifiers if identifier not in after]
    if any(not record_matches(actual.get(identifier), unique[identifier]) for identifier in allowed):
        raise RuntimeError("uploaded v1 records are not yet visible; pending batch retained")
    return RecordDelivery(tuple(allowed), tuple(identifier for identifier in identifiers if identifier in after))


def _historical_read_scope(observation: ExactReadObservation) -> str:
    """Bind an assessment to the captured original, projection and exact request."""
    doc = observation.document
    return hashlib.sha256(_observations._canonical({"original_target": doc["original_target"],
        "control_projection": doc["control_projection"], "purpose": doc["purpose"],
        "request": doc["details"]["request"]})).hexdigest()


def reconcile_historical_records(store, exclusions, data_type: str, records: list[dict], *,
        dispatch: SourceDispatch, history: HistoricalReplayEvidence, assessment: HistoricalReadAssessor,
        authority: bytes, ownership_proof: bytes, expected_schema: dict,
        context: ObservationContext, evidence_sink: EvidenceSink, retained: RetainedEvidence,
        decision_sink: DecisionSink, request_budget: RequestBudget,
        before_submit: Callable[[HistoricalReplayEvidence], None],
        persist_outcome: Callable[[HistoricalDelivery, HistoricalReplayEvidence], None],
        allow_submit: bool = False) -> HistoricalDelivery:
    """Unused singleton reconciliation seam for explicitly synthetic adapters.

    Trusted adapters must resolve exact original authority/ownership artifacts,
    retain observations and decisions, and implement atomic issued/outcome CAS
    plus independent readback. No production adapter or authority issuer exists.
    Persistence uncertainty propagates; this path never repairs or retries POST.
    """
    from .v1 import _capture_expected_schema, _json_equal
    required = ((store, "observe_records"), (store, "ingest_observed"),
        (exclusions, "observe_exclusions"), (exclusions, "binding_proof"),
        (assessment, "assess"), (assessment, "check_current"),
        (retained, "lookup"), (retained, "lookup_assessment"),
        (retained, "reload_history"), (retained, "bind_event_allowance"))
    if (any(not callable(getattr(obj, name, None)) for obj, name in required)
            or any(not callable(cb) for cb in (evidence_sink, decision_sink, before_submit, persist_outcome))
            or not isinstance(context, ObservationContext) or not isinstance(dispatch, SourceDispatch)
            or not isinstance(history, HistoricalReplayEvidence) or not isinstance(request_budget, RequestBudget)
            or type(authority) is not bytes or type(ownership_proof) is not bytes
            or type(allow_submit) is not bool):
        raise ValueError("historical reconciliation requires immutable context and evidence-capable callbacks")
    batch = snapshot_json(records)
    destination = _destination(data_type)
    if (not isinstance(batch, list) or len(batch) != 1 or not isinstance(batch[0], dict)
            or destination != data_type or context.purpose != "target-read"
            or context.destination_data_type != destination or len(context.requested_target_ids) != 1
            or context.control_projection or batch[0].get("id") != str(context.requested_target_ids[0])
            or not _json_equal(dispatch.records, batch)):
        raise ValueError("historical reconciliation requires the exact original singleton payload")
    original, identifier = context.original_target, context.requested_target_ids[0]
    if history.original_hash != context.original_hash or history.target_record_id != identifier:
        raise ValueError("historical replay evidence differs from original")
    schema = _capture_expected_schema(expected_schema, context.account_id)
    if schema is None:
        raise ValueError("historical reconciliation requires the full expected schema")
    control_type = _destination(getattr(exclusions, "data_type", None), "Event")
    if control_type == destination:
        raise ValueError("historical exclusion control cannot target itself")
    from .record_exclusions import _source
    projection = ((identifier, UUID(record_id_for(control_type, _source(destination, str(identifier))))),)
    authority, ownership_proof = bytes(authority), bytes(ownership_proof)
    request_budget.assert_bound(retained, original)
    binding = dispatch.state.get("binding") if isinstance(dispatch.state, dict) else None
    refs, decisions, commits, current_refs = {}, {}, {}, set()
    revision = history.evidence_revision
    ever_positive, issued_seen = history.ever_positive, history.issued_seen
    initial_refs = set(history.evidence_refs)

    def validate_dispatch():
        state = snapshot_json(dispatch.state)
        if not _json_equal(dispatch.records, batch):
            raise ValueError("historical original dispatch payload changed")
        if state is None:
            return None
        if (set(state) != {"version", "binding", "phase", "issued_records", "body_sha256", "positive_ids"}
                or type(state["version"]) is not int or state["version"] != 1 or state["binding"] != binding
                or state["phase"] not in {"prepared", "issued"} or not isinstance(state["positive_ids"], list)
                or len(set(state["positive_ids"])) != len(state["positive_ids"])
                or not set(state["positive_ids"]) <= {str(identifier)}
                or not isinstance(state["issued_records"], list)):
            raise ValueError("historical dispatch state UNKNOWN")
        if state["phase"] == "prepared":
            if state["issued_records"] or state["body_sha256"] is not None:
                raise ValueError("historical prepared dispatch differs")
        elif not _json_equal(state["issued_records"], batch) or state["body_sha256"] != _source_body_digest(batch):
            raise ValueError("historical issued dispatch differs")
        return state

    validate_dispatch()

    def current(stage):
        validate_dispatch()
        result = assessment.check_current(authority=authority, ownership_proof=ownership_proof,
                                          original=original, stage=stage)
        if result is not None:
            raise ValueError("historical current-authority callback must return None")

    def guard(*, kind, context):
        if (not isinstance(context, ObservationContext) or context.original_target.canonical != original.canonical
                or context.account_id != original.document["account_id"]
                or context.attempt_id != UUID(original.document["attempt_id"])
                or context.original_hash != original.document["original_hash"]):
            raise ValueError("historical child original scope differs")
        if kind not in {"account-proof", "owned-list", "record-get", "schema-get", "record-post"}:
            raise ValueError("historical request kind differs")
        target = context.destination_data_type == destination and not context.control_projection
        control = context.destination_data_type == control_type and context.control_projection == projection
        if (not (target or control) or (kind == "owned-list" and not control)
                or (kind == "schema-get" and context.purpose != "schema-read")
                or (kind == "record-get" and context.purpose not in {"target-read", "policy-read"})
                or (kind == "record-post" and (not target or context.purpose != "upload-receipt"))):
            raise ValueError("historical child request scope differs")
        if kind == "record-post":
            require_current_coverage(stage="before-post")
        else:
            current("before-read")

    def retained_observation(ref, expected=None, *, historical=False):
        item = retained.lookup(ref)
        if (not isinstance(item, RetainedObservation) or item.observation.ref != ref
                or (expected is not None and item.observation.canonical != expected.canonical)):
            raise ValueError("historical retained observation differs")
        _observations._ack(item.observation, item.commit)
        doc = item.observation.document
        anchor = doc["original_target"]
        fixed = original.document
        keys = ("account_id", "original_hash", "destination_data_type", "requested_target_ids", "read_contract_ref")
        if (any(anchor[k] != fixed[k] for k in keys)
                or (not historical and anchor != fixed)):
            raise ValueError("historical retained original scope differs")
        if ref in commits and commits[ref] != item.commit:
            raise ValueError("historical stored acknowledgement changed")
        commits[ref] = item.commit
        refs[ref] = item.observation
        return item

    def sink(observation):
        commit = evidence_sink(observation)
        _observations._ack(observation, commit)
        item = retained_observation(observation.ref, observation)
        if item.commit != commit:
            raise ValueError("historical sink acknowledgement differs from retained commit")
        current_refs.add(observation.ref)
        return commit

    def reload():
        nonlocal revision, ever_positive, issued_seen
        fresh = retained.reload_history(original)
        if (not isinstance(fresh, HistoricalReplayEvidence) or fresh.original_hash != context.original_hash
                or fresh.target_record_id != identifier or fresh.evidence_revision < revision
                or (ever_positive and not fresh.ever_positive) or (issued_seen and not fresh.issued_seen)
                or not initial_refs <= set(fresh.evidence_refs)):
            raise ValueError("historical retained replay evidence regressed or differs")
        for ref in fresh.evidence_refs:
            retained_observation(ref, historical=True)
        # The trusted complete fold proves provenance. These local cross-checks
        # also refuse projections contradicted by this invocation's observations.
        positive = any(isinstance(o, ExactReadObservation) and o.document["purpose"] == "target-read"
            and any(record_matches(row, batch[0]) for row in o.records) for o in refs.values())
        state = validate_dispatch()
        if (positive and not fresh.ever_positive) or (state and state["positive_ids"] and not fresh.ever_positive):
            raise ValueError("historical matching-positive linkage UNKNOWN")
        if fresh.ever_positive and not positive:
            raise ValueError("historical positive history lacks matching evidence linkage")
        if state and state["phase"] == "issued" and not fresh.issued_seen:
            raise ValueError("historical issued linkage UNKNOWN")
        revision, ever_positive, issued_seen = fresh.evidence_revision, fresh.ever_positive, fresh.issued_seen
        return fresh

    def current_coverage(*links, stage=None):
        facts = []
        for ref, observation_ref in links:
            saved = retained.lookup_assessment(ref)
            if (not isinstance(saved, RetainedAssessment) or ref not in decisions
                    or saved != decisions[ref] or saved.commit.ref != ref):
                raise ValueError("historical retained assessment acknowledgement changed")
            doc = saved.event.document
            value = doc["assessment"]
            if (doc["attempt_id"] != str(context.attempt_id) or doc["original_hash"] != context.original_hash
                    or value["observation_ref"] != _observations._ref_document(observation_ref)):
                raise ValueError("historical assessment observation linkage changed")
            item = retained_observation(observation_ref, refs[observation_ref])
            if (not isinstance(item.observation, ExactReadObservation)
                    or value["scope_hash"] != _historical_read_scope(item.observation)
                    or value["read_contract_ref"] != _observations._ref_document(context.read_contract_ref)):
                raise ValueError("historical retained assessment scope/contract differs")
            facts.append((value, item.commit, saved.commit))
        if stage is not None:
            current(stage)
        # Sample after all independent lookups, so the earlier support cannot
        # expire unnoticed while the later supporting records are being read.
        now = _observations._time(_observations._now())
        if any(not _observations._time(value["assessed_at"]) <= now < _observations._time(value["validity_end"])
               for value, _, _ in facts):
            raise ValueError("historical assessment validity expired")
        return all(value["coverage"] == "authoritative-for-requested-ids"
                   and observation.retention_mode == decision.retention_mode == "decision-eligible"
                   for value, observation, decision in facts)

    def assess_read(observation):
        retained_observation(observation.ref, observation)
        result = assessment.assess(observation, authority=authority, ownership_proof=ownership_proof)
        if (not isinstance(result, ReadAssessment) or result.observation_ref != observation.ref
                or result.scope_hash != _historical_read_scope(observation)
                or result.read_contract_ref != context.read_contract_ref
                or not (_observations._time(result.assessed_at) <= _observations._time(_observations._now())
                        < _observations._time(result.validity_end))):
            raise ValueError("historical assessment scope/contract differs")
        event = ReadAssessmentEvent(_observations._canonical({"version": 1, "kind": "read-assessment",
            "event_id": str(uuid4()), "attempt_id": str(context.attempt_id), "original_hash": context.original_hash,
            "assessment": {"observation_ref": _observations._ref_document(result.observation_ref),
                "scope_hash": result.scope_hash, "read_contract_ref": _observations._ref_document(result.read_contract_ref),
                "deployment_binding_ref": _observations._ref_document(result.deployment_binding_ref),
                "assessed_at": result.assessed_at, "validity_end": result.validity_end, "coverage": result.coverage}}))
        reservation = request_budget.reserve_events(1)
        reservation.start()
        reservation.consume("assessment", event_id=event.ref.event_id, event_sha256=event.sha256)
        commit = decision_sink(event)
        if (not isinstance(commit, DecisionCommit) or commit.ref != event.ref or commit.original_hash != context.original_hash):
            raise ValueError("historical decision acknowledgement differs")
        saved = retained.lookup_assessment(event.ref)
        if (not isinstance(saved, RetainedAssessment) or saved.event.canonical != event.canonical or saved.commit != commit):
            raise ValueError("historical retained assessment differs")
        # Reread the observation after decision retention: neither caller state
        # nor an earlier acknowledgement can stand in for retained coverage.
        retained_observation(observation.ref, observation)
        decisions[event.ref] = saved
        current_coverage((event.ref, observation.ref), stage="before-read")
        return event.ref

    def policy_read():
        before = exclusions.binding_proof(phase="before", context=context,
            on_observation=sink, request_budget=request_budget, request_guard=guard)
        if not isinstance(before, PolicyBindingProof):
            raise ValueError("historical before policy binding missing")
        retained_observation(before.ref, before)
        after_proofs = []
        def after():
            proof = exclusions.binding_proof(phase="after", context=context,
                on_observation=sink, request_budget=request_budget, request_guard=guard)
            if not isinstance(proof, PolicyBindingProof) or after_proofs:
                raise ValueError("historical after policy binding missing/repeated")
            retained_observation(proof.ref, proof)
            after_proofs.append(proof)
            return proof
        policy_context = replace(context, observation_id=uuid4(), purpose="policy-read", control_projection=projection)
        policy = exclusions.observe_exclusions(destination, (str(identifier),), context=policy_context,
            binding_before=before, revalidate_binding=after, on_observation=sink,
            request_budget=request_budget, request_guard=guard)
        if not isinstance(policy, PolicyObservation) or len(after_proofs) != 1:
            raise ValueError("historical retained policy chain missing")
        retained_observation(policy.ref, policy)
        details = policy.document["details"]
        if policy.document["control_projection"] != [[str(a), str(b)] for a, b in projection]:
            raise ValueError("historical policy projection differs")
        a, b = after_proofs[0].document["details"], before.document["details"]
        if (details["binding_before_ref"] != _observations._ref_document(before.ref)
                or details["binding_after_ref"] != _observations._ref_document(after_proofs[0].ref)
                or b["phase"] != "before" or a["phase"] != "after" or before.ref == after_proofs[0].ref
                or a["control_data_type"] != control_type or b["control_data_type"] != control_type
                or a["owned_binding_ref"] != b["owned_binding_ref"]
                or a["parent_target_context_hash"] != original.sha256 or b["parent_target_context_hash"] != original.sha256):
            raise ValueError("historical policy binding scope/phases differ")
        before_schema = retained_observation(EvidenceRef(UUID(b["control_schema_ref"]["observation_id"]), b["control_schema_ref"]["sha256"])).observation
        after_schema = retained_observation(EvidenceRef(UUID(a["control_schema_ref"]["observation_id"]), a["control_schema_ref"]["sha256"])).observation
        if (not isinstance(before_schema, SchemaObservation) or not isinstance(after_schema, SchemaObservation)
                or before_schema.ref == after_schema.ref or not _json_equal(before_schema.schema, after_schema.schema)
                or before_schema.document["destination_data_type"] != control_type
                or after_schema.document["destination_data_type"] != control_type):
            raise ValueError("historical policy served schema changed/reused")
        read_ref = details["record_read_ref"]
        read = retained_observation(EvidenceRef(UUID(read_ref["observation_id"]), read_ref["sha256"])).observation
        if (not isinstance(read, ExactReadObservation) or read.document["purpose"] != "policy-read"
                or read.document["destination_data_type"] != control_type
                or read.document["control_projection"] != [[str(a), str(b)] for a, b in projection]):
            raise ValueError("historical control read scope differs")
        control_rows = list(read.records)
        expected_blocked = [str(target) for target, control in projection
                            if any(row["id"] == str(control) for row in control_rows)]
        if (not _json_equal(details["valid_exclusion_rows"], control_rows)
                or details["excluded_target_ids"] != expected_blocked):
            raise ValueError("historical policy result differs from retained control rows")
        chain = [before_schema, before, read, after_schema, after_proofs[0], policy]
        if len({o.ref for o in chain}) != 6 or sum(len(o.canonical) for o in chain) > 8 * 1024 * 1024:
            raise ValueError("historical policy chain cap exceeded")
        return policy, assess_read(read)

    def target_read():
        child = replace(context, observation_id=uuid4())
        observation = store.observe_records(destination, (str(identifier),), account_id=context.account_id,
            context=child, on_observation=sink, request_budget=request_budget, request_guard=guard)
        if not isinstance(observation, ExactReadObservation):
            raise ValueError("historical exact target observation missing")
        retained_observation(observation.ref, observation)
        if observation.document["purpose"] != "target-read" or observation.document["control_projection"]:
            raise ValueError("historical target read scope differs")
        fresh = reload()  # matching positivity is retained before coverage use
        return observation, assess_read(observation), fresh

    def policy_read_ref():
        ref = policy.document["details"]["record_read_ref"]
        return EvidenceRef(UUID(ref["observation_id"]), ref["sha256"])

    def require_current_coverage(*, excluded=False, stage=None):
        links = [(target_assessment, target.ref)]
        if not excluded:
            links.append((policy_assessment, policy_read_ref()))
        if not current_coverage(*links, stage=stage):
            raise ValueError("historical current assessment coverage required")

    def finish(result):
        fresh = reload()
        evidence_refs = tuple(sorted(refs, key=lambda r: (str(r.observation_id), r.sha256)))
        assessment_refs = tuple(sorted(decisions, key=lambda r: (str(r.event_id), r.sha256)))
        candidate = HistoricalDelivery(**{key + "_ids": (identifier,) if key == result else ()
            for key in ("verified", "excluded", "held", "conflict", "unknown")},
            evidence_refs=evidence_refs, assessment_refs=assessment_refs)
        for ref in evidence_refs:
            retained_observation(ref, historical=True)
        for ref in assessment_refs:
            if retained.lookup_assessment(ref) != decisions[ref]:
                raise ValueError("historical outcome assessment acknowledgement changed")
        if result in {"verified", "excluded"}:
            if any(commits[r].retention_mode != "decision-eligible" for r in current_refs):
                raise ValueError("historical success requires decision-eligible evidence")
            require_current_coverage(excluded=result == "excluded", stage="before-verified")
        if persist_outcome(candidate, fresh) is not None:
            raise ValueError("historical outcome callback must return None")
        if result in {"verified", "excluded"}:
            require_current_coverage(excluded=result == "excluded", stage="before-verified")
        return candidate

    fresh = reload()
    current("before-read")
    target, target_assessment, fresh = target_read()
    policy, policy_assessment = policy_read()
    fresh = reload()

    def classify():
        blocked = bool(policy.document["details"]["excluded_target_ids"])
        actual = target.records[0] if target.records else None
        if blocked and actual is not None:
            return "held"
        if actual is not None and not record_matches(actual, batch[0]):
            return "conflict"
        links = [(target_assessment, target.ref)]
        if not blocked:
            links.append((policy_assessment, policy_read_ref()))
        if not current_coverage(*links):
            return "unknown"
        if blocked:
            return "excluded" if not fresh.issued_seen or fresh.ever_positive else "unknown"
        if actual is not None:
            return "verified"
        if fresh.ever_positive or fresh.issued_seen or validate_dispatch() is None:
            return "unknown"
        return None

    result = classify()
    if result is not None:
        return finish(result)
    if not allow_submit:
        return finish("held")

    def issue():
        newest = reload()
        state = validate_dispatch()
        if newest.ever_positive or newest.issued_seen or state is None or state["phase"] != "prepared":
            raise ValueError("historical first-write eligibility changed")
        require_current_coverage(stage="before-issued")
        if before_submit(newest) is not None:
            raise ValueError("historical issued callback must return None")
        after_issue = reload()
        if not after_issue.issued_seen or validate_dispatch()["phase"] != "issued":
            raise ValueError("historical issued checkpoint not retained")
        require_current_coverage(stage="before-post")

    receipt_context = replace(context, observation_id=uuid4(), purpose="upload-receipt")
    receipt = store.ingest_observed(destination, batch, account_id=context.account_id, context=receipt_context,
        on_observation=sink, expected_schema=schema, before_submit=issue,
        request_budget=request_budget, request_guard=guard)
    if not isinstance(receipt, UploadReceiptObservation):
        raise ValueError("historical nonempty upload receipt missing")
    retained_observation(receipt.ref, receipt)
    prior_blocked = set(policy.document["details"]["excluded_target_ids"])
    target, target_assessment, fresh = target_read()
    policy, policy_assessment = policy_read()
    if prior_blocked - set(policy.document["details"]["excluded_target_ids"]):
        raise ValueError("historical permanent policy changed")
    fresh = reload()
    return finish(classify() or "unknown")
