"""Synthetic, deny-network tests of retained transport facts; no authority issuer."""
import hashlib
import json
import socket
import subprocess
import urllib.request
from dataclasses import replace
from threading import RLock
from uuid import UUID, uuid4

import httpx
import pytest

from fulcra_common.client import BaseFulcraClient
from fulcra_common.v1 import V1DataClient

ACCOUNT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
DESTINATION = "Event/11111111-2222-4333-8444-555555555555"
IDENTIFIER = "22222222-3333-4444-8555-666666666666"
STAMP = "2026-01-02T12:00:00.000000Z"
SCHEMA = {"type": "object", "properties": {"id": {"type": "string"}, "note": {"type": "string"}},
          "required": ["id", "note"]}
ROW = {"id": IDENTIFIER, "note": "Synthetic full row"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


@pytest.fixture
def deny_io(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("unstubbed external I/O")
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(urllib.request, "urlopen", denied)
    monkeypatch.setattr(subprocess, "run", denied)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(BaseFulcraClient, "get_token", denied)


def test_public_exact_observer_retains_actual_response_before_return(deny_io, monkeypatch):
    # Removing the library observer or retaining a manufactured dictionary fails here.
    assert callable(getattr(V1DataClient, "observe_records", None)), "public exact observer missing"
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    observation = env.read()
    assert observation.records == (ROW,)
    assert env.retained.lookup(observation.ref).observation.canonical == observation.canonical
    details = observation.document["details"]
    assert details["response"]["body_sha256"] == hashlib.sha256(json.dumps(ROW, separators=(",", ":")).encode()).hexdigest()
    assert details["request"]["ordered_query"][1] == ["fulcra_userid", ACCOUNT]
    assert details["response"]["headers"]["x-fulcra-userid"] is None
    assert details["local_validation"] == "complete-local-framing-and-selector-validation"


class MemoryJournal:
    """Canonical fixture bytes shared by reopened handles; explicitly synthetic."""
    def __init__(self):
        self.lock = RLock()
        self.bytes = canonical({"version": 1, "revision": 0, "bindings": {}, "slots": [], "events": [], "target_presence_refs": {}})
        self.floor = 0


class RetainedFixture:
    def __init__(self, journal=None):
        self.journal = journal or MemoryJournal()

    def _load(self):
        state = json.loads(self.journal.bytes)
        if canonical(state) != self.journal.bytes or set(state) != {"version", "revision", "bindings", "slots", "events", "target_presence_refs"}:
            raise ValueError("fixture manifest unknown")
        if type(state["revision"]) is not int or state["revision"] < self.journal.floor:
            raise ValueError("fixture revision regressed")
        if state["target_presence_refs"] != self._target_presence_references(state["events"]):
            raise ValueError("fixture target-presence references corrupt")
        seen = set()
        for event in state["events"]:
            body = event["canonical"].encode()
            if hashlib.sha256(body).hexdigest() != event["sha256"] or event["event_id"] in seen:
                raise ValueError("fixture event corrupt")
            seen.add(event["event_id"])
            slot = state["slots"][event["slot"]]
            if (slot["state"] != "consumed" or not slot["committed"] or slot["event_id"] != event["event_id"]
                    or slot["sha256"] != event["sha256"] or slot["kind"] != event["kind"]):
                raise ValueError("fixture event slot linkage corrupt")
            if event["kind"] == "observation":
                observation = self._observation_for_slot(state, slot, body)
                if str(observation.ref.observation_id) != event["event_id"]:
                    raise ValueError("fixture observation identity differs from claim")
                self._stored_commit(state, event, observation)
        occupied = set()
        for index, slot in enumerate(state["slots"]):
            key = slot["anchor"], slot["slot_id"]
            if slot["state"] != "released":
                if key in occupied:
                    raise ValueError("fixture duplicated occupied slot")
                occupied.add(key)
            self._bound_original(state, slot["anchor"])
            binding = state["bindings"][slot["anchor"]]
            if slot["slot_id"] not in range(binding["limit"]) or slot["state"] not in {"held", "started", "consumed", "uncertain", "released"}:
                raise ValueError("fixture slot corrupt")
            if slot.get("committed") and not any(e["slot"] == index for e in state["events"]):
                raise ValueError("fixture manifest missing event")
        return state

    @staticmethod
    def _bound_original(state, anchor):
        from fulcra_common.record_observations import OriginalTargetContext
        try:
            binding = state["bindings"][anchor]
            if set(binding) != {"original", "limit"} or type(binding["limit"]) is not int or not 1 <= binding["limit"] <= 128:
                raise ValueError("fixture retained binding malformed")
            original = OriginalTargetContext(canonical(binding["original"]))
            if original.sha256 != anchor:
                raise ValueError("fixture retained anchor digest differs")
            return original
        except (KeyError, TypeError):
            raise ValueError("fixture retained binding unavailable") from None

    @classmethod
    def _observation_for_slot(cls, state, slot, body):
        from fulcra_common.record_observations import ExactReadObservation, SchemaObservation, UploadReceiptObservation
        constructors = {"exact-read": ExactReadObservation, "served-schema": SchemaObservation,
                        "upload-receipt": UploadReceiptObservation}
        try:
            observation = constructors[json.loads(body)["kind"]](body)
        except (KeyError, TypeError):
            raise ValueError("fixture observation type unavailable") from None
        original = cls._bound_original(state, slot["anchor"]).document
        doc = observation.document
        if doc["original_target"] != original or any(doc[key] != original[key] for key in ("account_id", "attempt_id", "original_hash")):
            raise ValueError("fixture observation/reservation original scope differs")
        return observation

    @staticmethod
    def _stored_commit(state, event, observation):
        from fulcra_common.record_observations import EvidenceCommit, EvidenceRef
        try:
            if set(event) != {"event_id", "sha256", "kind", "canonical", "slot", "commit"}:
                raise ValueError("fixture stored event acknowledgement missing/malformed")
            saved = event["commit"]
            if not isinstance(saved, dict) or set(saved) != {"ref", "original_hash", "ledger_revision", "retention_mode"}:
                raise ValueError("fixture stored acknowledgement fields malformed")
            ref = saved["ref"]
            if not isinstance(ref, dict) or set(ref) != {"observation_id", "sha256"}:
                raise ValueError("fixture stored acknowledgement reference malformed")
            identifier = UUID(ref["observation_id"])
            if str(identifier) != ref["observation_id"]:
                raise ValueError("fixture stored acknowledgement UUID noncanonical")
            commit = EvidenceCommit(EvidenceRef(identifier, ref["sha256"]), saved["original_hash"],
                                    saved["ledger_revision"], saved["retention_mode"])
            if (commit.ref != observation.ref or commit.original_hash != observation.document["original_hash"]
                    or commit.ledger_revision > state["revision"]):
                raise ValueError("fixture stored acknowledgement scope/revision differs")
            return commit
        except (KeyError, TypeError, AttributeError):
            raise ValueError("fixture stored acknowledgement unavailable") from None

    @staticmethod
    def _target_presence_references(events):
        """Raw row presence only; no frozen-payload matching or replay history."""
        references = {}
        for event in events:
            if event["kind"] != "observation":
                continue
            doc = json.loads(event["canonical"])
            if doc["kind"] != "exact-read" or doc["purpose"] != "target-read":
                continue
            for row in doc["details"]["rows"]:
                key = json.dumps([doc["account_id"], doc["original_hash"], doc["destination_data_type"], row["id"]])
                references.setdefault(key, []).append({"observation_id": doc["observation_id"], "sha256": event["sha256"]})
        return references

    def _save(self, state):
        state["revision"] += 1
        self.journal.bytes = canonical(state)
        self.journal.floor = state["revision"]
        assert self._load() == state  # actual fixture reread

    def bind_event_allowance(self, original, *, event_limit):
        with self.journal.lock:
            state = self._load()
            old = state["bindings"].get(original.sha256)
            binding = {"original": original.document, "limit": event_limit}
            if old is not None and old != binding:
                raise ValueError("fixture resumed ceiling changed")
            if old is None:
                state["bindings"][original.sha256] = binding
                self._save(state)
            return AllowanceFixture(self, original)

    def sink(self, observation):
        return self.append_claim("observation", observation.document["observation_id"], observation.canonical)

    def append_claim(self, kind, event_id, body):
        digest = hashlib.sha256(body).hexdigest()
        with self.journal.lock:
            state = self._load()
            matches = [(i, s) for i, s in enumerate(state["slots"])
                       if s["state"] == "consumed" and not s["committed"] and s["event_id"] == str(event_id)
                       and s["sha256"] == digest and s["kind"] == kind]
            if len(matches) != 1 or any(e["event_id"] == str(event_id) for e in state["events"]):
                raise ValueError("fixture sink requires exact unique claim")
            index, slot = matches[0]
            if kind == "observation":
                observation = self._observation_for_slot(state, slot, body)
                if str(observation.ref.observation_id) != str(event_id):
                    raise ValueError("fixture observation identity differs from claim")
            event = {"event_id": str(event_id), "sha256": digest, "kind": kind,
                     "canonical": body.decode(), "slot": index}
            commit = None
            if kind == "observation":
                from fulcra_common.record_observations import EvidenceCommit
                commit = EvidenceCommit(observation.ref, observation.document["original_hash"],
                                        state["revision"] + 1, "audit-only")
                event["commit"] = {"ref": {"observation_id": str(commit.ref.observation_id), "sha256": commit.ref.sha256},
                    "original_hash": commit.original_hash, "ledger_revision": commit.ledger_revision,
                    "retention_mode": commit.retention_mode}
            slot["committed"] = True
            state["events"].append(event)
            state["target_presence_refs"] = self._target_presence_references(state["events"])
            self._save(state)
            return commit

    def lookup(self, ref):
        from fulcra_common.record_observations import RetainedObservation
        state = self._load()
        matches = [e for e in state["events"] if e["event_id"] == str(ref.observation_id) and e["sha256"] == ref.sha256]
        if len(matches) != 1:
            raise ValueError("fixture observation unavailable")
        body = matches[0]["canonical"].encode()
        observation = self._observation_for_slot(state, state["slots"][matches[0]["slot"]], body)
        return RetainedObservation(observation, self._stored_commit(state, matches[0], observation))

    def lookup_assessment(self, ref):
        raise ValueError("no fixture assessment retained")

    def reload_history(self, original):
        raise ValueError("B1a fixture has no external replay history")


class AllowanceFixture:
    def __init__(self, store, original):
        self.store, self.original = store, original

    def reserve_events(self, count):
        if type(count) is not int or count < 1:
            raise ValueError("invalid reservation count")
        with self.store.journal.lock:
            state = self.store._load()
            if self.store._bound_original(state, self.original.sha256).canonical != self.original.canonical:
                raise ValueError("fixture allowance original changed")
            slots = [s for s in state["slots"] if s["anchor"] == self.original.sha256]
            if any(s["state"] == "uncertain" or (s["state"] == "consumed" and not s["committed"]) for s in slots):
                raise ValueError("fixture uncertain occupancy")
            used = {s["slot_id"] for s in slots if s["state"] != "released"}
            limit = state["bindings"][self.original.sha256]["limit"]
            free = sorted(set(range(limit)) - used)
            if len(free) < count:
                raise ValueError("fixture event allowance exhausted")
            reservation = str(uuid4())
            indices = []
            for slot_id in free[:count]:
                indices.append(len(state["slots"]))
                state["slots"].append({"reservation_id": reservation, "anchor": self.original.sha256,
                                       "slot_id": slot_id, "state": "held", "committed": False})
            self.store._save(state)
            return ReservationFixture(self.store, tuple(indices), self.original)


class ReservationFixture:
    def __init__(self, store, indices, original):
        self.store, self.indices, self.original = store, indices, original

    def _state(self):
        state = self.store._load()
        for index in self.indices:
            anchor = state["slots"][index]["anchor"]
            if anchor != self.original.sha256 or self.store._bound_original(state, anchor).canonical != self.original.canonical:
                raise ValueError("fixture reservation original changed")
        return state

    def start(self):
        with self.store.journal.lock:
            state = self._state()
            for index in self.indices:
                if state["slots"][index]["state"] != "held":
                    raise ValueError("fixture reservation cannot start")
                state["slots"][index]["state"] = "started"
            self.store._save(state)

    def consume(self, kind, *, event_id, event_sha256):
        with self.store.journal.lock:
            state = self._state()
            if any(s.get("event_id") == str(event_id) for s in state["slots"]):
                raise ValueError("fixture duplicate event claim")
            free = [i for i in self.indices if state["slots"][i]["state"] in {"held", "started"}]
            if not free:
                raise ValueError("fixture reservation consumed")
            state["slots"][free[0]].update(state="consumed", kind=kind, event_id=str(event_id), sha256=event_sha256)
            self.store._save(state)

    def release_unused(self):
        with self.store.journal.lock:
            state = self._state()
            if any(state["slots"][i]["state"] != "held" for i in self.indices):
                raise ValueError("fixture started/consumed reservation cannot be released")
            for index in self.indices:
                state["slots"][index]["state"] = "released"
            self.store._save(state)


class Environment:
    def __init__(self, monkeypatch, handler, *, event_limit=128, request_limit=30, store=None):
        from fulcra_common import data_types
        from fulcra_common.record_observations import OriginalTargetContext, ObservationContext, RequestBudget
        self.calls, self.order = [], []
        self.active = True
        self.token_account = ACCOUNT
        self.after_account = lambda: None
        self.after_sink = lambda observation: None
        env = self
        class Credentials:
            def __init__(self, *, access_token):
                assert access_token == "synthetic-observation-token"
        class API:
            def __init__(self, *, credentials):
                assert isinstance(credentials, Credentials)
            def get_fulcra_userid(self):
                env.order.append("sdk")
                value = env.token_account
                env.after_account()
                return value
        class Client(BaseFulcraClient):
            def get_token(self):
                env.order.append("token")
                return "synthetic-observation-token"
        monkeypatch.setattr(data_types, "FulcraCredentials", Credentials)
        monkeypatch.setattr(data_types, "FulcraAPI", API)
        def transport(request):
            self.calls.append(request)
            self.order.append(request.method)
            return handler(request)
        self.client = V1DataClient(Client(base_url="https://synthetic.invalid", transport=httpx.MockTransport(transport)))
        self.original = OriginalTargetContext(canonical({"version": 1, "attempt_id": str(uuid4()), "original_hash": "a" * 64,
            "account_id": ACCOUNT, "destination_data_type": DESTINATION, "requested_target_ids": [IDENTIFIER], "read_contract_ref": None}))
        self.context = ObservationContext(version=1, observation_id=uuid4(), attempt_id=UUID(self.original.document["attempt_id"]),
            original_hash="a" * 64, account_id=ACCOUNT, destination_data_type=DESTINATION,
            requested_target_ids=(UUID(IDENTIFIER),), purpose="target-read", read_contract_ref=None,
            original_target=self.original, control_projection=())
        self.retained = store or RetainedFixture()
        self.budget = RequestBudget(request_limit, retained=self.retained, original=self.original, event_limit=event_limit)

    def guard(self, *, kind, context):
        self.order.append("guard:" + kind)
        assert context.original_target.canonical == self.original.canonical
        if not self.active:
            raise ValueError("synthetic authority expired")

    def sink(self, observation):
        self.order.append("sink:" + observation.document["kind"])
        commit = self.retained.sink(observation)
        self.after_sink(observation)
        return commit

    def kwargs(self, context=None):
        return dict(account_id=ACCOUNT, context=context or self.context, on_observation=self.sink,
                    request_guard=self.guard, request_budget=self.budget)

    def read(self, **overrides):
        kwargs = {**self.kwargs(), **overrides}
        return self.client.observe_records(DESTINATION, (IDENTIFIER,), **kwargs)

    def ingest(self, before_submit=lambda: None, records=None, **overrides):
        ctx = replace(self.context, observation_id=uuid4(), purpose="upload-receipt")
        return self.client.ingest_observed(DESTINATION, [ROW] if records is None else records,
            expected_schema=SCHEMA, before_submit=before_submit, **{**self.kwargs(ctx), **overrides})


def test_original_canonical_bytes_are_frozen_and_scope_checked(deny_io):
    from fulcra_common.record_observations import OriginalTargetContext
    doc = {"version": 1, "attempt_id": str(uuid4()), "original_hash": "a" * 64, "account_id": ACCOUNT,
           "destination_data_type": DESTINATION, "requested_target_ids": [IDENTIFIER], "read_contract_ref": None}
    original = OriginalTargetContext(canonical(doc))
    original.document["requested_target_ids"].clear()
    assert original.document == doc
    with pytest.raises(ValueError):
        OriginalTargetContext(json.dumps(doc).encode())
    for key, value in [("version", True), ("original_hash", "x" * 64), ("requested_target_ids", []),
                       ("account_id", " "), ("destination_data_type", "DeletedRecord")]:
        with pytest.raises(ValueError):
            OriginalTargetContext(canonical({**doc, key: value}))


@pytest.mark.parametrize("kind", ["ref", "original", "revision", "mode", "uncertain"])
def test_foreign_or_uncertain_sink_ack_never_returns_observation(deny_io, monkeypatch, kind):
    from fulcra_common.record_observations import EvidenceCommit, EvidenceRef
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    def sink(observation):
        if kind == "uncertain":
            raise RuntimeError("synthetic uncertain commit")
        return EvidenceCommit(EvidenceRef(uuid4(), observation.sha256) if kind == "ref" else observation.ref,
            "b" * 64 if kind == "original" else "a" * 64, True if kind == "revision" else 0,
            "untrusted" if kind == "mode" else "audit-only")
    with pytest.raises((ValueError, RuntimeError)):
        env.read(on_observation=sink)
    assert len(env.calls) == 1


def test_reopened_event_allowance_uses_retained_events_and_shared_handles(deny_io, monkeypatch):
    from fulcra_common.record_observations import RequestBudget
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW), request_limit=1000)
    for _ in range(127):
        env.read(context=replace(env.context, observation_id=uuid4()))
    reopened = RetainedFixture(env.retained.journal)
    other = RequestBudget(10, retained=reopened, original=env.original)
    env.budget = other
    env.retained = reopened
    env.read(context=replace(env.context, observation_id=uuid4()))
    before = list(env.order)
    with pytest.raises(ValueError):
        env.read(context=replace(env.context, observation_id=uuid4()))
    assert env.order == before and len(env.calls) == 128
    with pytest.raises(ValueError):
        RequestBudget(10, retained=reopened, original=env.original, event_limit=129)
    with pytest.raises(ValueError):
        other.assert_bound(RetainedFixture(env.retained.journal), env.original)


