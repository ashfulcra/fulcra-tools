"""Replay policy is checked around writes, including native repair of revivals."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, replace
import hashlib
import json
import socket
from threading import Event, RLock, Thread, current_thread
from functools import lru_cache
from uuid import UUID, uuid4

import httpx
import pytest

from fulcra_common.v1 import build_event_record
from fulcra_common import record_observations as ro

DESTINATION = "Event/11111111-2222-4333-8444-555555555555"
CONTROL = "Event/aaaaaaaa-2222-4333-8444-555555555555"


def batch():
    return [build_event_record(data_type=DESTINATION, source_id=f"synthetic.delivery.{i}",
        start_time=datetime(2026, 1, 2, tzinfo=timezone.utc)) for i in range(3)]


@pytest.mark.parametrize("bad", [{1: "numeric"}, "\ud800"])
def test_delivery_refuses_lossy_json_before_policy_or_record_effects(bad):
    from fulcra_common.record_delivery import reconcile_records
    class NoEffects:
        def __getattr__(self, _):
            pytest.fail("invalid JSON reached a record/policy effect")
    with pytest.raises(ValueError, match="finite records|JSON"):
        reconcile_records(NoEffects(), NoEffects(), DESTINATION, [batch()[0] | {"custom": bad}])


class Store:
    def __init__(self):
        self.rows = {}
        self.posts = []
        self.deletes = []
        self.excluded = set()
        self.after_post = lambda: None
        self.visible = self.delete_visible = True
        self.fail_policy = self.fail_post = False

    def get_records(self, destination, ids):
        return {i: deepcopy(self.rows[i]) for i in ids if i in self.rows}

    def ingest(self, destination, records):
        self.posts.append(deepcopy(records))
        if self.visible:
            self.rows.update({r["id"]: deepcopy(r) for r in records})
        self.after_post()
        if self.fail_post:
            raise RuntimeError("synthetic uncertain upload")

    def delete_records(self, destination, ids, *, action_id):
        self.deletes.append((list(ids), action_id))
        if self.delete_visible:
            for identifier in ids:
                self.rows.pop(identifier, None)

    @property
    def data_type(self):
        return CONTROL

    def excluded_ids(self, destination, ids):
        if self.fail_policy:
            raise RuntimeError("synthetic unreadable policy")
        return self.excluded.intersection(ids)


def deliver(store, records=None):
    from fulcra_common.record_delivery import reconcile_records
    return reconcile_records(store, store, DESTINATION, batch() if records is None else records)


def test_existing_and_new_records_require_full_field_visibility():
    records = batch()
    store = Store()
    store.rows[records[0]["id"]] = records[0]
    result = deliver(store, records)
    assert set(result.verified_ids) == {r["id"] for r in records}
    assert result.excluded_ids == ()
    assert store.posts == [records[1:]]


def test_previously_excluded_pending_rows_are_skipped_and_revivals_are_deleted():
    records = batch()
    store = Store()
    store.excluded = {r["id"] for r in records[:2]}
    store.rows[records[0]["id"]] = records[0]  # Another old writer revived it.
    result = deliver(store, records)
    assert result.verified_ids == (records[2]["id"],)
    assert set(result.excluded_ids) == store.excluded
    assert store.posts == [[records[2]]]
    assert set(store.rows) == {records[2]["id"]}
    assert store.deletes[0][0] == [records[0]["id"]]


def test_exclusion_appearing_during_upload_is_repaired_before_success():
    records = batch()
    store = Store()
    store.after_post = lambda: store.excluded.add(records[1]["id"])
    result = deliver(store, records)
    assert result.excluded_ids == (records[1]["id"],)
    assert set(result.verified_ids) == {records[0]["id"], records[2]["id"]}
    assert records[1]["id"] not in store.rows
    assert store.deletes


def test_unknown_policy_keeps_the_batch_pending_without_a_source_post():
    store = Store()
    store.fail_policy = True
    with pytest.raises(RuntimeError, match="unreadable"):
        deliver(store)
    assert not store.posts and not store.deletes


def test_unknown_post_is_reconciled_before_any_retry():
    store = Store()
    store.fail_post = True
    with pytest.raises(RuntimeError, match="uncertain"):
        deliver(store)
    store.fail_post = False
    assert len(deliver(store).verified_ids) == 3
    assert len(store.posts) == 1


@pytest.mark.parametrize("phase", ["upload", "deletion"])
def test_receipts_without_final_visibility_never_report_complete(phase):
    store = Store()
    if phase == "upload":
        store.visible = False
    else:
        store.excluded = {batch()[0]["id"]}
        store.rows[batch()[0]["id"]] = batch()[0]
        store.delete_visible = False
    with pytest.raises(RuntimeError, match="not yet visible"):
        deliver(store)


def test_repair_tombstone_identity_is_stable_across_repeated_revivals():
    store = Store()
    record = batch()[0]
    store.excluded.add(record["id"])
    for _ in range(2):
        store.rows[record["id"]] = record
        deliver(store, [record])
    assert store.deletes[0] == store.deletes[1]


def test_conflicting_or_missing_stable_identity_fails_before_any_io():
    store = Store()
    records = batch()
    for bad in [[records[0], {**records[0], "sources": ["other"]}], [{"id": "bad"}]]:
        with pytest.raises(ValueError):
            deliver(store, bad)
    assert not store.posts and not store.deletes


def test_historical_entry_requires_evidence_capabilities_before_preparation():
    from fulcra_common import record_delivery as module
    reconcile = getattr(module, "reconcile_historical_records", None)
    assert callable(reconcile), "unused historical reconciler missing"
    with pytest.raises(ValueError, match="historical"):
        reconcile(Store(), Store(), DESTINATION, batch()[:1], dispatch=None, history=None,
            assessment=None, authority=b"{}", ownership_proof=b"{}", expected_schema={},
            context=None, evidence_sink=None, retained=None, decision_sink=None,
            request_budget=None, before_submit=None, persist_outcome=None)


# Every authority, owned resolver, retained store and lifecycle implementation
# below is synthetic and test-only. HTTP and SDK doubles never enforce guards.
H_ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
H_STAMP = "2026-01-02T12:00:00.000000Z"
H_END = "2026-01-03T12:00:00.000000Z"
H_SCHEMA = {"type": "object", "properties": {"id": {"type": "string"},
    "note": {"type": "string"}}, "required": ["id", "note"]}
H_ROW = {"id": "22222222-3333-4444-8555-666666666666", "note": "Synthetic café row"}


def h_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def h_hash(value):
    return hashlib.sha256(value).hexdigest()


def h_ref(ref):
    return ro._ref_document(ref)


def h_evidence(value):
    return ro.EvidenceRef(UUID(value["observation_id"]), value["sha256"])


def h_decision(value):
    return ro.DecisionRef(UUID(value["event_id"]), value["sha256"])


def h_replay(history):
    return {"original_hash": history.original_hash, "target_record_id": str(history.target_record_id),
        "ever_positive": history.ever_positive, "issued_seen": history.issued_seen,
        "evidence_refs": [h_ref(r) for r in history.evidence_refs], "evidence_revision": history.evidence_revision}


def h_delivery(delivery):
    return {**{key: list(map(str, getattr(delivery, key))) for key in
        ("verified_ids", "excluded_ids", "held_ids", "conflict_ids", "unknown_ids")},
        "evidence_refs": [h_ref(r) for r in delivery.evidence_refs],
        "assessment_refs": [h_ref(r) for r in delivery.assessment_refs], "historical_complete": False}


def h_scope_hash(observation):
    doc = observation.document
    return h_hash(h_bytes({"original_target": doc["original_target"],
        "control_projection": doc["control_projection"], "purpose": doc["purpose"],
        "request": doc["details"]["request"]}))


@lru_cache(maxsize=1024)
def h_immutable_event(body, kind):
    if kind == "observation":
        classes = {c.kind: c for c in (ro.ExactReadObservation, ro.SchemaObservation,
            ro.PolicyBindingProof, ro.PolicyObservation, ro.UploadReceiptObservation)}
        return classes[json.loads(body)["kind"]](body)
    if kind == "assessment":
        return ro.ReadAssessmentEvent(body)
    return FixtureLifecycleEvent(body)


@dataclass(frozen=True)
class FixtureOwnedBinding:
    canonical: bytes

    def __post_init__(self):
        doc = ro._document(self.canonical)
        ro._fields(doc, "version issuer fixture_id account_id control_data_type original_target_sha256 owned_type_ids journal_binding control_schema_ref")
        if (doc["version"] != 1 or doc["issuer"] != "synthetic-test-only"
                or doc["control_data_type"] not in doc["owned_type_ids"]
                or doc["journal_binding"] != ["synthetic-fingerprint", "resolved", doc["control_data_type"]]):
            raise ValueError("fixture owned binding malformed")
        ro._wire_uuid(doc["fixture_id"])
        ro._hash(doc["original_target_sha256"])
        ro._check_ref(doc["control_schema_ref"], "evidence")

    @property
    def document(self):
        return json.loads(self.canonical)


@dataclass(frozen=True)
class SyntheticAuthority:
    canonical: bytes


@dataclass(frozen=True)
class SyntheticOwnership:
    canonical: bytes


class FixtureManifest:
    def __init__(self):
        self.documents = {}

    def add(self, doc):
        body = h_bytes(doc)
        ref = ro.ArtifactRef(uuid4(), h_hash(body))
        self.documents[ref] = body
        return ref

    def resolve(self, ref):
        if ref not in self.documents or h_hash(self.documents[ref]) != ref.sha256:
            raise ValueError("fixture manifest artifact unavailable")
        return ro._document(self.documents[ref])


@dataclass(frozen=True)
class FixtureEventRef:
    event_id: UUID
    sha256: str

    def __post_init__(self):
        ro._uuid(self.event_id)
        ro._hash(self.sha256)


@dataclass(frozen=True)
class FixtureSlotRef:
    reservation_id: UUID
    slot_id: int

    def __post_init__(self):
        ro._uuid(self.reservation_id)
        ro._integer(self.slot_id, 0, 127)


@dataclass(frozen=True)
class FixtureLifecycleCommit:
    ref: FixtureEventRef
    original_hash: str
    ledger_revision: int
    slot: FixtureSlotRef
    state_sha256: str
    retention_mode: str

    def __post_init__(self):
        if not isinstance(self.ref, FixtureEventRef) or not isinstance(self.slot, FixtureSlotRef):
            raise ValueError("fixture lifecycle acknowledgement malformed")
        ro._hash(self.original_hash)
        ro._hash(self.state_sha256)
        ro._integer(self.ledger_revision)
        if self.retention_mode not in {"audit-only", "decision-eligible"}:
            raise ValueError("fixture lifecycle mode malformed")


@dataclass(frozen=True)
class FixtureLifecycleEvent:
    canonical: bytes

    def __post_init__(self):
        doc = ro._document(self.canonical)
        ro._fields(doc, "version event_id attempt_id original_hash at kind scope result proof_refs details")
        if type(doc["version"]) is not int or doc["version"] != 1:
            raise ValueError("fixture lifecycle version malformed")
        for name in ("event_id", "attempt_id"):
            ro._wire_uuid(doc[name])
        ro._hash(doc["original_hash"])
        ro._time(doc["at"])
        scope = doc["scope"]
        ro._fields(scope, "account_id destination_data_type target_ids original_target_context_sha256 expected_schema_sha256 authority_sha256 ownership_sha256")
        ro._account(scope["account_id"])
        ro._destination(scope["destination_data_type"])
        ro._ids(scope["target_ids"])
        if len(scope["target_ids"]) != 1:
            raise ValueError("fixture lifecycle requires singleton")
        for name in ("original_target_context_sha256", "expected_schema_sha256", "authority_sha256", "ownership_sha256"):
            ro._hash(scope[name])
        ro._fields(doc["proof_refs"], "observations assessments")
        for name, kind, id_key in (("observations", "evidence", "observation_id"),
                                   ("assessments", "decision", "event_id")):
            refs = doc["proof_refs"][name]
            for ref in refs:
                ro._check_ref(ref, kind)
            if refs != sorted(refs, key=lambda r: (r[id_key], r["sha256"])) or len({r[id_key] for r in refs}) != len(refs):
                raise ValueError("fixture lifecycle refs not unique/sorted")
        details = doc["details"]
        if doc["kind"] == "dispatch-issued":
            ro._fields(details, "expected_revision replay dispatch_before_sha256 dispatch_issued_sha256 body_sha256")
            if doc["result"] != "issued":
                raise ValueError("fixture checkpoint result malformed")
            for name in ("dispatch_before_sha256", "dispatch_issued_sha256", "body_sha256"):
                ro._hash(details[name])
        elif doc["kind"] == "outcome":
            ro._fields(details, "expected_revision replay dispatch_sha256 delivery reason")
            ro._hash(details["dispatch_sha256"])
            if doc["result"] not in {"verified", "excluded", "held", "conflict", "unknown"}:
                raise ValueError("fixture outcome malformed")
            delivery = details["delivery"]
            ro._fields(delivery, "verified_ids excluded_ids held_ids conflict_ids unknown_ids evidence_refs assessment_refs historical_complete")
            ids = [i for name in ("verified", "excluded", "held", "conflict", "unknown") for i in delivery[name + "_ids"]]
            if ids != scope["target_ids"] or delivery[doc["result"] + "_ids"] != ids or delivery["historical_complete"] is not False:
                raise ValueError("fixture outcome partition malformed")
            if details["reason"] not in {None, "excluded-pending-repair"}:
                raise ValueError("fixture outcome reason malformed")
        else:
            raise ValueError("fixture lifecycle kind malformed")
        ro._integer(details["expected_revision"])
        replay = details["replay"]
        ro._fields(replay, "original_hash target_record_id ever_positive issued_seen evidence_refs evidence_revision")
        ro.HistoricalReplayEvidence(replay["original_hash"], UUID(replay["target_record_id"]),
            replay["ever_positive"], replay["issued_seen"], tuple(h_evidence(r) for r in replay["evidence_refs"]), replay["evidence_revision"])
        if details["expected_revision"] != replay["evidence_revision"]:
            raise ValueError("fixture lifecycle replay revision differs")

    @property
    def document(self):
        return json.loads(self.canonical)

    @property
    def ref(self):
        return FixtureEventRef(UUID(self.document["event_id"]), h_hash(self.canonical))


@dataclass(frozen=True)
class RetainedLifecycle:
    event: FixtureLifecycleEvent
    commit: FixtureLifecycleCommit
    state_bytes: bytes


class FixtureJournal:
    def __init__(self, original, dispatch):
        self.lock = RLock()
        self.floor = 0
        self.bytes = h_bytes({"version": 1, "revision": 0, "accounting_revision": 0,
            "bindings": {}, "slots": [], "events": [], "positive_refs": [],
            "original": original.document, "dispatch": dispatch,
            "outcome": "pending", "last_event_ref": None})


class FixtureRetained:
    """Canonical synthetic journal: accounting revisions never count as events."""
    def __init__(self, env, journal=None):
        self.env = env
        self.journal = journal or FixtureJournal(env.original, env.dispatch.state)
        self.fail_after_append = None
        self.bad_ack = None
        self.bad_lookup = None
        self.before_cas = lambda: None

    def _load(self):
        state = json.loads(self.journal.bytes)
        if h_bytes(state) != self.journal.bytes:
            raise ValueError("fixture journal bytes not canonical")
        ro._fields(state, "version revision accounting_revision bindings slots events positive_refs original dispatch outcome last_event_ref")
        ro._integer(state["revision"])
        ro._integer(state["accounting_revision"])
        fixed = ("account_id", "original_hash", "destination_data_type", "requested_target_ids", "read_contract_ref")
        if (state["revision"] < self.journal.floor
                or any(state["original"][key] != self.env.original.document[key] for key in fixed)):
            raise ValueError("fixture journal regressed or original changed")
        seen, occupied = set(), set()
        for slot in state["slots"]:
            original = ro.OriginalTargetContext(h_bytes(state["bindings"][slot["anchor"]]["original"]))
            limit = state["bindings"][slot["anchor"]]["limit"]
            ro._integer(limit, 1, 128)
            if original.sha256 != slot["anchor"] or type(slot["slot_id"]) is not int or slot["slot_id"] not in range(limit):
                raise ValueError("fixture slot binding malformed")
            if slot["state"] not in {"held", "started", "consumed", "uncertain", "released"}:
                raise ValueError("fixture slot state malformed")
            key = slot["anchor"], slot["slot_id"]
            if slot["state"] != "released":
                if key in occupied:
                    raise ValueError("fixture occupied slot duplicated")
                occupied.add(key)
        for event in state["events"]:
            body = event["canonical"].encode()
            if event["event_id"] in seen or h_hash(body) != event["sha256"]:
                raise ValueError("fixture manifest event corrupt")
            if event.get("commit_sha256") != h_hash(h_bytes(event["commit"])):
                raise ValueError("fixture stored acknowledgement digest differs")
            seen.add(event["event_id"])
            slot = state["slots"][event["slot"]]
            if (slot["state"] != "consumed" or not slot["committed"]
                    or any(slot[k] != event[k] for k in ("event_id", "sha256", "kind"))):
                raise ValueError("fixture event/slot linkage corrupt")
            self._decode_event(state, event)
        if any(slot.get("committed") and not any(e["slot"] == i for e in state["events"])
               for i, slot in enumerate(state["slots"])):
            raise ValueError("fixture manifest missing committed event")
        expected = [h_ref(obs.ref) for obs in self._target_observations(state)
                    if self._matches_original(obs)]
        if state["positive_refs"] != expected:
            raise ValueError("fixture matching-positive projection corrupt")
        return state

    def _save(self, state, *, event=False):
        if event:
            state["revision"] += 1
        else:
            state["accounting_revision"] += 1
        self.journal.bytes = h_bytes(state)
        self.journal.floor = state["revision"]
        self.env.dispatch.state = deepcopy(state["dispatch"])
        if self._load() != state:
            raise ValueError("fixture independent journal reread differs")

    def _decode_event(self, state, event):
        body = event["canonical"].encode()
        doc = json.loads(body)
        original = state["bindings"][state["slots"][event["slot"]]["anchor"]]["original"]
        if doc["original_hash"] != original["original_hash"] or doc["attempt_id"] != original["attempt_id"]:
            raise ValueError("fixture retained event original/attempt differs")
        if event["kind"] == "observation":
            obj = h_immutable_event(body, "observation")
            if doc["original_target"] != original:
                raise ValueError("fixture retained observation anchor differs")
            ref, commit_type = obj.ref, ro.EvidenceCommit
        elif event["kind"] == "assessment":
            obj, commit_type = h_immutable_event(body, "assessment"), ro.DecisionCommit
            ref = obj.ref
        else:
            obj = h_immutable_event(body, event["kind"])
            commit = self._lifecycle_commit(event["commit"])
            if (obj.ref != commit.ref or h_hash(event["state_bytes"].encode()) != commit.state_sha256
                    or commit.original_hash != original["original_hash"]):
                raise ValueError("fixture lifecycle stored acknowledgement differs")
            return obj, commit
        saved = event["commit"]
        ro._fields(saved, "ref original_hash ledger_revision retention_mode")
        saved_ref = h_evidence(saved["ref"]) if event["kind"] == "observation" else h_decision(saved["ref"])
        commit = commit_type(saved_ref, saved["original_hash"], saved["ledger_revision"], saved["retention_mode"])
        if commit.ref != ref or commit.original_hash != original["original_hash"] or commit.ledger_revision > state["revision"]:
            raise ValueError("fixture stored acknowledgement scope/revision differs")
        return obj, commit

    @staticmethod
    def _lifecycle_commit(saved):
        ro._fields(saved, "ref original_hash ledger_revision slot state_sha256 retention_mode")
        ro._fields(saved["ref"], "event_id sha256")
        ro._fields(saved["slot"], "reservation_id slot_id")
        return FixtureLifecycleCommit(FixtureEventRef(UUID(saved["ref"]["event_id"]), saved["ref"]["sha256"]),
            saved["original_hash"], saved["ledger_revision"],
            FixtureSlotRef(UUID(saved["slot"]["reservation_id"]), saved["slot"]["slot_id"]),
            saved["state_sha256"], saved["retention_mode"])

    def _target_observations(self, state):
        for event in state["events"]:
            if event["kind"] == "observation":
                obj, _ = self._decode_event(state, event)
                if isinstance(obj, ro.ExactReadObservation) and obj.document["purpose"] == "target-read":
                    yield obj

    def _matches_original(self, obs):
        from fulcra_common.v1 import record_matches
        doc = obs.document
        return (doc["account_id"] == H_ACCOUNT and doc["original_hash"] == self.env.original.document["original_hash"]
            and doc["destination_data_type"] == DESTINATION
            and any(record_matches(row, H_ROW) for row in obs.records))

    def bind_event_allowance(self, original, *, event_limit):
        with self.journal.lock:
            state = self._load()
            binding = {"original": original.document, "limit": event_limit}
            if original.sha256 in state["bindings"] and state["bindings"][original.sha256] != binding:
                raise ValueError("fixture resumed allowance differs")
            if original.sha256 not in state["bindings"]:
                state["bindings"][original.sha256] = binding
                self._save(state)
            return FixtureAllowance(self, original)

    def _claim(self, state, kind, event_id, digest):
        claims = [(i, s) for i, s in enumerate(state["slots"]) if s["state"] == "consumed"
            and not s["committed"] and (s.get("kind"), s.get("event_id"), s.get("sha256")) == (kind, event_id, digest)]
        if len(claims) != 1 or any(e["event_id"] == event_id for e in state["events"]):
            raise ValueError("fixture append requires exact unique consumed claim")
        index, slot = claims[0]
        if slot["anchor"] != self.env.original.sha256:
            raise ValueError("fixture foreign original slot")
        return index, slot

    def _append(self, obj, kind):
        doc = obj.document
        identifier = doc.get("observation_id", doc.get("event_id"))
        digest = h_hash(obj.canonical)
        with self.journal.lock:
            state = self._load()
            index, slot = self._claim(state, kind, identifier, digest)
            if doc["original_hash"] != self.env.context.original_hash or doc["attempt_id"] != str(self.env.context.attempt_id):
                raise ValueError("fixture event provenance differs")
            commit_type = ro.EvidenceCommit if kind == "observation" else ro.DecisionCommit
            mode = "decision-eligible" if self.env.active else "audit-only"
            commit = commit_type(obj.ref, doc["original_hash"], state["revision"] + 1, mode)
            event = {"event_id": identifier, "sha256": digest, "kind": kind,
                "canonical": obj.canonical.decode(), "slot": index,
                "commit": {"ref": h_ref(commit.ref), "original_hash": commit.original_hash,
                    "ledger_revision": commit.ledger_revision, "retention_mode": commit.retention_mode}}
            event["commit_sha256"] = h_hash(h_bytes(event["commit"]))
            if kind == "assessment":
                observation = self.lookup(h_evidence(doc["assessment"]["observation_ref"])).observation
                if observation.document["original_target"] != self.env.original.document:
                    raise ValueError("fixture assessment original differs")
            else:
                if doc["original_target"] != self.env.original.document:
                    raise ValueError("fixture observation original differs")
                if isinstance(obj, ro.ExactReadObservation) and self._matches_original(obj):
                    state["positive_refs"].append(h_ref(obj.ref))
                    state["dispatch"]["positive_ids"] = [H_ROW["id"]]
            slot["committed"] = True
            state["events"].append(event)
            self._save(state, event=True)
            if self.fail_after_append == kind:
                raise RuntimeError("fixture written then raised " + kind)
            return self.bad_ack(commit) if self.bad_ack else commit

    def sink(self, obj):
        return self._append(obj, "observation")

    def decision_sink(self, obj):
        return self._append(obj, "assessment")

    def lookup(self, ref):
        state = self._load()
        found = [e for e in state["events"] if e["kind"] == "observation"
                 and e["event_id"] == str(ref.observation_id) and e["sha256"] == ref.sha256]
        if len(found) != 1:
            raise ValueError("fixture observation unavailable")
        obj, commit = self._decode_event(state, found[0])
        result = ro.RetainedObservation(obj, commit)
        return self.bad_lookup(result) if self.bad_lookup else result

    def lookup_assessment(self, ref):
        state = self._load()
        found = [e for e in state["events"] if e["kind"] == "assessment"
                 and e["event_id"] == str(ref.event_id) and e["sha256"] == ref.sha256]
        if len(found) != 1:
            raise ValueError("fixture assessment unavailable")
        obj, commit = self._decode_event(state, found[0])
        return ro.RetainedAssessment(obj, commit)

    def reload_history(self, original):
        state = self._load()
        if original.canonical != self.env.original.canonical:
            raise ValueError("fixture history original differs")
        if any(s["state"] == "uncertain" or (s["state"] == "consumed" and not s["committed"]) for s in state["slots"]):
            raise ValueError("fixture history persistence UNKNOWN")
        external = self.env.manifest.resolve(self.env.external_ref)
        if external != self.env.external_document or external["complete"] is not True:
            raise ValueError("fixture external history UNKNOWN")
        refs = tuple(h_evidence(r) for r in state["positive_refs"])
        positive = bool(refs)
        if state["dispatch"]["positive_ids"] and not positive:
            raise ValueError("fixture positive projection lacks matching link")
        issued = state["dispatch"]["phase"] == "issued" or bool(external["issued_events"])
        if (state["dispatch"]["phase"] == "issued" and not external["issued_events"]
                and not any(e["kind"] == "issued-checkpoint" for e in state["events"])):
            raise ValueError("fixture issued projection lacks checkpoint linkage")
        if state["outcome"] == "verified" and not positive:
            raise ValueError("fixture verified lacks matching link")
        return ro.HistoricalReplayEvidence(original.document["original_hash"], UUID(H_ROW["id"]),
            positive, issued, refs, state["revision"])

    def proof_refs(self):
        state = self._load()
        observations, assessments = [], []
        for event in state["events"]:
            obj, _ = self._decode_event(state, event)
            if event["kind"] == "observation":
                observations.append(h_ref(obj.ref))
            elif event["kind"] == "assessment":
                assessments.append(h_ref(obj.ref))
        return {"observations": sorted(observations, key=lambda r: (r["observation_id"], r["sha256"])),
                "assessments": sorted(assessments, key=lambda r: (r["event_id"], r["sha256"]))}

    def _facts(self, proofs, *, current_stage=None):
        observations = [self.lookup(h_evidence(r)) for r in proofs["observations"]]
        decisions = [self.lookup_assessment(h_decision(r)) for r in proofs["assessments"]]
        fixed = ("account_id", "original_hash", "destination_data_type", "requested_target_ids", "read_contract_ref")
        if any(any(item.observation.document["original_target"][key] != self.env.original.document[key]
                   for key in fixed) for item in observations):
            raise ValueError("fixture lifecycle foreign observation")
        state = self._load()
        order = {e["event_id"]: n for n, e in enumerate(state["events"])}
        observations.sort(key=lambda i: order[str(i.observation.ref.observation_id)])
        decisions.sort(key=lambda i: order[str(i.event.ref.event_id)])
        targets = [i for i in observations if isinstance(i.observation, ro.ExactReadObservation)
                   and i.observation.document["purpose"] == "target-read"]
        policies = [i for i in observations if isinstance(i.observation, ro.PolicyObservation)]
        target, policy = (targets[-1] if targets else None), (policies[-1] if policies else None)
        coverage = {}
        decision_facts = []
        for item in decisions:
            a = item.event.document["assessment"]
            obs = self.lookup(h_evidence(a["observation_ref"]))
            if a["scope_hash"] != h_scope_hash(obs.observation):
                raise ValueError("fixture lifecycle assessment scope differs")
            decision_facts.append((item, obs, a))
        if current_stage is not None:
            self.env.assessor.check_current(authority=self.env.authority.canonical,
                ownership_proof=self.env.ownership.canonical, original=self.env.original, stage=current_stage)
        now = ro._time(ro._now())
        for item, obs, a in decision_facts:
            coverage[obs.observation.ref] = (a["coverage"] == "authoritative-for-requested-ids"
                and item.commit.retention_mode == obs.commit.retention_mode == "decision-eligible"
                and ro._time(a["assessed_at"]) <= now < ro._time(a["validity_end"]))
        if policy is not None:
            details = policy.observation.document["details"]
            for name in ("record_read_ref", "binding_before_ref", "binding_after_ref"):
                ref = h_evidence(details[name])
                if ref not in {i.observation.ref for i in observations}:
                    raise ValueError("fixture lifecycle missing policy proof")
        return target, policy, coverage

    def _state_bytes(self, state):
        return h_bytes({"version": 1, "attempt_id": self.env.original.document["attempt_id"],
            "original_hash": self.env.context.original_hash,
            "original_target_context_sha256": self.env.original.sha256,
            "dispatch": state["dispatch"], "outcome": state["outcome"],
            "ledger_revision": state["revision"], "last_event_ref": state["last_event_ref"]})

    def append_lifecycle(self, event):
        from fulcra_common.record_delivery import _dispatch_digest, _source_body_digest
        self.before_cas()
        with self.journal.lock:
            state = self._load()
            doc, details = event.document, event.document["details"]
            kind = "issued-checkpoint" if doc["kind"] == "dispatch-issued" else "outcome"
            index, slot = self._claim(state, kind, str(event.ref.event_id), event.ref.sha256)
            if doc["scope"] != self.env.lifecycle_scope() or doc["attempt_id"] != str(self.env.context.attempt_id):
                raise ValueError("fixture lifecycle scope differs")
            # The event's own consumed claim is excluded from this complete fold.
            replay = self._history_for_cas(state, index)
            if details["expected_revision"] != state["revision"] or details["replay"] != h_replay(replay):
                raise ValueError("fixture stale lifecycle CAS")
            target, policy, coverage = self._facts(doc["proof_refs"])
            target_present = bool(target and target.observation.records)
            blocked = bool(policy and policy.observation.document["details"]["excluded_target_ids"])
            target_complete = bool(target and coverage.get(target.observation.ref))
            control_complete = bool(policy and coverage.get(h_evidence(policy.observation.document["details"]["record_read_ref"])))
            if kind == "issued-checkpoint":
                if (state["dispatch"]["phase"] != "prepared" or replay.ever_positive or replay.issued_seen
                        or not target_complete or not control_complete or target_present or blocked):
                    raise ValueError("fixture checkpoint ineligible")
                if doc["proof_refs"] != self.proof_refs():
                    raise ValueError("fixture checkpoint incomplete proof chain")
                issued = {**state["dispatch"], "phase": "issued", "issued_records": [H_ROW],
                          "body_sha256": _source_body_digest([H_ROW])}
                if (details["dispatch_before_sha256"] != _dispatch_digest(state["dispatch"])
                        or details["dispatch_issued_sha256"] != _dispatch_digest(issued)
                        or details["body_sha256"] != _source_body_digest([H_ROW])):
                    raise ValueError("fixture checkpoint dispatch/body differs")
                schemas = [self.lookup(h_evidence(r)).observation for r in doc["proof_refs"]["observations"]]
                if not any(isinstance(o, ro.SchemaObservation) and o.document["destination_data_type"] == DESTINATION
                           and o.schema == H_SCHEMA for o in schemas):
                    raise ValueError("fixture checkpoint missing final schema")
                _, _, coverage = self._facts(doc["proof_refs"], current_stage="before-issued")
                if not (coverage.get(target.observation.ref)
                        and coverage.get(h_evidence(policy.observation.document["details"]["record_read_ref"]))):
                    raise ValueError("fixture checkpoint assessment coverage expired")
                state["dispatch"], state["outcome"] = issued, "unknown"
            else:
                result = doc["result"]
                delivery = details["delivery"]
                if details["dispatch_sha256"] != _dispatch_digest(state["dispatch"]):
                    raise ValueError("fixture outcome dispatch differs")
                expected_proofs = {"observations": delivery["evidence_refs"], "assessments": delivery["assessment_refs"]}
                if expected_proofs != doc["proof_refs"]:
                    raise ValueError("fixture outcome proof linkage differs")
                reason = "excluded-pending-repair" if target_present and blocked else None
                if details["reason"] != reason or (reason and result != "held"):
                    raise ValueError("fixture outcome exclusion classification differs")
                if result in {"verified", "excluded"}:
                    _, _, coverage = self._facts(doc["proof_refs"], current_stage="before-verified")
                    target_complete = bool(target and coverage.get(target.observation.ref))
                    control_complete = bool(policy and coverage.get(h_evidence(policy.observation.document["details"]["record_read_ref"])))
                    if not target_complete or (not blocked and not control_complete):
                        raise ValueError("fixture outcome incomplete coverage")
                    supporting = [target, policy]
                    if any(i is not None and i.commit.retention_mode != "decision-eligible" for i in supporting):
                        raise ValueError("fixture outcome audit-only support")
                    if result == "verified" and (blocked or not target_present or not self._matches_original(target.observation)):
                        raise ValueError("fixture outcome lacks matching target")
                    if result == "excluded" and (not blocked or target_present or (replay.issued_seen and not replay.ever_positive)):
                        raise ValueError("fixture outcome lacks exclusion/absence history")
                if result == "conflict" and (blocked or not target_present or self._matches_original(target.observation)):
                    raise ValueError("fixture outcome conflict differs")
                state["outcome"] = result
            state["last_event_ref"] = {"event_id": str(event.ref.event_id), "sha256": event.ref.sha256}
            state["revision"] += 1
            state_bytes = self._state_bytes(state)
            commit = FixtureLifecycleCommit(event.ref, self.env.context.original_hash, state["revision"],
                FixtureSlotRef(UUID(slot["reservation_id"]), slot["slot_id"]), h_hash(state_bytes),
                "decision-eligible" if self.env.active else "audit-only")
            saved = {"ref": state["last_event_ref"], "original_hash": commit.original_hash,
                "ledger_revision": commit.ledger_revision,
                "slot": {"reservation_id": str(commit.slot.reservation_id), "slot_id": commit.slot.slot_id},
                "state_sha256": commit.state_sha256, "retention_mode": commit.retention_mode}
            slot["committed"] = True
            state["events"].append({"event_id": str(event.ref.event_id), "sha256": event.ref.sha256,
                "kind": kind, "canonical": event.canonical.decode(), "slot": index,
                "commit": saved, "commit_sha256": h_hash(h_bytes(saved)), "state_bytes": state_bytes.decode()})
            self._save(state)
            if self.fail_after_append == kind:
                raise RuntimeError("fixture written then raised " + kind)
            return self.bad_ack(commit) if self.bad_ack else commit

    def _history_for_cas(self, state, own_slot):
        # No other unacknowledged claim can be silently omitted; the callback's
        # single consumed lifecycle claim is the transaction being committed.
        if any(s["state"] == "uncertain" or (s["state"] == "consumed" and not s["committed"] and i != own_slot)
               for i, s in enumerate(state["slots"])):
            raise ValueError("fixture CAS persistence UNKNOWN")
        external = self.env.manifest.resolve(self.env.external_ref)
        if external != self.env.external_document or external["complete"] is not True:
            raise ValueError("fixture CAS external history UNKNOWN")
        refs = tuple(h_evidence(r) for r in state["positive_refs"])
        if (state["dispatch"]["positive_ids"] or state["outcome"] == "verified") and not refs:
            raise ValueError("fixture CAS positive linkage UNKNOWN")
        if (state["dispatch"]["phase"] == "issued" and not external["issued_events"]
                and not any(e["kind"] == "issued-checkpoint" for e in state["events"])):
            raise ValueError("fixture CAS issued linkage UNKNOWN")
        return ro.HistoricalReplayEvidence(self.env.context.original_hash, UUID(H_ROW["id"]), bool(refs),
            state["dispatch"]["phase"] == "issued" or bool(external["issued_events"]), refs, state["revision"])

    def lookup_lifecycle(self, ref):
        state = self._load()
        matches = [e for e in state["events"] if e["event_id"] == str(ref.event_id) and e["sha256"] == ref.sha256
                   and e["kind"] in {"issued-checkpoint", "outcome"}]
        if len(matches) != 1:
            raise ValueError("fixture lifecycle readback missing")
        obj, commit = self._decode_event(state, matches[0])
        slot = state["slots"][matches[0]["slot"]]
        if (commit.slot != FixtureSlotRef(UUID(slot["reservation_id"]), slot["slot_id"])
                or commit.ledger_revision != obj.document["details"]["expected_revision"] + 1):
            raise ValueError("fixture lifecycle readback slot/revision differs")
        result = RetainedLifecycle(obj, commit, matches[0]["state_bytes"].encode())
        return self.bad_lookup(result) if self.bad_lookup else result


class FixtureAllowance:
    def __init__(self, retained, original):
        self.retained, self.original = retained, original

    def reserve_events(self, count):
        with self.retained.journal.lock:
            state = self.retained._load()
            slots = [s for s in state["slots"] if s["anchor"] == self.original.sha256]
            if any(s["state"] == "uncertain" or (s["state"] == "consumed" and not s["committed"]) for s in slots):
                raise ValueError("fixture uncertain occupancy")
            used = {s["slot_id"] for s in slots if s["state"] != "released"}
            limit = state["bindings"][self.original.sha256]["limit"]
            free = sorted(set(range(limit)) - used)
            if len(free) < count:
                raise ValueError("fixture allowance exhausted")
            reservation_id, indices = str(uuid4()), []
            for slot_id in free[:count]:
                indices.append(len(state["slots"]))
                state["slots"].append({"reservation_id": reservation_id, "anchor": self.original.sha256,
                    "slot_id": slot_id, "state": "held", "committed": False})
            self.retained._save(state)
            return FixtureReservation(self.retained, self.original, tuple(indices))


class FixtureReservation:
    def __init__(self, retained, original, indices):
        self.retained, self.original, self.indices = retained, original, indices

    def _state(self):
        state = self.retained._load()
        if any(state["slots"][i]["anchor"] != self.original.sha256 for i in self.indices):
            raise ValueError("fixture foreign reservation")
        return state

    def start(self):
        with self.retained.journal.lock:
            state = self._state()
            for i in self.indices:
                if state["slots"][i]["state"] != "held":
                    raise ValueError("fixture reservation cannot start")
                state["slots"][i]["state"] = "started"
            self.retained._save(state)

    def consume(self, kind, *, event_id, event_sha256):
        ro._uuid(event_id)
        ro._hash(event_sha256)
        if kind not in {"observation", "assessment", "issued-checkpoint", "outcome"}:
            raise ValueError("fixture claim kind malformed")
        with self.retained.journal.lock:
            state = self._state()
            if any(s.get("event_id") == str(event_id) for s in state["slots"]):
                raise ValueError("fixture duplicate event identity")
            free = [i for i in self.indices if state["slots"][i]["state"] in {"held", "started"}]
            if not free:
                raise ValueError("fixture reservation already consumed")
            state["slots"][free[0]].update(state="consumed", kind=kind, event_id=str(event_id), sha256=event_sha256)
            self.retained._save(state)

    def release_unused(self):
        with self.retained.journal.lock:
            state = self._state()
            if any(state["slots"][i]["state"] != "held" for i in self.indices):
                raise ValueError("fixture cannot release started/consumed/uncertain slot")
            for i in self.indices:
                state["slots"][i]["state"] = "released"
            self.retained._save(state)


class FixtureOwnedResolver:
    def __init__(self, env):
        self.env = env

    def resolve(self, ref):
        return FixtureOwnedBinding(h_bytes(self.env.manifest.resolve(ref)))

    def verify_current(self, artifact, *, original, phase, schema, budget, request_guard):
        from fulcra_common.data_types import _account_headers
        env, doc = self.env, artifact.document
        if (phase not in {"before", "after"} or original.canonical != env.original.canonical
                or artifact.canonical != env.manifest.documents.get(env.owned_ref)
                or doc["original_target_sha256"] != original.sha256 or doc["account_id"] != H_ACCOUNT):
            raise ValueError("fixture owned artifact/phase scope differs")
        old_schema = env.retained.lookup(h_evidence(doc["control_schema_ref"])).observation
        if (not isinstance(old_schema, ro.SchemaObservation) or old_schema.schema != schema.schema
                or schema.document["destination_data_type"] != CONTROL):
            raise ValueError("fixture owned served schema differs")
        context = ro._context_from_document(schema.document)
        # The real observed account helper calls the inert SDK at its actual
        # guarded/charged boundary. Lists are explicit synthetic catalog scripts.
        for n in range(3):
            _account_headers(env.client.client, H_ACCOUNT, request_guard=request_guard,
                request_budget=budget, observation_context=context)
            if n < 2:
                budget.charge_request("owned-list")
                request_guard(kind="owned-list", context=context)
                env.trace.append("owned-list")
                env.on_owned_list(n, phase)
                if env.owned_ids != doc["owned_type_ids"]:
                    raise ValueError("fixture repeated owned list changed")
            if env.journal_binding != doc["journal_binding"]:
                raise ValueError("fixture saved owned journal changed")
            if self.resolve(env.owned_ref).canonical != artifact.canonical:
                raise ValueError("fixture owned manifest changed during verification")


def make_fixture_policy_proof(*, owned_binding, resolver, phase, schema, context,
                              on_observation, request_budget, request_guard):
    reservation = request_budget.reserve_events(1)
    reservation.start()
    resolver.verify_current(owned_binding, original=context.original_target, phase=phase,
        schema=schema, budget=request_budget, request_guard=request_guard)
    env = resolver.env
    return ro._retain(ro.PolicyBindingProof, context, H_STAMP, H_STAMP,
        {"control_data_type": CONTROL, "control_schema_ref": h_ref(schema.ref),
         "owned_binding_ref": h_ref(env.owned_ref), "phase": phase,
         "parent_target_context_hash": context.original_target.sha256}, reservation, on_observation)


class FixturePolicy:
    data_type = CONTROL

    def __init__(self, env):
        self.env = env
        self.calls = []

    def projection(self):
        from fulcra_common.record_exclusions import _source
        from fulcra_common.v1 import record_id_for
        return ((UUID(H_ROW["id"]), UUID(record_id_for(CONTROL, _source(DESTINATION, H_ROW["id"])))),)

    def binding_proof(self, *, phase, context, on_observation, request_budget, request_guard):
        from fulcra_common.data_types import observe_served_schema
        env = self.env
        self.calls.append((phase, request_guard, request_budget, on_observation, context.original_target.canonical))
        schema_context = replace(context, observation_id=uuid4(), purpose="schema-read",
            destination_data_type=CONTROL, requested_target_ids=(), control_projection=self.projection())
        schema = observe_served_schema(client=env.client.client, account_id=H_ACCOUNT,
            data_type=CONTROL, context=schema_context, on_observation=on_observation,
            request_budget=request_budget, request_guard=request_guard)
        if env.owned_ref is None:
            env.owned_ref = env.manifest.add({"version": 1, "issuer": "synthetic-test-only",
                "fixture_id": str(env.fixture_id), "account_id": H_ACCOUNT, "control_data_type": CONTROL,
                "original_target_sha256": env.original.sha256, "owned_type_ids": [CONTROL],
                "journal_binding": ["synthetic-fingerprint", "resolved", CONTROL], "control_schema_ref": h_ref(schema.ref)})
        binding = env.resolver.resolve(env.owned_ref)
        proof_context = replace(schema_context, observation_id=uuid4())
        return make_fixture_policy_proof(owned_binding=binding, resolver=env.resolver,
            phase=phase, schema=schema, context=proof_context, on_observation=on_observation,
            request_budget=request_budget, request_guard=request_guard)

    def observe_exclusions(self, data_type, record_ids, *, context, binding_before,
                           revalidate_binding, on_observation, request_budget, request_guard):
        from fulcra_common.record_exclusions import _validated_exclusion_rows
        from fulcra_common.v1 import _json_equal
        env, projection = self.env, self.projection()
        assert data_type == DESTINATION and record_ids == (H_ROW["id"],)
        control_context = replace(context, observation_id=uuid4(), purpose="policy-read",
            destination_data_type=CONTROL, requested_target_ids=tuple(p[1] for p in projection),
            control_projection=projection)
        read = env.client.observe_records(CONTROL, tuple(str(p[1]) for p in projection),
            account_id=H_ACCOUNT, context=control_context, on_observation=on_observation,
            request_budget=request_budget, request_guard=request_guard)
        rows = _validated_exclusion_rows(CONTROL, DESTINATION, {str(b): str(a) for a, b in projection},
            {r["id"]: r for r in read.records}, account_id=H_ACCOUNT)
        after = revalidate_binding()
        b, a = binding_before.document["details"], after.document["details"]
        before_schema = env.retained.lookup(h_evidence(b["control_schema_ref"])).observation
        after_schema = env.retained.lookup(h_evidence(a["control_schema_ref"])).observation
        if (b["phase"] != "before" or a["phase"] != "after" or binding_before.ref == after.ref
                or before_schema.ref == after_schema.ref or b["owned_binding_ref"] != a["owned_binding_ref"]
                or not _json_equal(before_schema.schema, after_schema.schema)):
            raise ValueError("fixture policy fresh phases/schema differ")
        policy_context = replace(context, observation_id=uuid4(), purpose="policy-read", control_projection=projection)
        reservation = request_budget.reserve_events(1)
        reservation.start()
        result = ro._retain(ro.PolicyObservation, policy_context, H_STAMP, H_STAMP,
            env.policy_mutator({"target_to_control_ids": [[str(a), str(b)] for a, b in projection],
             "valid_exclusion_rows": list(rows.values()), "excluded_target_ids": list(rows),
             "record_read_ref": h_ref(read.ref), "binding_before_ref": h_ref(binding_before.ref),
             "binding_after_ref": h_ref(after.ref), "validation": "validated-permanent-policy-rows"}),
            reservation, on_observation)
        chain = [before_schema, binding_before, read, after_schema, after, result]
        if len({o.ref for o in chain}) != 6 or sum(len(o.canonical) for o in chain) > 8 * 1024 * 1024:
            raise ValueError("fixture policy chain cap exceeded")
        return result


class FixtureAssessor:
    def __init__(self, env):
        self.env = env
        self.control_coverage = self.target_coverage = "authoritative-for-requested-ids"
        self.mutate_assessment = lambda a: a

    def check_current(self, *, authority, ownership_proof, original, stage):
        env = self.env
        env.trace.append("current:" + stage)
        env.on_current(stage)
        if not env.active:
            raise ValueError("synthetic authority expired")
        if authority != env.authority.canonical or ownership_proof != env.ownership.canonical or original.canonical != env.original.canonical:
            raise ValueError("synthetic authority/ownership bytes differ")
        if (h_bytes(env.manifest.resolve(env.authority_ref)) != authority
                or h_bytes(env.manifest.resolve(env.ownership_ref)) != ownership_proof):
            raise ValueError("synthetic authority/ownership manifest differs")
        if env.dispatch.state != env.retained._load()["dispatch"]:
            raise ValueError("synthetic original dispatch differs from retained state")
        auth = ro._document(authority)
        ro._fields(auth, "version issuer fixture_id original_target_sha256 read_contract_ref deployment_binding_ref valid_from valid_until replay_disposition_ref external_history_ref")
        owner = ro._document(ownership_proof)
        ro._fields(owner, "issuer fixture_id proof")
        if (auth["issuer"] != owner["issuer"] or auth["issuer"] != "synthetic-test-only"
                or auth["fixture_id"] != owner["fixture_id"] or auth["original_target_sha256"] != original.sha256
                or not auth["valid_from"] <= env.now < auth["valid_until"]):
            raise ValueError("synthetic authority scope/interval differs")
        for name in ("read_contract_ref", "deployment_binding_ref", "replay_disposition_ref", "external_history_ref"):
            value = auth[name]
            ref = ro.ArtifactRef(UUID(value["artifact_id"]), value["sha256"])
            resolved = env.manifest.resolve(ref)
            if name == "read_contract_ref":
                binding = env.manifest.resolve(env.contract_binding)
                if (ref != env.contract_ref or resolved != {"kind": "synthetic-read-contract", "account_id": H_ACCOUNT}
                        or binding["original_target_sha256"] != original.sha256 or binding["contract_ref"] != value):
                    raise ValueError("synthetic read-contract binding differs")
            elif resolved["original_target_sha256"] != original.sha256:
                raise ValueError("synthetic referenced artifact scope differs")
        proof = owner["proof"]
        ro._fields(proof, "version proof_id account_id destination_data_type target_ids mechanism authority_ref owner_id issued_at valid_until writer_inventory_ref fencing_evidence_refs inflight_resolution_refs external_attempt_history_ref scope_sha256")
        if (proof["account_id"] != H_ACCOUNT or proof["destination_data_type"] != DESTINATION
                or proof["target_ids"] != [H_ROW["id"]] or proof["scope_sha256"] != original.sha256
                or not proof["issued_at"] <= env.now < proof["valid_until"]):
            raise ValueError("synthetic target ownership differs")
        for value in [proof["authority_ref"], proof["writer_inventory_ref"], proof["external_attempt_history_ref"],
                      *proof["fencing_evidence_refs"], *proof["inflight_resolution_refs"]]:
            resolved = env.manifest.resolve(ro.ArtifactRef(UUID(value["artifact_id"]), value["sha256"]))
            if resolved["original_target_sha256"] != original.sha256:
                raise ValueError("synthetic ownership artifact scope differs")
        if not env.fenced or not env.replay_resolved:
            raise ValueError("synthetic fencing/replay disposition UNKNOWN")
        if self.env.manifest.resolve(env.external_ref) != env.external_document:
            raise ValueError("synthetic external history UNKNOWN")

    def assess(self, observation, *, authority, ownership_proof):
        self.check_current(authority=authority, ownership_proof=ownership_proof,
                           original=self.env.original, stage="before-read")
        doc = observation.document
        coverage = self.control_coverage if doc["purpose"] == "policy-read" else self.target_coverage
        result = ro.ReadAssessment(observation.ref, h_scope_hash(observation), self.env.contract_ref,
            self.env.deployment_ref, H_STAMP, H_END, coverage)
        return self.mutate_assessment(result)


@pytest.fixture
def historical_deny_io(monkeypatch):
    from fulcra_common import data_types, v1
    from fulcra_common.client import BaseFulcraClient
    def denied(*args, **kwargs):
        raise AssertionError("unstubbed external historical I/O")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(BaseFulcraClient, "get_token", denied)
    monkeypatch.setattr(ro, "_now", lambda: H_STAMP)
    monkeypatch.setattr(v1, "_now", lambda: H_STAMP)
    monkeypatch.setattr(data_types, "_now", lambda: H_STAMP)


class HistoricalEnvironment:
    def __init__(self, monkeypatch, *, present=False, excluded=False, event_limit=128):
        from fulcra_common import data_types
        from fulcra_common.client import BaseFulcraClient
        from fulcra_common.record_delivery import SourceDispatch, prepared_source_dispatch
        from fulcra_common.v1 import V1DataClient
        self.trace, self.requests, self.outcomes, self.checkpoints = [], [], [], []
        self.active, self.fenced, self.replay_resolved = True, True, True
        self.now, self.fixture_id, self.owned_ref = H_STAMP, uuid4(), None
        self.owned_ids, self.journal_binding = [CONTROL], ["synthetic-fingerprint", "resolved", CONTROL]
        self.on_current = lambda stage: None
        self.on_owned_list = lambda n, phase: None
        self.after_account = lambda: None
        self.after_sink = lambda observation: None
        self.after_http = lambda request: None
        self.after_checkpoint = lambda: None
        self.after_outcome = lambda: None
        self.mutate_event = lambda event: event
        self.mutate_commit = lambda commit: commit
        self.policy_mutator = lambda details: details
        self.before_callback = lambda: None
        self.control_rows = {}
        self.target_rows = {H_ROW["id"]: deepcopy(H_ROW)} if present else {}
        self.manifest = FixtureManifest()
        original_hash = h_hash(h_bytes({"payload": H_ROW, "schema": H_SCHEMA, "fixture_id": str(self.fixture_id)}))
        attempt_id = uuid4()
        # A fixture contract references the anchor by its exact immutable bytes;
        # the original's read-contract identity itself is fixed before hashing.
        contract_id = uuid4()
        contract_bytes = h_bytes({"kind": "synthetic-read-contract", "account_id": H_ACCOUNT})
        initial_contract = ro.ArtifactRef(contract_id, h_hash(contract_bytes))
        self.original = ro.OriginalTargetContext(h_bytes({"version": 1, "attempt_id": str(attempt_id),
            "original_hash": original_hash, "account_id": H_ACCOUNT, "destination_data_type": DESTINATION,
            "requested_target_ids": [H_ROW["id"]], "read_contract_ref": h_ref(initial_contract)}))
        # The contract has an exact scoped anchor field via a separate manifest
        # binding document; avoid self-referential content hashing.
        self.contract_ref = initial_contract
        self.manifest.documents[initial_contract] = contract_bytes
        self.contract_binding = self.manifest.add({"kind": "contract-binding", "original_target_sha256": self.original.sha256,
                                                  "contract_ref": h_ref(initial_contract)})
        def artifact(kind, **extra):
            return self.manifest.add({"kind": kind, "original_target_sha256": self.original.sha256, **extra})
        self.deployment_ref = artifact("synthetic-deployment")
        self.replay_ref = artifact("synthetic-replay-disposition", disposition="never-created")
        self.external_document = {"kind": "synthetic-external-history", "original_target_sha256": self.original.sha256,
                                  "complete": True, "issued_events": []}
        self.external_ref = self.manifest.add(self.external_document)
        inventory, fence, drain = (artifact(k) for k in ("synthetic-inventory", "synthetic-fence", "synthetic-drain"))
        auth_ref = artifact("synthetic-authority-root")
        self.authority = SyntheticAuthority(h_bytes({"version": 1, "issuer": "synthetic-test-only", "fixture_id": str(self.fixture_id),
            "original_target_sha256": self.original.sha256, "read_contract_ref": h_ref(self.contract_ref),
            "deployment_binding_ref": h_ref(self.deployment_ref), "valid_from": H_STAMP, "valid_until": H_END,
            "replay_disposition_ref": h_ref(self.replay_ref), "external_history_ref": h_ref(self.external_ref)}))
        self.ownership = SyntheticOwnership(h_bytes({"issuer": "synthetic-test-only", "fixture_id": str(self.fixture_id),
            "proof": {"version": 1, "proof_id": str(uuid4()), "account_id": H_ACCOUNT, "destination_data_type": DESTINATION,
                "target_ids": [H_ROW["id"]], "mechanism": "synthetic-all-target-fence", "authority_ref": h_ref(auth_ref),
                "owner_id": "synthetic-test-only", "issued_at": H_STAMP, "valid_until": H_END,
                "writer_inventory_ref": h_ref(inventory), "fencing_evidence_refs": [h_ref(fence)],
                "inflight_resolution_refs": [h_ref(drain)], "external_attempt_history_ref": h_ref(self.external_ref),
                "scope_sha256": self.original.sha256}}))
        self.authority_ref = self.manifest.add(json.loads(self.authority.canonical))
        self.ownership_ref = self.manifest.add(json.loads(self.ownership.canonical))
        self.context = ro.ObservationContext(version=1, observation_id=uuid4(), attempt_id=attempt_id,
            original_hash=original_hash, account_id=H_ACCOUNT, destination_data_type=DESTINATION,
            requested_target_ids=(UUID(H_ROW["id"]),), purpose="target-read", read_contract_ref=self.contract_ref,
            original_target=self.original, control_projection=())
        self.dispatch = SourceDispatch("synthetic-bound-original", [H_ROW],
            prepared_source_dispatch("synthetic-bound-original", [H_ROW]),
            lambda state: pytest.fail("historical path called live SourceDispatch.persist"))
        self.retained = FixtureRetained(self)
        self.budget = ro.RequestBudget(500, retained=self.retained, original=self.original, event_limit=event_limit)
        env = self
        class Credentials:
            def __init__(self, *, access_token):
                assert access_token == "synthetic-historical-token"
        class API:
            def __init__(self, *, credentials):
                assert isinstance(credentials, Credentials)
            def get_fulcra_userid(self):
                env.trace.append("sdk")
                env.after_account()
                return H_ACCOUNT
        class Client(BaseFulcraClient):
            def get_token(self):
                env.trace.append("token")
                return "synthetic-historical-token"
        monkeypatch.setattr(data_types, "FulcraCredentials", Credentials)
        monkeypatch.setattr(data_types, "FulcraAPI", API)
        def transport(request):
            self.requests.append(request)
            self.trace.append(request.method + ":" + request.url.path)
            if request.method == "GET" and request.url.path.endswith("/schema"):
                response = httpx.Response(200, json=H_SCHEMA)
            elif request.method == "GET":
                rows = self.control_rows if CONTROL in request.url.params.get("q", "") else self.target_rows
                response = httpx.Response(200, content=b"\n".join(h_bytes(row) for row in rows.values()))
            elif request.method == "POST":
                rows = [json.loads(line) for line in request.content.splitlines()]
                self.target_rows.update({r["id"]: r for r in rows})
                response = httpx.Response(200, content=b'{ "upload_id" : "synthetic-only", "n": 1 }')
            else:
                raise AssertionError("historical repair/delete reached transport")
            self.after_http(request)
            return response
        self.client = V1DataClient(Client(base_url="https://synthetic.invalid", transport=httpx.MockTransport(transport)))
        def forbidden_live(*args, **kwargs):
            raise AssertionError("historical path reached live read/ingest/native repair")
        self.client.get_records = forbidden_live
        self.client.ingest = forbidden_live
        self.client.delete_records = forbidden_live
        self.policy, self.assessor = FixturePolicy(self), FixtureAssessor(self)
        self.resolver = FixtureOwnedResolver(self)
        if excluded:
            from fulcra_common.record_exclusions import _record
            row = _record(CONTROL, DESTINATION, H_ROW["id"], action_id=str(uuid4()),
                requested_at=datetime(2026, 1, 2, tzinfo=timezone.utc), reason="operator", evidence=["synthetic-marker"])
            self.control_rows[row["id"]] = row

    def sink(self, observation):
        self.trace.append("sink:" + observation.kind)
        commit = self.retained.sink(observation)
        self.after_sink(observation)
        return commit

    def start_new_attempt(self):
        """Reopen the SAME retained target log with a new synthetic attempt."""
        previous = self.original.sha256
        doc = self.original.document
        doc["attempt_id"] = str(uuid4())
        self.original = ro.OriginalTargetContext(h_bytes(doc))
        self.context = replace(self.context, observation_id=uuid4(), attempt_id=UUID(doc["attempt_id"]),
                               original_target=self.original)
        mapping = {}
        for ref, body in list(self.manifest.documents.items()):
            value = json.loads(body)
            if value.get("original_target_sha256") == previous and value.get("kind"):
                value["original_target_sha256"] = self.original.sha256
                mapping[ref] = self.manifest.add(value)
        def mapped(value):
            ref = ro.ArtifactRef(UUID(value["artifact_id"]), value["sha256"])
            return h_ref(mapping.get(ref, ref))
        self.contract_binding = mapping[self.contract_binding]
        self.deployment_ref, self.replay_ref = mapping[self.deployment_ref], mapping[self.replay_ref]
        self.external_ref = mapping[self.external_ref]
        self.external_document = self.manifest.resolve(self.external_ref)
        authority = json.loads(self.authority.canonical)
        authority["original_target_sha256"] = self.original.sha256
        for key in ("deployment_binding_ref", "replay_disposition_ref", "external_history_ref"):
            authority[key] = mapped(authority[key])
        self.authority = SyntheticAuthority(h_bytes(authority))
        ownership = json.loads(self.ownership.canonical)
        proof = ownership["proof"]
        proof["scope_sha256"] = self.original.sha256
        for key in ("authority_ref", "writer_inventory_ref", "external_attempt_history_ref"):
            proof[key] = mapped(proof[key])
        for key in ("fencing_evidence_refs", "inflight_resolution_refs"):
            proof[key] = [mapped(value) for value in proof[key]]
        self.ownership = SyntheticOwnership(h_bytes(ownership))
        self.authority_ref = self.manifest.add(json.loads(self.authority.canonical))
        self.ownership_ref = self.manifest.add(json.loads(self.ownership.canonical))
        self.owned_ref = None
        self.retained = FixtureRetained(self, self.retained.journal)
        self.budget = ro.RequestBudget(500, retained=self.retained, original=self.original)

    def lifecycle_scope(self):
        return {"account_id": H_ACCOUNT, "destination_data_type": DESTINATION, "target_ids": [H_ROW["id"]],
            "original_target_context_sha256": self.original.sha256, "expected_schema_sha256": h_hash(h_bytes(H_SCHEMA)),
            "authority_sha256": h_hash(self.authority.canonical), "ownership_sha256": h_hash(self.ownership.canonical)}

    def _lifecycle(self, kind, result, details, proofs):
        return FixtureLifecycleEvent(h_bytes({"version": 1, "event_id": str(uuid4()),
            "attempt_id": str(self.context.attempt_id), "original_hash": self.context.original_hash,
            "at": H_STAMP, "kind": kind, "scope": self.lifecycle_scope(), "result": result,
            "proof_refs": proofs, "details": details}))

    def _commit_lifecycle(self, event, kind, expected_dispatch, expected_outcome):
        self.budget.assert_bound(self.retained, self.original)
        reservation = self.budget.reserve_events(1)
        reservation.start()
        event = self.mutate_event(event)
        reservation.consume(kind, event_id=event.ref.event_id, event_sha256=event.ref.sha256)
        commit = self.mutate_commit(self.retained.append_lifecycle(event))
        # This is a separate fresh read, not the append's event echo.
        readback = self.retained.lookup_lifecycle(event.ref)
        if (readback.event.canonical != event.canonical or readback.commit != commit
                or commit.ref != event.ref or commit.original_hash != self.context.original_hash
                or type(commit.ledger_revision) is not int
                or commit.ledger_revision != event.document["details"]["expected_revision"] + 1
                or h_hash(readback.state_bytes) != commit.state_sha256):
            raise ValueError("fixture callback acknowledgement/readback differs")
        state = self.retained._load()
        expected = {"version": 1, "attempt_id": str(self.context.attempt_id), "original_hash": self.context.original_hash,
            "original_target_context_sha256": self.original.sha256, "dispatch": expected_dispatch,
            "outcome": expected_outcome, "ledger_revision": commit.ledger_revision,
            "last_event_ref": {"event_id": str(event.ref.event_id), "sha256": event.ref.sha256}}
        if readback.state_bytes != h_bytes(expected) or self.retained._state_bytes(state) != readback.state_bytes:
            raise ValueError("fixture callback live state/readback differs")
        self.retained.reload_history(self.original)
        return None

    def before_submit(self, history):
        from fulcra_common.record_delivery import _dispatch_digest, _source_body_digest
        self.before_callback()
        self.checkpoints.append(history)
        before = deepcopy(self.dispatch.state)
        issued = {**before, "phase": "issued", "issued_records": [deepcopy(H_ROW)],
                  "body_sha256": _source_body_digest([H_ROW])}
        details = {"expected_revision": history.evidence_revision, "replay": h_replay(history),
            "dispatch_before_sha256": _dispatch_digest(before), "dispatch_issued_sha256": _dispatch_digest(issued),
            "body_sha256": _source_body_digest([H_ROW])}
        event = self._lifecycle("dispatch-issued", "issued", details, self.retained.proof_refs())
        self._commit_lifecycle(event, "issued-checkpoint", issued, "unknown")
        self.after_checkpoint()

    def persist_outcome(self, delivery, history):
        from fulcra_common.record_delivery import _dispatch_digest
        self.outcomes.append((delivery, history))
        result = next(name for name in ("verified", "excluded", "held", "conflict", "unknown")
                      if getattr(delivery, name + "_ids"))
        proofs = {"observations": [h_ref(r) for r in delivery.evidence_refs],
                  "assessments": [h_ref(r) for r in delivery.assessment_refs]}
        target, policy, _ = self.retained._facts(proofs)
        reason = ("excluded-pending-repair" if target and target.observation.records and policy
                  and policy.observation.document["details"]["excluded_target_ids"] else None)
        details = {"expected_revision": history.evidence_revision, "replay": h_replay(history),
            "dispatch_sha256": _dispatch_digest(self.dispatch.state), "delivery": h_delivery(delivery), "reason": reason}
        event = self._lifecycle("outcome", result, details, proofs)
        self._commit_lifecycle(event, "outcome", deepcopy(self.dispatch.state), result)
        self.after_outcome()

    def reconcile(self, **overrides):
        from fulcra_common import record_delivery as module
        reconcile = getattr(module, "reconcile_historical_records", None)
        assert callable(reconcile), "unused historical reconciler missing"
        kwargs = dict(dispatch=self.dispatch, history=self.retained.reload_history(self.original), assessment=self.assessor,
            authority=self.authority.canonical, ownership_proof=self.ownership.canonical, expected_schema=H_SCHEMA,
            context=self.context, evidence_sink=self.sink, retained=self.retained,
            decision_sink=self.retained.decision_sink, request_budget=self.budget,
            before_submit=self.before_submit, persist_outcome=self.persist_outcome)
        kwargs.update(overrides)
        return reconcile(self.client, self.policy, DESTINATION, [deepcopy(H_ROW)], **kwargs)


@pytest.mark.parametrize("scenario,expected", [("matching", "verified"), ("excluded", "excluded"),
    ("revival", "held"), ("mismatch", "conflict"), ("coverage", "unknown")])
def test_historical_five_outcomes_commit_once_before_return(historical_deny_io, monkeypatch, scenario, expected):
    env = HistoricalEnvironment(monkeypatch, present=scenario in {"matching", "revival", "mismatch"},
                                excluded=scenario in {"excluded", "revival"})
    if scenario == "mismatch":
        env.target_rows[H_ROW["id"]]["note"] = "different original payload"
    if scenario == "coverage":
        env.assessor.control_coverage = "unknown"
    result = env.reconcile()
    assert getattr(result, expected + "_ids") == (UUID(H_ROW["id"]),)
    assert sum(len(getattr(result, k + "_ids")) for k in ("verified", "excluded", "held", "conflict", "unknown")) == 1
    assert result.historical_complete is False
    assert len(env.outcomes) == 1 and env.outcomes[0][0] is result
    assert env.retained._load()["outcome"] == expected
    assert not [r for r in env.requests if r.method == "POST"]
    phases = env.policy.calls
    assert [p[0] for p in phases] == ["before", "after"]
    assert phases[0][1] is phases[1][1] and phases[0][2] is phases[1][2] is env.budget
    assert phases[0][3] is phases[1][3] and phases[0][4] == phases[1][4] == env.original.canonical
    saved = [env.retained._decode_event(env.retained._load(), e)[0] for e in env.retained._load()["events"]]
    proofs = [o for o in saved if isinstance(o, ro.PolicyBindingProof)]
    assert len({p.ref for p in proofs}) == 2
    assert len({p.document["details"]["control_schema_ref"]["observation_id"] for p in proofs}) == 2


def test_historical_authorized_synthetic_first_write_has_one_post_and_independent_hashes(historical_deny_io, monkeypatch):
    from fulcra_common.record_delivery import _source_body_digest
    env = HistoricalEnvironment(monkeypatch)
    result = env.reconcile(allow_submit=True)
    posts = [r for r in env.requests if r.method == "POST"]
    assert len(posts) == 1 and len(env.checkpoints) == len(env.outcomes) == 1
    assert result.verified_ids == (UUID(H_ROW["id"]),)
    assert env.dispatch.state["phase"] == "issued"
    assert env.retained.reload_history(env.original).ever_positive
    receipt = next(env.retained.lookup(r).observation for r in result.evidence_refs
                   if isinstance(env.retained.lookup(r).observation, ro.UploadReceiptObservation))
    assert receipt.document["details"]["request_binding"]["body_sha256"] == h_hash(posts[0].content)
    assert _source_body_digest([H_ROW]) == h_hash(posts[0].content)
    assert posts[0].content != h_bytes(H_ROW)  # existing spaces/ASCII escaping must stay intact
    lifecycle = [e for e in env.retained._load()["events"] if e["kind"] == "issued-checkpoint"]
    checkpoint = env.retained.lookup_lifecycle(FixtureEventRef(UUID(lifecycle[0]["event_id"]), lifecycle[0]["sha256"]))
    assert checkpoint.commit.state_sha256 == h_hash(checkpoint.state_bytes)
    assert checkpoint.commit.state_sha256 != h_hash(posts[0].content)


@pytest.mark.parametrize("capability", ["binding_proof", "observe_exclusions"])
def test_historical_missing_policy_capability_fails_before_credentials(historical_deny_io, monkeypatch, capability):
    env = HistoricalEnvironment(monkeypatch)
    setattr(env.policy, capability, None)
    with pytest.raises(ValueError, match="historical"):
        env.reconcile(allow_submit=True)
    assert not env.requests and "token" not in env.trace


@pytest.mark.parametrize("callback", ["before_submit", "persist_outcome", "evidence_sink", "decision_sink"])
def test_historical_missing_callback_fails_before_credentials(historical_deny_io, monkeypatch, callback):
    env = HistoricalEnvironment(monkeypatch)
    with pytest.raises(ValueError, match="historical"):
        env.reconcile(**{callback: None})
    assert not env.requests and "token" not in env.trace


@pytest.mark.parametrize("coverage", ["target", "control"])
def test_historical_target_and_control_coverage_are_separate(historical_deny_io, monkeypatch, coverage):
    env = HistoricalEnvironment(monkeypatch)
    setattr(env.assessor, coverage + "_coverage", "unknown")
    result = env.reconcile(allow_submit=True)
    assert result.unknown_ids == (UUID(H_ROW["id"]),)
    assert not env.checkpoints and not [r for r in env.requests if r.method == "POST"]
    assert len(result.assessment_refs) == 2


@pytest.mark.parametrize("mutation", ["scope", "observation", "contract"])
def test_historical_foreign_assessment_cannot_authorize_absence(historical_deny_io, monkeypatch, mutation):
    env = HistoricalEnvironment(monkeypatch)
    def mutate(a):
        if mutation == "scope":
            return replace(a, scope_hash="b" * 64)
        if mutation == "observation":
            return replace(a, observation_ref=ro.EvidenceRef(uuid4(), "b" * 64))
        return replace(a, read_contract_ref=ro.ArtifactRef(uuid4(), "b" * 64))
    env.assessor.mutate_assessment = mutate
    with pytest.raises(ValueError, match="assessment|scope|contract"):
        env.reconcile(allow_submit=True)
    assert not env.checkpoints and not [r for r in env.requests if r.method == "POST"]


@pytest.mark.parametrize("history_kind", ["positive", "issued", "audit-positive", "terminal-positive"])
def test_historical_retained_safety_evidence_survives_delete_reopen(historical_deny_io, monkeypatch, history_kind):
    env = HistoricalEnvironment(monkeypatch, present="positive" in history_kind)
    if history_kind == "issued":
        env.after_checkpoint = lambda: setattr(env, "active", False)
        with pytest.raises(ValueError, match="expired"):
            env.reconcile(allow_submit=True)
        env.active, env.after_checkpoint = True, lambda: None
    elif history_kind == "audit-positive":
        env.after_http = lambda request: setattr(env, "active", False) if "/records" in request.url.path and CONTROL not in request.url.params.get("q", "") else None
        with pytest.raises(ValueError, match="expired"):
            env.reconcile()
        env.active, env.after_http = True, lambda request: None
    else:
        env.reconcile()
    env.target_rows.clear()
    env.retained = FixtureRetained(env, env.retained.journal)
    env.budget = ro.RequestBudget(500, retained=env.retained, original=env.original)
    result = env.reconcile(allow_submit=True)
    assert result.unknown_ids == (UUID(H_ROW["id"]),)
    assert not [r for r in env.requests if r.method == "POST"]
    assert env.retained.reload_history(env.original).ever_positive == (history_kind != "issued")


@pytest.mark.parametrize("phase", ["issued-checkpoint", "outcome"])
def test_historical_written_then_raised_lifecycle_never_returns_or_retries(historical_deny_io, monkeypatch, phase):
    env = HistoricalEnvironment(monkeypatch, present=phase == "outcome")
    env.retained.fail_after_append = phase
    with pytest.raises(RuntimeError, match="written then raised"):
        env.reconcile(allow_submit=True)
    assert not [r for r in env.requests if r.method == "POST"]
    state = env.retained._load()
    assert sum(e["kind"] == phase for e in state["events"]) == 1
    reopened = FixtureRetained(env, env.retained.journal)
    history = reopened.reload_history(env.original)
    assert history.issued_seen if phase == "issued-checkpoint" else history.ever_positive


@pytest.mark.parametrize("phase", ["account", "before-schema", "control", "final-schema", "issued", "receipt", "outcome"])
def test_historical_expiry_at_real_nested_boundaries(historical_deny_io, monkeypatch, phase):
    env = HistoricalEnvironment(monkeypatch)
    if phase == "account":
        env.after_account = lambda: setattr(env, "active", False)
    elif phase in {"before-schema", "control", "final-schema", "receipt"}:
        def expire(observation):
            doc = observation.document
            hit = ((phase == "before-schema" and observation.kind == "served-schema" and doc["destination_data_type"] == CONTROL)
                or (phase == "control" and observation.kind == "exact-read" and doc["purpose"] == "policy-read")
                or (phase == "final-schema" and observation.kind == "served-schema" and doc["destination_data_type"] == DESTINATION)
                or (phase == "receipt" and observation.kind == "upload-receipt"))
            if hit:
                env.active = False
        env.after_sink = expire
    elif phase == "issued":
        env.after_checkpoint = lambda: setattr(env, "active", False)
    else:
        env.target_rows[H_ROW["id"]] = deepcopy(H_ROW)
        env.after_outcome = lambda: setattr(env, "active", False)
    with pytest.raises(ValueError, match="expired"):
        env.reconcile(allow_submit=True)
    posts = [r for r in env.requests if r.method == "POST"]
    assert len(posts) == (1 if phase == "receipt" else 0)
    if phase == "account":
        assert not env.requests
    if phase == "control":
        assert len(env.requests) == 3  # target, before-schema, complete control response
    if phase == "receipt":
        receipts = [e for e in env.retained._load()["events"] if '"kind":"upload-receipt"' in e["canonical"]]
        assert len(receipts) == 1
        # Expiry here is after the sink's stored acknowledgement; a separate
        # during-response test below verifies audit-only receipt metadata.
    if phase == "issued":
        assert env.retained.reload_history(env.original).issued_seen


def test_historical_control_rows_cannot_supply_target_positive_history(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch, excluded=True)
    result = env.reconcile()
    assert result.excluded_ids == (UUID(H_ROW["id"]),)
    assert not env.retained.reload_history(env.original).ever_positive


def test_historical_visible_mismatch_is_retained_without_matching_positive(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch, present=True)
    env.target_rows[H_ROW["id"]]["note"] = "conflict"
    result = env.reconcile(allow_submit=True)
    assert result.conflict_ids == (UUID(H_ROW["id"]),)
    assert not env.retained.reload_history(env.original).ever_positive
    actual = [env.retained.lookup(r).observation.records for r in result.evidence_refs
              if isinstance(env.retained.lookup(r).observation, ro.ExactReadObservation)
              and env.retained.lookup(r).observation.document["purpose"] == "target-read"]
    assert actual == [({"id": H_ROW["id"], "note": "conflict"},)]
    assert not [r for r in env.requests if r.method == "POST"]


def test_historical_old_audit_positive_does_not_replace_fresh_verification_support(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch, present=True)
    env.after_http = lambda request: setattr(env, "active", False) if "/records" in request.url.path and CONTROL not in request.url.params.get("q", "") else None
    with pytest.raises(ValueError, match="expired"):
        env.reconcile()
    env.active, env.after_http = True, lambda request: None
    result = env.reconcile()
    assert result.verified_ids == (UUID(H_ROW["id"]),)
    assert not [r for r in env.requests if r.method == "POST"]


@pytest.mark.parametrize("count,stage", [(127, "read"), (128, "none")])
def test_historical_reopened_attempt_allowance_counts_real_retained_events(historical_deny_io, monkeypatch, count, stage):
    env = HistoricalEnvironment(monkeypatch)
    def guard(**kwargs):
        return None
    for _ in range(count):
        env.client.observe_records(DESTINATION, (H_ROW["id"],), account_id=H_ACCOUNT,
            context=replace(env.context, observation_id=uuid4()), on_observation=env.sink,
            request_budget=env.budget, request_guard=guard)
    journal = env.retained.journal
    assert len(env.retained._load()["events"]) == count
    assert env.retained._load()["accounting_revision"] > count
    env.retained = FixtureRetained(env, journal)
    env.budget = ro.RequestBudget(500, retained=env.retained, original=env.original)
    before = len(env.requests)
    with pytest.raises(ValueError, match="allowance"):
        env.reconcile(allow_submit=True)
    assert len(env.requests) - before == (1 if stage == "read" else 0)
    if count == 128:
        assert env.budget._remaining == 500


def test_historical_shared_handles_cannot_overbook_or_refund_started_slots(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch, event_limit=2)
    other = FixtureRetained(env, env.retained.journal)
    budget = ro.RequestBudget(10, retained=other, original=env.original, event_limit=2)
    held = env.budget.reserve_events(1)
    started = budget.reserve_events(1)
    started.start()
    with pytest.raises(ValueError, match="allowance"):
        env.budget.reserve_events(1)
    held.release_unused()
    replacement = budget.reserve_events(1)
    with pytest.raises(ValueError, match="release"):
        started.release_unused()
    replacement.start()
    with pytest.raises(ValueError, match="allowance"):
        ro.RequestBudget(10, retained=other, original=env.original, event_limit=2).reserve_events(1)


@pytest.mark.parametrize("mutation", ["missing-slots", "missing-events", "regressed", "uncertain"])
def test_historical_corrupt_or_uncertain_journal_prevents_preparation(historical_deny_io, monkeypatch, mutation):
    env = HistoricalEnvironment(monkeypatch)
    env.budget.reserve_events(1)
    state = json.loads(env.retained.journal.bytes)
    if mutation == "missing-slots":
        del state["slots"]
    elif mutation == "missing-events":
        del state["events"]
    elif mutation == "regressed":
        state["revision"] = -1
    else:
        state["slots"][0]["state"] = "uncertain"
    env.retained.journal.bytes = h_bytes(state)
    with pytest.raises((ValueError, KeyError)):
        env.reconcile(allow_submit=True)
    assert not env.requests and "token" not in env.trace


@pytest.mark.parametrize("limit", [True, 0, 129])
def test_historical_invalid_event_ceiling_rejected_without_binding_or_credentials(historical_deny_io, monkeypatch, limit):
    env = HistoricalEnvironment(monkeypatch)
    before = env.retained.journal.bytes
    with pytest.raises(ValueError):
        ro.RequestBudget(10, retained=env.retained, original=env.original, event_limit=limit)
    assert env.retained.journal.bytes == before and not env.trace


@pytest.mark.parametrize("request_limit", [0, 1, 2])
def test_historical_exact_request_budget_stops_at_library_call_boundary(historical_deny_io, monkeypatch, request_limit):
    env = HistoricalEnvironment(monkeypatch)
    env.budget = ro.RequestBudget(request_limit, retained=env.retained, original=env.original)
    with pytest.raises(ValueError, match="budget"):
        env.reconcile(allow_submit=True)
    assert env.trace.count("sdk") == (0 if request_limit == 0 else 1)
    assert len(env.requests) == (1 if request_limit == 2 else 0)
    assert env.budget._remaining == 0


def test_historical_exact_nested_request_and_event_counts(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch)
    env.reconcile(allow_submit=True)
    assert len(env.requests) == 10  # nine GETs plus the single POST
    assert env.trace.count("sdk") == 22
    assert env.trace.count("owned-list") == 8
    assert env.budget._remaining == 460  # each SDK, owned-list and HTTP call
    state = env.retained._load()
    assert len(state["events"]) == 22
    assert len([s for s in state["slots"] if s["state"] != "released"]) == 22


@pytest.mark.parametrize("phase", ["issued-checkpoint", "outcome"])
@pytest.mark.parametrize("fault", ["ack", "readback", "state", "non-none"])
def test_historical_lifecycle_uncertainty_never_returns_delivery(historical_deny_io, monkeypatch, phase, fault):
    env = HistoricalEnvironment(monkeypatch, present=phase == "outcome")
    if fault == "ack":
        env.mutate_commit = lambda c: replace(c, state_sha256="b" * 64)
    elif fault == "readback":
        real_lookup = env.retained.lookup_lifecycle
        def wrong(ref):
            value = real_lookup(ref)
            return replace(value, event=env._lifecycle(value.event.document["kind"], value.event.document["result"],
                value.event.document["details"], value.event.document["proof_refs"]))
        env.retained.lookup_lifecycle = wrong
    elif fault == "state":
        real_lookup = env.retained.lookup_lifecycle
        def wrong_state(ref):
            value = real_lookup(ref)
            return replace(value, state_bytes=b"{}")
        env.retained.lookup_lifecycle = wrong_state
    else:
        if phase == "issued-checkpoint":
            real = env.before_submit
            env.before_submit = lambda h: (real(h), True)[1]
        else:
            real = env.persist_outcome
            env.persist_outcome = lambda d, h: (real(d, h), True)[1]
    with pytest.raises(ValueError, match="acknowledgement|readback|None"):
        env.reconcile(allow_submit=True)
    assert not [r for r in env.requests if r.method == "POST"]
    assert len([e for e in env.retained._load()["events"] if e["kind"] == phase]) == 1


@pytest.mark.parametrize("field", ["event_id", "attempt_id", "account", "expected_revision", "proofs", "body"])
def test_historical_checkpoint_exact_event_scope_and_cas_reject_mutation(historical_deny_io, monkeypatch, field):
    env = HistoricalEnvironment(monkeypatch)
    def mutate(event):
        doc = event.document
        if field == "event_id":
            # Claim identity is deliberately made different at the append boundary.
            real = env.retained.append_lifecycle
            def wrong_claim(value):
                other = value.document
                other["event_id"] = str(uuid4())
                return real(FixtureLifecycleEvent(h_bytes(other)))
            env.retained.append_lifecycle = wrong_claim
        elif field == "attempt_id":
            doc["attempt_id"] = str(uuid4())
        elif field == "account":
            doc["scope"]["account_id"] = "foreign-account"
        elif field == "expected_revision":
            doc["details"]["expected_revision"] = max(0, doc["details"]["expected_revision"] - 1)
            doc["details"]["replay"]["evidence_revision"] = doc["details"]["expected_revision"]
        elif field == "proofs":
            doc["proof_refs"]["observations"] = []
        else:
            doc["details"]["body_sha256"] = "b" * 64
        return FixtureLifecycleEvent(h_bytes(doc))
    env.mutate_event = mutate
    with pytest.raises(ValueError, match="fixture"):
        env.reconcile(allow_submit=True)
    assert not [r for r in env.requests if r.method == "POST"]


@pytest.mark.parametrize("stage", ["before-callback", "inside-cas"])
def test_historical_concurrent_matching_positive_refuses_issued_cas(historical_deny_io, monkeypatch, stage):
    env = HistoricalEnvironment(monkeypatch)
    def positive():
        env.target_rows[H_ROW["id"]] = deepcopy(H_ROW)
        # Retain through the genuine producer, not by setting a boolean/index.
        env.client.observe_records(DESTINATION, (H_ROW["id"],), account_id=H_ACCOUNT,
            context=replace(env.context, observation_id=uuid4()), on_observation=env.sink,
            request_budget=env.budget, request_guard=lambda **kwargs: None)
    if stage == "before-callback":
        env.before_callback = positive
    else:
        received, resume = Event(), Event()
        errors = []
        env.target_rows[H_ROW["id"]] = deepcopy(H_ROW)
        def pause_response(request):
            if current_thread().name == "synthetic-prior-read":
                received.set()
                assert resume.wait(10)
        env.after_http = pause_response
        def in_flight():
            try:
                env.client.observe_records(DESTINATION, (H_ROW["id"],), account_id=H_ACCOUNT,
                    context=replace(env.context, observation_id=uuid4()), on_observation=env.sink,
                    request_budget=env.budget, request_guard=lambda **kwargs: None)
            except Exception as error:
                errors.append(error)
        worker = Thread(target=in_flight, name="synthetic-prior-read")
        worker.start()
        assert received.wait(10)
        env.target_rows.clear()
        def during():
            resume.set()
            worker.join(10)
            assert not worker.is_alive() and not errors
        env.retained.before_cas = during
    try:
        with pytest.raises(ValueError, match="stale|uncertain|UNKNOWN"):
            env.reconcile(allow_submit=True)
    finally:
        if stage == "inside-cas":
            resume.set()
            worker.join(10)
    assert not [r for r in env.requests if r.method == "POST"]
    assert env.retained._load()["positive_refs"]


def test_historical_receipt_response_expiry_uses_preheld_audit_slot(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch)
    env.after_http = lambda r: setattr(env, "active", False) if r.method == "POST" else None
    with pytest.raises(ValueError, match="expired"):
        env.reconcile(allow_submit=True)
    state = env.retained._load()
    receipts = [e for e in state["events"] if e["kind"] == "observation"
                and json.loads(e["canonical"])["kind"] == "upload-receipt"]
    assert len(receipts) == 1 and receipts[0]["commit"]["retention_mode"] == "audit-only"
    assert len([r for r in env.requests if r.method == "POST"]) == 1
    assert env.dispatch.state["phase"] == "issued"


def test_historical_stale_assessment_window_cannot_authorize_current_absence(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch)
    env.assessor.mutate_assessment = lambda a: replace(a, assessed_at="2010-01-01T00:00:00.000000Z",
        validity_end="2011-01-01T00:00:00.000000Z")
    with pytest.raises(ValueError, match="assessment"):
        env.reconcile(allow_submit=True)
    assert not [r for r in env.requests if r.method == "POST"]


def test_historical_policy_result_cannot_omit_a_retained_present_control_row(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch, excluded=True)
    env.policy_mutator = lambda d: {**d, "valid_exclusion_rows": [], "excluded_target_ids": []}
    with pytest.raises(ValueError, match="policy"):
        env.reconcile(allow_submit=True)
    assert not [r for r in env.requests if r.method == "POST"]


def test_historical_unlinked_positive_history_is_unknown_before_preparation(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch)
    actual = env.retained.reload_history
    env.retained.reload_history = lambda original: replace(actual(original), ever_positive=True)
    with pytest.raises(ValueError, match="positive|linkage"):
        env.reconcile(allow_submit=True)
    assert not env.requests and "token" not in env.trace


def test_historical_initial_history_refs_cannot_be_silently_discarded(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch)
    history = env.retained.reload_history(env.original)
    bad = replace(history, evidence_refs=(ro.EvidenceRef(uuid4(), "b" * 64),))
    with pytest.raises(ValueError, match="history|evidence|observation"):
        env.reconcile(history=bad, allow_submit=True)
    assert not env.requests and "token" not in env.trace


@pytest.mark.parametrize("callback", ["issued", "outcome"])
def test_historical_foreign_callback_cannot_commit_this_original(historical_deny_io, monkeypatch, callback):
    env = HistoricalEnvironment(monkeypatch, present=callback == "outcome")
    foreign = HistoricalEnvironment(monkeypatch)
    if callback == "issued":
        supplied = {"before_submit": foreign.before_submit}
    else:
        supplied = {"persist_outcome": foreign.persist_outcome}
    with pytest.raises(ValueError):
        env.reconcile(allow_submit=True, **supplied)
    assert not [r for r in env.requests if r.method == "POST"]


@pytest.mark.parametrize("mutation", ["records", "authority", "ownership", "context", "budget"])
def test_historical_original_scope_mismatch_fails_before_preparation(historical_deny_io, monkeypatch, mutation):
    env = HistoricalEnvironment(monkeypatch)
    kwargs = {}
    if mutation == "records":
        env.dispatch.records[0]["note"] = "mutated original"
    elif mutation in {"authority", "ownership"}:
        kwargs["authority" if mutation == "authority" else "ownership_proof"] = b"{}"
    elif mutation == "context":
        kwargs["context"] = replace(env.context, purpose="upload-receipt")
    else:
        foreign = FixtureRetained(env)
        kwargs["request_budget"] = ro.RequestBudget(10, retained=foreign, original=env.original)
    with pytest.raises(ValueError):
        env.reconcile(allow_submit=True, **kwargs)
    assert not env.requests and "token" not in env.trace


def test_historical_new_attempt_cannot_erase_prior_matching_positive(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch, present=True)
    env.reconcile()
    prior_attempt = env.context.attempt_id
    prior_refs = env.retained.reload_history(env.original).evidence_refs
    rotate = getattr(env, "start_new_attempt", None)
    assert callable(rotate), "retained fixture must reopen prior-attempt history"
    rotate()
    assert env.context.attempt_id != prior_attempt
    env.target_rows.clear()
    result = env.reconcile(allow_submit=True)
    assert result.unknown_ids == (UUID(H_ROW["id"]),)
    assert set(prior_refs) <= set(result.evidence_refs)
    assert env.retained.reload_history(env.original).ever_positive
    assert not [r for r in env.requests if r.method == "POST"]


@pytest.mark.parametrize("prior", [116, 117])
def test_historical_receipt_and_checkpoint_compete_for_last_slots(historical_deny_io, monkeypatch, prior):
    env = HistoricalEnvironment(monkeypatch)
    for _ in range(prior):
        env.client.observe_records(DESTINATION, (H_ROW["id"],), account_id=H_ACCOUNT,
            context=replace(env.context, observation_id=uuid4()), on_observation=env.sink,
            request_budget=env.budget, request_guard=lambda **kwargs: None)
    env.after_http = lambda r: setattr(env, "active", False) if r.method == "POST" else None
    with pytest.raises(ValueError, match="allowance|expired"):
        env.reconcile(allow_submit=True)
    state = env.retained._load()
    assert len([s for s in state["slots"] if s["state"] != "released"]) == 128
    posts = [r for r in env.requests if r.method == "POST"]
    assert len(posts) == (1 if prior == 116 else 0)
    receipts = [e for e in state["events"] if e["kind"] == "observation"
                and json.loads(e["canonical"])["kind"] == "upload-receipt"]
    if prior == 116:
        assert len(receipts) == 1 and receipts[0]["commit"]["retention_mode"] == "audit-only"
    else:
        assert not receipts and not [e for e in state["events"] if e["kind"] == "issued-checkpoint"]


@pytest.mark.parametrize("which", ["authority", "ownership"])
def test_historical_unregistered_fixture_bytes_are_not_authority(historical_deny_io, monkeypatch, which):
    env = HistoricalEnvironment(monkeypatch)
    value = json.loads(getattr(env, which).canonical)
    if which == "authority":
        value["valid_until"] = "2027-01-01T00:00:00.000000Z"
    else:
        value["proof"]["valid_until"] = "2027-01-01T00:00:00.000000Z"
    setattr(env, which, (SyntheticAuthority if which == "authority" else SyntheticOwnership)(h_bytes(value)))
    with pytest.raises(ValueError, match="synthetic|manifest|artifact"):
        env.reconcile(allow_submit=True)
    assert not env.requests


@pytest.mark.parametrize("drift", ["journal", "owned-list", "owned-ref", "owned-issuer"])
def test_historical_owned_binding_is_resolved_and_rechecked_each_phase(historical_deny_io, monkeypatch, drift):
    env = HistoricalEnvironment(monkeypatch)
    def change(n, phase):
        if phase == "after" and n == 0:
            if drift == "journal":
                env.journal_binding = ["changed", "resolved", CONTROL]
            elif drift == "owned-list":
                env.owned_ids = []
            elif drift == "owned-ref":
                env.manifest.documents.pop(env.owned_ref)
            else:
                saved = env.manifest.resolve(env.owned_ref)
                saved["issuer"] = "unregistered"
                env.manifest.documents[env.owned_ref] = h_bytes(saved)
    env.on_owned_list = change
    with pytest.raises((ValueError, KeyError), match="owned|manifest|artifact"):
        env.reconcile(allow_submit=True)
    assert not [r for r in env.requests if r.method == "POST"]


def test_historical_issued_projection_without_checkpoint_is_unknown(historical_deny_io, monkeypatch):
    from fulcra_common.record_delivery import _source_body_digest
    env = HistoricalEnvironment(monkeypatch)
    state = env.retained._load()
    state["dispatch"].update(phase="issued", issued_records=[H_ROW], body_sha256=_source_body_digest([H_ROW]))
    env.retained.journal.bytes = h_bytes(state)
    env.dispatch.state = deepcopy(state["dispatch"])
    with pytest.raises(ValueError, match="issued|linkage"):
        env.reconcile(allow_submit=True)
    assert not env.requests and "token" not in env.trace


@pytest.mark.parametrize("fault", ["noncanonical", "boolean-revision", "unsupported-kind", "duplicate-proof",
    "wrong-slot", "wrong-kind", "duplicate-event", "raise-after-readback"])
def test_historical_lifecycle_identity_and_slot_contract_at_callback(historical_deny_io, monkeypatch, fault):
    env = HistoricalEnvironment(monkeypatch)
    def mutate(event):
        doc = event.document
        if fault == "noncanonical":
            return FixtureLifecycleEvent(json.dumps(doc).encode())
        if fault == "boolean-revision":
            doc["details"]["expected_revision"] = True
            return FixtureLifecycleEvent(h_bytes(doc))
        if fault == "unsupported-kind":
            doc["kind"] = "invented"
            return FixtureLifecycleEvent(h_bytes(doc))
        if fault == "duplicate-proof":
            doc["proof_refs"]["observations"].append(doc["proof_refs"]["observations"][0])
            return FixtureLifecycleEvent(h_bytes(doc))
        return event
    env.mutate_event = mutate
    if fault in {"wrong-slot", "wrong-kind"}:
        append = env.retained.append_lifecycle
        def corrupt_claim(event):
            state = env.retained._load()
            slot = next(s for s in state["slots"] if s.get("event_id") == str(event.ref.event_id))
            if fault == "wrong-kind":
                slot["kind"] = "outcome"
            else:
                slot["anchor"] = "b" * 64
            env.retained.journal.bytes = h_bytes(state)
            return append(event)
        env.retained.append_lifecycle = corrupt_claim
    if fault == "duplicate-event":
        lookup = env.retained.lookup_lifecycle
        def append_twice(ref):
            value = lookup(ref)
            env.retained.append_lifecycle(value.event)
            return value
        env.retained.lookup_lifecycle = append_twice
    if fault == "raise-after-readback":
        env.after_checkpoint = lambda: (_ for _ in ()).throw(RuntimeError("fixture callback failed after reread"))
    with pytest.raises((ValueError, KeyError, RuntimeError)):
        env.reconcile(allow_submit=True)
    assert not [r for r in env.requests if r.method == "POST"]


@pytest.mark.parametrize("which", ["observation", "assessment"])
def test_historical_sink_acknowledgement_must_equal_independent_retention(historical_deny_io, monkeypatch, which):
    env = HistoricalEnvironment(monkeypatch)
    env.retained.bad_ack = lambda c: replace(c, ledger_revision=c.ledger_revision + 1)
    if which == "assessment":
        real_sink = env.retained.sink
        def observations_ok(observation):
            bad, env.retained.bad_ack = env.retained.bad_ack, None
            try:
                return real_sink(observation)
            finally:
                env.retained.bad_ack = bad
        env.retained.sink = observations_ok
    with pytest.raises(ValueError, match="acknowledgement|assessment"):
        env.reconcile(allow_submit=True)
    assert not env.checkpoints and not [r for r in env.requests if r.method == "POST"]


@pytest.mark.parametrize("kind", ["observation", "assessment"])
def test_historical_evidence_written_then_raised_is_never_clean_unknown(historical_deny_io, monkeypatch, kind):
    env = HistoricalEnvironment(monkeypatch)
    env.retained.fail_after_append = kind
    with pytest.raises(RuntimeError, match="written then raised"):
        env.reconcile(allow_submit=True)
    assert not env.outcomes and not env.checkpoints and not [r for r in env.requests if r.method == "POST"]
    state = env.retained._load()
    assert len([e for e in state["events"] if e["kind"] == kind]) == 1


def test_historical_policy_recheck_follows_target_visibility(historical_deny_io, monkeypatch):
    from fulcra_common.record_exclusions import _record
    env = HistoricalEnvironment(monkeypatch, present=True)
    def exclusion_during_target(request):
        if "/records" in request.url.path and CONTROL not in request.url.params.get("q", ""):
            row = _record(CONTROL, DESTINATION, H_ROW["id"], action_id=str(uuid4()),
                requested_at=datetime(2026, 1, 2, tzinfo=timezone.utc), reason="operator", evidence=["synthetic-new-policy"])
            env.control_rows[row["id"]] = row
    env.after_http = exclusion_during_target
    result = env.reconcile()
    assert result.held_ids == (UUID(H_ROW["id"]),)
    assert not result.verified_ids and not [r for r in env.requests if r.method == "POST"]


@pytest.mark.parametrize("field", ["revision", "mode"])
def test_historical_reopen_verifies_original_stored_ack_bytes(historical_deny_io, monkeypatch, field):
    env = HistoricalEnvironment(monkeypatch)
    observation = env.client.observe_records(DESTINATION, (H_ROW["id"],), account_id=H_ACCOUNT,
        context=env.context, on_observation=env.sink, request_budget=env.budget, request_guard=lambda **kwargs: None)
    state = env.retained._load()
    if field == "revision":
        state["events"][0]["commit"]["ledger_revision"] = 0
    else:
        state["events"][0]["commit"]["retention_mode"] = "audit-only"
    env.retained.journal.bytes = h_bytes(state)
    reopened = FixtureRetained(env, env.retained.journal)
    with pytest.raises(ValueError, match="acknowledgement|commit"):
        reopened.lookup(observation.ref)


def h_short_assessment_clock(env, monkeypatch):
    """Assessment expiry advances independently of still-valid authority."""
    from fulcra_common import data_types, v1
    clock = [H_STAMP]
    for module in (ro, data_types, v1):
        monkeypatch.setattr(module, "_now", lambda: clock[0])
    def assess(a):
        end = (datetime.fromisoformat(clock[0].replace("Z", "+00:00")) + timedelta(seconds=1))
        return replace(a, assessed_at=clock[0], validity_end=end.isoformat(timespec="microseconds").replace("+00:00", "Z"))
    env.assessor.mutate_assessment = assess
    def advance():
        clock[0] = env.now = "2026-01-02T12:00:02.000000Z"
    return clock, advance


@pytest.mark.parametrize("stage", ["target-retained", "policy-schema", "final-schema",
    "issued-entry", "issued-current", "final-post", "outcome-entry", "outcome-readback"])
def test_historical_assessment_expiry_is_checked_at_each_coverage_use(historical_deny_io, monkeypatch, stage):
    env = HistoricalEnvironment(monkeypatch, present=stage in
        {"target-retained", "policy-schema", "outcome-entry", "outcome-readback"})
    clock, advance = h_short_assessment_clock(env, monkeypatch)
    if stage == "target-retained":
        real = env.retained.decision_sink
        def retained_then_expired(event):
            commit = real(event)
            advance()
            return commit
        env.retained.decision_sink = retained_then_expired
    elif stage in {"policy-schema", "final-schema"}:
        def schema_work(observation):
            if isinstance(observation, ro.SchemaObservation) and observation.document["destination_data_type"] == (
                    CONTROL if stage == "policy-schema" else DESTINATION):
                advance()
        env.after_sink = schema_work
    elif stage == "issued-entry":
        env.before_callback = advance
    elif stage in {"issued-current", "final-post"}:
        calls = [0]
        watched = "before-issued" if stage == "issued-current" else "before-post"
        def current_work(current_stage):
            if current_stage == watched:
                calls[0] += 1
                if calls[0] == (2 if stage == "issued-current" else 3):
                    advance()
        env.on_current = current_work
    elif stage == "outcome-entry":
        real = env.persist_outcome
        def expire_before_cas(delivery, history):
            advance()
            return real(delivery, history)
        env.persist_outcome = expire_before_cas
    else:
        env.after_outcome = advance
    with pytest.raises(ValueError, match="assessment|coverage|ineligible"):
        env.reconcile(allow_submit=True)
    assert env.active and env.now == clock[0] < H_END  # authority remains current
    assert not [r for r in env.requests if r.method == "POST"]
    state = env.retained._load()
    issued = [e for e in state["events"] if e["kind"] == "issued-checkpoint"]
    outcomes = [e for e in state["events"] if e["kind"] == "outcome"]
    assert len(issued) == (1 if stage == "final-post" else 0)
    assert len(outcomes) == (1 if stage == "outcome-readback" else 0)
    assert len(env.outcomes) == (1 if stage in {"outcome-entry", "outcome-readback"} else 0)
    if stage in {"target-retained", "policy-schema", "outcome-entry", "outcome-readback"}:
        assert state["positive_refs"]  # expiry never erases matching safety evidence


def test_historical_full_allowance_stops_before_proof_resolver_preparation(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch, event_limit=3)
    with pytest.raises(ValueError, match="allowance"):
        env.reconcile(allow_submit=True)
    assert len(env.retained._load()["events"]) == 3  # target, target assessment, before-schema
    assert env.trace.count("sdk") == env.trace.count("token") == 2
    assert env.trace.count("owned-list") == 0
    assert len(env.requests) == 2 and not env.checkpoints and not env.outcomes


def test_historical_interrupted_proof_keeps_its_started_slot_across_reopen(historical_deny_io, monkeypatch):
    env = HistoricalEnvironment(monkeypatch)
    reservations = []
    reserve = env.budget.reserve_events
    def capture(count):
        result = reserve(count)
        reservations.append(result)
        return result
    env.budget.reserve_events = capture
    def interrupted():
        if env.trace.count("sdk") == 3:
            raise RuntimeError("synthetic interrupted proof resolver")
    env.after_account = interrupted
    with pytest.raises(ValueError, match="cannot establish the authenticated account"):
        env.reconcile(allow_submit=True)
    state = env.retained._load()
    assert len(state["events"]) == 3
    assert len(state["slots"]) == 4 and state["slots"][-1]["state"] == "started"
    proof_slot = reservations[-1]
    with pytest.raises(ValueError, match="release"):
        proof_slot.release_unused()
    env.after_account = lambda: None
    env.retained = FixtureRetained(env, env.retained.journal)
    env.budget = ro.RequestBudget(500, retained=env.retained, original=env.original)
    for _ in range(124):
        env.client.observe_records(DESTINATION, (H_ROW["id"],), account_id=H_ACCOUNT,
            context=replace(env.context, observation_id=uuid4()), on_observation=env.sink,
            request_budget=env.budget, request_guard=lambda **kwargs: None)
    assert len(env.retained._load()["events"]) == 127
    assert len(env.retained._load()["slots"]) == 128
    other = FixtureRetained(env, env.retained.journal)
    resumed = ro.RequestBudget(500, retained=other, original=env.original)
    with pytest.raises(ValueError, match="allowance"):
        resumed.reserve_events(1)
    assert not env.checkpoints and not [r for r in env.requests if r.method == "POST"]


@pytest.mark.parametrize("stage", ["final-post", "success-return"])
def test_historical_current_authority_follows_coverage_lookups(historical_deny_io, monkeypatch, stage):
    env = HistoricalEnvironment(monkeypatch, present=stage == "success-return")
    checkpoint, outcome, lookups = [False], [False], [0]
    env.after_checkpoint = lambda: checkpoint.__setitem__(0, True)
    env.after_outcome = lambda: outcome.__setitem__(0, True)
    lookup = env.retained.lookup_assessment
    def local_read(ref):
        result = lookup(ref)
        if stage == "final-post" and checkpoint[0]:
            lookups[0] += 1
            # Two support lookups belong to the issued closure. The third is
            # the actual final POST guard, after that closure has returned.
            if lookups[0] == 3:
                env.active = False
        elif stage == "success-return" and outcome[0]:
            env.active = False
        return result
    env.retained.lookup_assessment = local_read
    with pytest.raises(ValueError, match="expired"):
        env.reconcile(allow_submit=True)
    assert not [r for r in env.requests if r.method == "POST"]