def test_context_cannot_retarget_original_and_child_ids(deny_io, monkeypatch):
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    for change in ({"account_id": "other"}, {"attempt_id": uuid4()}, {"original_hash": "b" * 64},
                   {"requested_target_ids": (uuid4(),)}, {"control_projection": ((UUID(IDENTIFIER), uuid4()),)}):
        with pytest.raises(ValueError):
            replace(env.context, **change)
    assert env.order == []


def test_inert_decision_history_and_delivery_values_reject_bad_shapes(deny_io):
    from fulcra_common.record_observations import HistoricalDelivery, HistoricalReplayEvidence
    from fulcra_common.record_observations import ReadAssessment, EvidenceRef, ArtifactRef
    ref = EvidenceRef(uuid4(), "a" * 64)
    artifact = ArtifactRef(uuid4(), "b" * 64)
    assessment = ReadAssessment(ref, "c" * 64, artifact, artifact, STAMP, STAMP, "unknown")
    assert assessment.coverage == "unknown"
    with pytest.raises(ValueError):
        ReadAssessment(ref, "c" * 64, artifact, artifact, STAMP, "2025-01-02T12:00:00.000000Z", "unknown")
    with pytest.raises(ValueError):
        HistoricalReplayEvidence("a" * 64, UUID(IDENTIFIER), True, False, (ref,), True)
    with pytest.raises(ValueError):
        HistoricalDelivery((UUID(IDENTIFIER),), (), (UUID(IDENTIFIER),), (), (), (), ())


def schema_handler(request):
    return httpx.Response(200, json=SCHEMA) if request.method == "GET" else httpx.Response(201, json={"upload_id": "synthetic", "extra": {"retained": True}})


def test_schema_and_receipt_are_actual_final_snapshot_and_serialized_body(deny_io, monkeypatch):
    env = Environment(monkeypatch, schema_handler)
    def callback():
        env.order.append("issued")
        ROW["note"] = "caller mutation after serialization"
    original_note = ROW["note"]
    try:
        receipt = env.ingest(before_submit=callback)
    finally:
        ROW["note"] = original_note
    docs = [json.loads(e["canonical"]) for e in env.retained._load()["events"]]
    assert [d["kind"] for d in docs] == ["served-schema", "upload-receipt"]
    assert docs[0]["observation_id"] != docs[1]["observation_id"]
    assert docs[0]["details"]["schema"] == SCHEMA
    assert receipt.document["details"]["receipt"] == {"upload_id": "synthetic", "extra": {"retained": True}}
    assert receipt.document["details"]["request_binding"]["schema_ref"]["observation_id"] == docs[0]["observation_id"]
    body = json.dumps({"id": IDENTIFIER, "note": original_note}, sort_keys=True, allow_nan=False).encode()
    assert env.calls[-1].content == body
    assert receipt.document["details"]["request_binding"]["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert [r.method for r in env.calls] == ["GET", "POST"]
    assert env.order.count("token") == 2 and env.order.count("sdk") == 2
    assert env.order[-3:] == ["guard:record-post", "POST", "sink:upload-receipt"]
    assert env.order.index("issued") > env.order.index("sink:served-schema")


@pytest.mark.parametrize("stage", ["sdk", "complete-read", "schema-sink", "before-post", "complete-receipt", "receipt-sink"])
def test_expiry_blocks_next_operation_but_never_received_audit(deny_io, monkeypatch, stage):
    def handler(request):
        if stage == "complete-read" or stage == "complete-receipt" and request.method == "POST":
            env.active = False
        return httpx.Response(200, json=ROW) if stage in {"sdk", "complete-read"} else schema_handler(request)
    env = Environment(monkeypatch, handler)
    if stage == "sdk":
        env.after_account = lambda: setattr(env, "active", False)
    if stage in {"schema-sink", "receipt-sink"}:
        env.after_sink = lambda observation: setattr(env, "active", False) if observation.kind == ("served-schema" if stage == "schema-sink" else "upload-receipt") else None
    callback = (lambda: setattr(env, "active", False)) if stage == "before-post" else (lambda: None)
    if stage in {"sdk", "schema-sink", "before-post"}:
        with pytest.raises(ValueError):
            env.read() if stage == "sdk" else env.ingest(before_submit=callback)
        assert [r.method for r in env.calls] == ([] if stage == "sdk" else ["GET"])
    else:
        result = env.read() if stage == "complete-read" else env.ingest()
        assert env.retained.lookup(result.ref).observation == result
        assert env.active is False
        before = len(env.calls)
        with pytest.raises(ValueError):
            env.read(context=replace(env.context, observation_id=uuid4()))
        assert len(env.calls) == before


@pytest.mark.parametrize("mode", ["duplicate-id", "unrequested", "partial", "owner", "encoding", "length", "duplicate-length", "duplicate-owner", "framing", "nonfinite", "suffix", "duplicate-key", "not-object", "retarget-origin"])
def test_observed_exact_response_failure_emits_no_evidence(deny_io, monkeypatch, mode):
    def handler(request):
        body = json.dumps(ROW).encode()
        headers = []
        if mode == "duplicate-id":
            body += b"\n" + body
        elif mode == "unrequested":
            body = json.dumps({**ROW, "id": str(uuid4())}).encode()
        elif mode in {"partial", "owner", "encoding", "length", "framing"}:
            headers = {"partial": [("x-next-page", "2")], "owner": [("x-fulcra-userid", "other")],
                "encoding": [("content-encoding", "gzip")], "length": [("content-length", "1")],
                "framing": [("content-length", str(len(body))), ("transfer-encoding", "chunked")]}[mode]
        elif mode == "duplicate-length":
            headers = [("content-length", str(len(body))), ("content-length", str(len(body)))]
        elif mode == "duplicate-owner":
            headers = [("x-fulcra-userid", ACCOUNT), ("x-fulcra-userid", ACCOUNT)]
        elif mode in {"nonfinite", "suffix", "duplicate-key", "not-object"}:
            body = {"nonfinite": b'{"id":NaN}', "suffix": b'{"id":"x"}bad', "duplicate-key": b'{"id":1,"id":2}', "not-object": b'[]'}[mode]
        # Install encoded metadata after construction: the boundary is buffered.
        response = httpx.Response(200, content=body, headers=[] if mode == "encoding" else headers)
        if mode == "encoding":
            response.headers.update(headers)
        return response
    env = Environment(monkeypatch, handler)
    if mode == "retarget-origin":
        http = env.client.client._client()
        actual = http.get
        def retarget(*args, **kwargs):
            response = actual(*args, **kwargs)
            http.base_url = "https://foreign.invalid"
            response.request = httpx.Request("GET", response.url.copy_with(host="foreign.invalid"))
            return response
        monkeypatch.setattr(http, "get", retarget)
    with pytest.raises((ValueError, httpx.HTTPError)):
        env.read()
    assert env.retained._load()["events"] == []
    assert len(env.calls) == 1


@pytest.mark.parametrize("bad_ids", [(), (IDENTIFIER, IDENTIFIER), ("bad",), ("AAAAAAAA-BBBB-4CCC-8DDD-EEEEEEEEEEEE",), tuple(str(UUID(int=i+1)) for i in range(33))])
def test_bad_exact_ids_refuse_before_credentials(deny_io, monkeypatch, bad_ids):
    env = Environment(monkeypatch, lambda request: pytest.fail("invalid IDs reached HTTP"))
    with pytest.raises(ValueError):
        env.client.observe_records(DESTINATION, bad_ids, **env.kwargs())
    assert env.order == [] and env.retained._load()["slots"] == []


def test_empty_observed_ingest_preserves_no_effect_contract(deny_io, monkeypatch):
    env = Environment(monkeypatch, lambda request: pytest.fail("empty ingest reached HTTP"))
    assert env.ingest(records=[]) is None
    assert env.order == [] and env.retained._load()["slots"] == []
    with pytest.raises(ValueError):
        env.ingest(records=[], before_submit=None)


@pytest.mark.parametrize("body", [b'{}', b'{"upload_id":7}', b'{"upload_id":""}', b'{"upload_id":"x","n":1.0000000000000001}', b'{"upload_id":"x","upload_id":"y"}', b'{"upload_id":NaN}', b'[]', b'\xff'])
def test_observed_receipt_uncertainty_never_retries_or_fabricates(deny_io, monkeypatch, body):
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=SCHEMA) if request.method == "GET" else httpx.Response(201, content=body))
    with pytest.raises(ValueError):
        env.ingest()
    assert [r.method for r in env.calls] == ["GET", "POST"]
    assert len(env.retained._load()["events"]) == 1


def test_request_budget_is_charged_at_each_sdk_and_http_boundary(deny_io, monkeypatch):
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW), request_limit=1)
    with pytest.raises(ValueError):
        env.read()
    assert env.order.count("sdk") == 1 and env.calls == []
    before = env.order.count("sdk")
    with pytest.raises(ValueError):
        env.read(context=replace(env.context, observation_id=uuid4()))
    assert env.order.count("sdk") == before


@pytest.mark.parametrize("event_limit,post_count", [(2, 0), (3, 1)])
def test_receipt_and_issued_checkpoint_compete_for_retained_capacity(deny_io, monkeypatch, event_limit, post_count):
    env = Environment(monkeypatch, schema_handler, event_limit=event_limit)
    def issued():
        reservation = env.budget.reserve_events(1)
        event_id, body = uuid4(), canonical({"synthetic-issued": True})
        reservation.consume("issued-checkpoint", event_id=event_id, event_sha256=hashlib.sha256(body).hexdigest())
        env.retained.append_claim("issued-checkpoint", event_id, body)
    if post_count:
        result = env.ingest(before_submit=issued)
        assert env.retained.lookup(result.ref).observation == result
    else:
        with pytest.raises(ValueError):
            env.ingest(before_submit=issued)
    assert sum(r.method == "POST" for r in env.calls) == post_count
    assert len([s for s in env.retained._load()["slots"] if s["state"] != "released"]) == event_limit


@pytest.mark.parametrize("bad", [None, 7, {"wrong": True}])
def test_canonical_observation_rejects_malformed_context_collections(deny_io, monkeypatch, bad):
    from fulcra_common.record_observations import ExactReadObservation
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    document = env.read().document
    document["requested_target_ids"] = bad
    with pytest.raises(ValueError):
        ExactReadObservation(canonical(document))


def test_public_schema_emits_one_actual_strict_parse_with_timeout(deny_io, monkeypatch):
    from fulcra_common import data_types
    body = b' {"type":"object","properties":{"id":{"type":"string"},"note":{"type":"string"}},"required":["id","note"]} '
    env = Environment(monkeypatch, lambda request: httpx.Response(200, content=body))
    calls = []
    parse = data_types._proof_json
    def parser(value, *, limit):
        if value == body:
            calls.append(limit)
        return parse(value, limit=limit)
    monkeypatch.setattr(data_types, "_proof_json", parser)
    ctx = replace(env.context, observation_id=uuid4(), purpose="schema-read", requested_target_ids=())
    observation = data_types.observe_served_schema(client=env.client.client, data_type=DESTINATION,
        timeout=2.5, **env.kwargs(ctx))
    assert calls == [1024 * 1024] and len(env.calls) == 1
    assert observation.schema == SCHEMA
    assert observation.document["details"]["response"]["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert env.calls[0].extensions["timeout"]["read"] == 2.5
    observation.schema["properties"].clear()
    assert observation.schema == SCHEMA


def test_observed_account_swap_refuses_http_and_never_substitutes_headers(deny_io, monkeypatch):
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    env.token_account = "other-account"
    with pytest.raises(ValueError):
        env.read()
    assert env.calls == []
    env.token_account = ACCOUNT
    result = env.read(context=replace(env.context, observation_id=uuid4()))
    assert result.records == (ROW,)
    assert env.calls[-1].headers["authorization"] == "Bearer synthetic-observation-token"
    assert env.order.count("token") == 2  # one captured token per actual proof, not another after proof


@pytest.mark.parametrize("stamp", ["2026-01-02T12:00:00Z", "2026-01-02T12:00:00.000000+00:00", "2026-02-30T12:00:00.000000Z", "2025-01-02T12:00:00.000000Z"])
def test_observation_clocks_are_canonical_and_ordered(deny_io, monkeypatch, stamp):
    from fulcra_common.record_observations import ExactReadObservation
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    doc = env.read().document
    doc["started_at"] = STAMP
    doc["received_at"] = stamp
    with pytest.raises(ValueError):
        ExactReadObservation(canonical(doc))


@pytest.mark.parametrize("change", [{"purpose": []}, {"requested_target_ids": [UUID(IDENTIFIER)]}, {"version": True}])
def test_context_invalid_python_shapes_refuse_locally(deny_io, monkeypatch, change):
    env = Environment(monkeypatch, lambda request: pytest.fail("invalid context reached HTTP"))
    with pytest.raises(ValueError):
        replace(env.context, **change)
    assert env.order == []


@pytest.mark.parametrize("state_name", ["held", "started", "uncertain", "consumed"])
def test_reopened_reservations_cannot_reset_or_refund_occupancy(deny_io, monkeypatch, state_name):
    from fulcra_common.record_observations import RequestBudget
    env = Environment(monkeypatch, lambda request: pytest.fail("occupied attempt reached HTTP"), event_limit=1)
    slot = env.budget.reserve_events(1)
    if state_name == "started":
        slot.start()
    elif state_name == "uncertain":
        state = env.retained._load()
        state["slots"][0]["state"] = "uncertain"
        env.retained._save(state)
    elif state_name == "consumed":
        slot.consume("outcome", event_id=uuid4(), event_sha256="b" * 64)
    reopened = RetainedFixture(env.retained.journal)
    budget = RequestBudget(10, retained=reopened, original=env.original, event_limit=1)
    with pytest.raises(ValueError):
        env.read(request_budget=budget)
    assert env.order == []
    if state_name != "held":
        with pytest.raises(ValueError):
            slot.release_unused()
    else:
        slot.release_unused()
        budget.reserve_events(1)  # positively reread release may be reused


def test_shared_handles_cannot_overbook_or_change_saved_ceiling(deny_io, monkeypatch):
    from fulcra_common.record_observations import RequestBudget
    env = Environment(monkeypatch, lambda request: pytest.fail("held capacity reached HTTP"), event_limit=1)
    other = RequestBudget(10, retained=env.retained, original=env.original, event_limit=1)
    env.budget.reserve_events(1)
    with pytest.raises(ValueError):
        other.reserve_events(1)
    with pytest.raises(ValueError):
        RequestBudget(10, retained=env.retained, original=env.original, event_limit=2)


@pytest.mark.parametrize("broken", ["lost-event", "duplicate-slot", "regression", "invalid-json"])
def test_corrupt_retained_accounting_holds_before_preparation(deny_io, monkeypatch, broken):
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    env.read()
    state = env.retained._load()
    if broken == "lost-event":
        state["events"] = []
    elif broken == "duplicate-slot":
        duplicate = dict(state["slots"][0], state="held", committed=False)
        state["slots"].append(duplicate)
    elif broken == "regression":
        state["revision"] = 0
    env.retained.journal.bytes = b'not-json' if broken == "invalid-json" else canonical(state)
    before = list(env.order)
    with pytest.raises(ValueError):
        env.read(context=replace(env.context, observation_id=uuid4()))
    assert env.order == before


def test_partial_internal_hooks_fail_before_any_client_or_token(deny_io, monkeypatch):
    from fulcra_common import data_types
    env = Environment(monkeypatch, lambda request: pytest.fail("partial hooks reached HTTP"))
    for operation in (
        lambda: env.client._read_rows("{}", ACCOUNT, label="synthetic", max_rows=1, request_guard=env.guard),
        lambda: env.client._submit(DESTINATION, [ROW], request_budget=env.budget),
        lambda: data_types._account_headers(env.client.client, ACCOUNT, observation_context=env.context),
    ):
        with pytest.raises(ValueError):
            operation()
    assert env.order == []


def test_target_target_presence_references_is_atomic_monotonic_and_control_separate(deny_io, monkeypatch):
    from fulcra_common.v1 import record_id_for
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    result = env.read()
    saved = env.retained._load()["target_presence_refs"]
    assert [result.ref.sha256] == [ref["sha256"] for refs in saved.values() for ref in refs]
    env.client.client._client()._transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b""))
    env.read(context=replace(env.context, observation_id=uuid4()))
    assert env.retained._load()["target_presence_refs"] == saved
    control = "Event/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    control_id = record_id_for(control, f"collect:v1:excluded:{DESTINATION}:{IDENTIFIER}")
    env.client.client._client()._transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"id": control_id}))
    ctx = replace(env.context, observation_id=uuid4(), purpose="policy-read", destination_data_type=control,
                  requested_target_ids=(UUID(control_id),), control_projection=((UUID(IDENTIFIER), UUID(control_id)),))
    observation = env.client.observe_records(control, (control_id,), **env.kwargs(ctx))
    assert observation.document["details"]["parent_target_ref"] == {"sha256": env.original.sha256}
    assert env.retained._load()["target_presence_refs"] == saved


def test_issued_marker_and_reserved_receipt_survive_full_capacity_expiry(deny_io, monkeypatch):
    env = Environment(monkeypatch, schema_handler, event_limit=3)
    def issued():
        reservation = env.budget.reserve_events(1)
        event_id, body = uuid4(), canonical({"synthetic-issued": True})
        reservation.consume("issued-checkpoint", event_id=event_id, event_sha256=hashlib.sha256(body).hexdigest())
        env.retained.append_claim("issued-checkpoint", event_id, body)
    env.after_sink = lambda observation: setattr(env, "active", False) if observation.kind == "upload-receipt" else None
    result = env.ingest(before_submit=issued)
    state = RetainedFixture(env.retained.journal)._load()
    assert len(state["events"]) == 3 and all(slot["state"] == "consumed" for slot in state["slots"])
    assert env.retained.lookup(result.ref).observation == result and not env.active


def test_policy_and_assessment_types_are_inert_exact_linked_documents(deny_io, monkeypatch):
    from fulcra_common import record_observations as observations
    from fulcra_common.v1 import record_id_for
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    control = "Event/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    control_id = UUID(record_id_for(control, f"collect:v1:excluded:{DESTINATION}:{IDENTIFIER}"))
    schema_ctx = replace(env.context, observation_id=uuid4(), purpose="schema-read", destination_data_type=control,
                         requested_target_ids=(), control_projection=((UUID(IDENTIFIER), control_id),))
    before_id, after_id, schema_id = uuid4(), uuid4(), uuid4()
    def reference(identifier):
        return {"observation_id": str(identifier), "sha256": "b" * 64}
    proof_details = {"control_data_type": control, "control_schema_ref": reference(schema_id),
        "owned_binding_ref": {"artifact_id": str(uuid4()), "sha256": "c" * 64}, "phase": "before",
        "parent_target_context_hash": env.original.sha256}
    proof = observations.PolicyBindingProof(canonical({**schema_ctx.document, "kind": "policy-binding-proof",
        "started_at": STAMP, "received_at": STAMP, "details": proof_details}))
    assert proof.document["details"]["phase"] == "before"
    ctx = replace(env.context, purpose="policy-read", control_projection=((UUID(IDENTIFIER), control_id),))
    policy_details = {"target_to_control_ids": [[IDENTIFIER, str(control_id)]], "valid_exclusion_rows": [],
        "excluded_target_ids": [], "record_read_ref": reference(uuid4()), "binding_before_ref": reference(before_id),
        "binding_after_ref": reference(after_id), "validation": "validated-permanent-policy-rows"}
    doc = {**ctx.document, "kind": "policy", "started_at": STAMP, "received_at": STAMP, "details": policy_details}
    assert observations.PolicyObservation(canonical(doc)).document == doc
    policy_details["binding_after_ref"] = reference(before_id)
    with pytest.raises(ValueError):
        observations.PolicyObservation(canonical(doc))
    event_id = uuid4()
    assessment = {"observation_ref": reference(schema_id), "scope_hash": "a" * 64,
        "read_contract_ref": {"artifact_id": str(uuid4()), "sha256": "b" * 64},
        "deployment_binding_ref": {"artifact_id": str(uuid4()), "sha256": "c" * 64},
        "assessed_at": STAMP, "validity_end": STAMP, "coverage": "unknown"}
    event = observations.ReadAssessmentEvent(canonical({"version": 1, "kind": "read-assessment", "event_id": str(event_id),
        "attempt_id": str(ctx.attempt_id), "original_hash": "a" * 64, "assessment": assessment}))
    commit = observations.DecisionCommit(event.ref, "a" * 64, 1, "audit-only")
    assert observations.RetainedAssessment(event, commit).event == event
    with pytest.raises(ValueError):
        observations.RetainedAssessment(event, replace(commit, original_hash="d" * 64))


def test_observed_ingest_requests_identity_encoding(deny_io, monkeypatch):
    env = Environment(monkeypatch, schema_handler)
    env.ingest()
    assert env.calls[-1].headers["accept-encoding"] == "identity"


def test_uncertain_sink_keeps_capacity_and_committed_target_presence(deny_io, monkeypatch):
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW), event_limit=1)
    def uncertain(observation):
        env.retained.sink(observation)
        raise RuntimeError("synthetic lost acknowledgement")
    with pytest.raises(RuntimeError):
        env.read(on_observation=uncertain)
    assert len(env.retained._load()["events"]) == 1
    assert env.retained._load()["target_presence_refs"]
    before = list(env.order)
    with pytest.raises(ValueError):
        env.read(context=replace(env.context, observation_id=uuid4()))
    assert env.order == before


@pytest.mark.parametrize("dimension", ["original_hash", "account_id", "attempt_id"])
def test_fixture_rejects_foreign_observation_before_append(deny_io, monkeypatch, dimension):
    from fulcra_common.record_observations import OriginalTargetContext, RequestBudget
    owner = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    foreign = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    different = {"original_hash": "b" * 64, "account_id": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff", "attempt_id": str(uuid4())}
    original = {**owner.original.document, dimension: different[dimension]}
    foreign.original = OriginalTargetContext(canonical(original))
    foreign.context = replace(foreign.context, original_target=foreign.original,
        original_hash=original["original_hash"], account_id=original["account_id"], attempt_id=UUID(original["attempt_id"]))
    foreign.token_account = original["account_id"]
    foreign.budget = RequestBudget(10, retained=foreign.retained, original=foreign.original)
    observation = foreign.read(account_id=original["account_id"])
    claim = owner.budget.reserve_events(1)
    claim.consume("observation", event_id=observation.ref.observation_id, event_sha256=observation.sha256)
    before = owner.retained.journal.bytes
    with pytest.raises(ValueError):
        owner.retained.sink(observation)
    assert owner.retained.journal.bytes == before
    assert owner.retained._load()["events"] == []
    assert owner.retained._load()["target_presence_refs"] == {}


@pytest.mark.parametrize("dimension", ["original_hash", "account_id", "attempt_id"])
def test_fixture_reopened_lookup_rejects_corrupt_original_link(deny_io, monkeypatch, dimension):
    from fulcra_common.record_observations import OriginalTargetContext
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    observation = env.read()
    state = env.retained._load()
    different = {"original_hash": "b" * 64, "account_id": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff", "attempt_id": str(uuid4())}
    other = OriginalTargetContext(canonical({**env.original.document, dimension: different[dimension]}))
    state["bindings"][other.sha256] = {"original": other.document, "limit": 128}
    state["slots"][state["events"][0]["slot"]]["anchor"] = other.sha256
    env.retained.journal.bytes = canonical(state)
    with pytest.raises(ValueError):
        RetainedFixture(env.retained.journal).lookup(observation.ref)


def test_fixture_reopen_returns_original_commit_after_other_handle_advances(deny_io, monkeypatch):
    from fulcra_common.record_observations import RequestBudget
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    accepted = []
    def sink(observation):
        commit = env.retained.sink(observation)
        accepted.append(commit)
        return commit
    first = env.read(on_observation=sink)
    other = RetainedFixture(env.retained.journal)
    budget = RequestBudget(10, retained=other, original=env.original)
    env.read(context=replace(env.context, observation_id=uuid4()), request_budget=budget, on_observation=other.sink)
    assert env.retained._load()["revision"] > accepted[0].ledger_revision
    assert RetainedFixture(env.retained.journal).lookup(first.ref).commit == accepted[0]


def test_fixture_sink_commit_is_created_inside_append_transaction(deny_io, monkeypatch):
    from fulcra_common.record_observations import RequestBudget
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    other = RetainedFixture(env.retained.journal)
    budget = RequestBudget(10, retained=other, original=env.original)
    append = env.retained.append_claim
    def append_then_other_handle_advances(*args):
        result = append(*args)
        budget.reserve_events(1).start()
        return result
    monkeypatch.setattr(env.retained, "append_claim", append_then_other_handle_advances)
    accepted = []
    def sink(observation):
        commit = env.retained.sink(observation)
        accepted.append(commit)
        return commit
    observation = env.read(on_observation=sink)
    assert accepted[0].ledger_revision < env.retained._load()["revision"]
    assert RetainedFixture(env.retained.journal).lookup(observation.ref).commit == accepted[0]


@pytest.mark.parametrize("field,value", [("ledger_revision", True), ("ledger_revision", -1),
    ("ledger_revision", "1"), ("ledger_revision", 100000), ("retention_mode", "untrusted"),
    ("original_hash", "b" * 64), ("ref", {"observation_id": str(UUID(int=1)), "sha256": "b" * 64})])
def test_fixture_lookup_refuses_malformed_stored_commit(deny_io, monkeypatch, field, value):
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=ROW))
    observation = env.read()
    state = env.retained._load()
    event = state["events"][0]
    event["commit"] = {"ref": {"observation_id": str(observation.ref.observation_id), "sha256": observation.sha256},
        "original_hash": env.context.original_hash, "ledger_revision": state["revision"], "retention_mode": "audit-only"}
    event["commit"][field] = value
    env.retained.journal.bytes = canonical(state)
    with pytest.raises(ValueError):
        RetainedFixture(env.retained.journal).lookup(observation.ref)


def test_fixture_target_presence_preserves_mismatching_rows_without_history_claim(deny_io, monkeypatch):
    mismatch = {**ROW, "note": "Synthetic different payload"}
    env = Environment(monkeypatch, lambda request: httpx.Response(200, json=mismatch))
    observation = env.read()
    assert observation.records == (mismatch,) and observation.records != (ROW,)
    assert env.retained._load()["target_presence_refs"]
    with pytest.raises(ValueError, match="no external replay history"):
        env.retained.reload_history(env.original)
