"""Synthetic retained namespace only; no production issuer/backend/gate exists."""
import hashlib
import importlib
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from fulcra_common.data_types import CreationJournal
from fulcra_common.record_exclusions import EXCLUSION_SPEC, _validated_exclusion_rows
from fulcra_common.record_observations import (
    ArtifactRef, EvidenceRef, PolicyBindingProof, PolicyObservation, RequestBudget,
    _canonical, _retain, _schema_context,
)
spec = importlib.util.spec_from_file_location('_fulcra_b1p_transport_fixtures',
    Path(__file__).with_name('test_historical_observed_transport.py'))
if spec.name not in sys.modules:
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
_fixture = sys.modules[spec.name]
deny_io, synthetic_client = _fixture.deny_io, _fixture.synthetic_client
RetainedFixture = _fixture._observations.RetainedFixture
ACCOUNT, DESTINATION, IDENTIFIER = _fixture.ACCOUNT, _fixture.DESTINATION, _fixture.IDENTIFIER

CONTROL = 'Event/aaaaaaaa-2222-4333-8444-555555555555'
CONTROL_ID = 'c21419c1-7934-5d4d-88de-febcde7de4a3'


def transcripts():
    name = 'fulcra_common._historical_owned_transcript'
    assert importlib.util.find_spec(name) is not None, 'actual verifier transcript collector is missing'
    return importlib.import_module(name)


class PolicyFixture(RetainedFixture):
    """Extend B1a fixture kinds, keeping its real shared reservation journal."""
    @classmethod
    def _observation_for_slot(cls, state, slot, body):
        kind = json.loads(body)['kind']
        if kind not in {'policy-binding-proof', 'policy'}:
            return super()._observation_for_slot(state, slot, body)
        observation = {'policy-binding-proof': PolicyBindingProof, 'policy': PolicyObservation}[kind](body)
        original = cls._bound_original(state, slot['anchor']).document
        assert observation.document['original_target'] == original
        return observation

    def sink(self, observation):
        with self.journal.lock:
            self.namespace.check_append(observation.canonical, document=True)
            if isinstance(observation, PolicyBindingProof):
                details = observation.document['details']
                ref = details['owned_binding_ref']
                issued = self.namespace.saved['issued'].get(ref['artifact_id'])
                state = self._load()
                slots = [slot for slot in state['slots'] if slot.get('event_id') == str(observation.ref.observation_id)
                         and slot.get('sha256') == observation.sha256 and slot['state'] == 'consumed']
                if (issued is None or issued['sha256'] != ref['sha256'] or len(slots) != 1
                        or slots[0]['reservation_id'] != issued['reservation_id']):
                    raise ValueError('synthetic proof sink requires exact transcript/slot linkage')
                transcript = json.loads(issued['canonical'])
                if (transcript['schema_ref'] != details['control_schema_ref']
                        or transcript['proof_observation_id'] != str(observation.ref.observation_id)):
                    raise ValueError('synthetic proof sink schema/proof linkage differs')
            return super().sink(observation)


class SyntheticAttachments:
    """Private fixture namespace. Canonical storage survives handles/reopen."""
    def __init__(self, env, saved=None):
        self.env = env
        self.saved = saved if saved is not None else {'runs': {}, 'issued': {}}
        self.fail = None

    def check_append(self, raw, *, document=False):
        # SAME store lock as append/sink; re-read actual bytes, never a byte counter.
        state = self.env.retained._load()
        documents = [event['canonical'].encode() for event in state['events']]
        attachments = [fragment.encode() for run in self.saved['runs'].values()
                       for fragment in run['fragments']]
        attachments += [issued['canonical'].encode() for issued in self.saved['issued'].values()]
        (documents if document else attachments).append(raw)
        transcripts()._check_chain_retention(documents=documents, attachments=attachments)

    def begin(self, *, invocation_id, context, phase, proof_reservation, admission_ref):
        state = proof_reservation._state()
        slot = state['slots'][proof_reservation.indices[0]]
        assert slot['state'] == 'started'
        run = {'original': context.original_target.sha256, 'invocation': str(invocation_id),
               'reservation_id': slot['reservation_id'], 'phase': phase, 'fragments': [],
               'revision': 0, 'complete': False}
        self.saved['runs'][str(invocation_id)] = run
        namespace = self
        class Writer:
            def append_received(self, fragment):
                with namespace.env.retained.journal.lock:
                    namespace.check_append(fragment.canonical)
                    raw = fragment.canonical
                    run['fragments'].append(raw.decode())
                    run['revision'] += 1
                    if namespace.fail == 'append':
                        raise ValueError('synthetic uncertain attachment write')
                    ack = {'reservation_id': run['reservation_id'], 'invocation_id': run['invocation'],
                           'ordinal': len(run['fragments']) - 1, 'sha256': hashlib.sha256(raw).hexdigest(),
                           'retained_revision': run['revision'], 'retention_mode': 'audit-only'}
                    if namespace.fail == 'ack':
                        ack['sha256'] = '0' * 64
                    return ack

            def finish(self, raw):
                with namespace.env.retained.journal.lock:
                    namespace.check_append(raw)
                    if namespace.fail == 'finish':
                        raise ValueError('synthetic uncertain finish')
                    doc = json.loads(raw)
                    assert doc['invocation_id'] == run['invocation']
                    assert len(run['fragments']) == 7
                    run['complete'] = True
                    namespace.saved['issued'][doc['artifact_id']] = {
                        'canonical': raw.decode(), 'invocation_id': run['invocation'],
                        'reservation_id': run['reservation_id'], 'sha256': hashlib.sha256(raw).hexdigest()}
                    return {'sha256': hashlib.sha256(raw).hexdigest(), 'invocation_id': run['invocation'],
                            'reservation_id': run['reservation_id'], 'retained_revision': run['revision']}
        writer = Writer()
        writer.reservation_id = run['reservation_id']
        return writer

    def resolve(self, ref, *, phase, proof, schema):
        issued = self.saved['issued'].get(str(ref.artifact_id))
        if issued is None:
            raise ValueError('unregistered synthetic artifact')
        raw = issued['canonical'].encode()
        if hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise ValueError('mutated synthetic artifact')
        run = self.saved['runs'][issued['invocation_id']]
        if not run['complete']:
            raise ValueError('unfinished synthetic artifact')
        state = self.env.retained._load()
        events = [event for event in state['events'] if event['event_id'] == str(proof.observation.ref.observation_id)
                  and event['sha256'] == proof.observation.sha256]
        if (len(events) != 1 or state['slots'][events[0]['slot']]['reservation_id'] != run['reservation_id']
                or self.env.retained.lookup(proof.observation.ref) != proof
                or self.env.retained.lookup(schema.observation.ref) != schema):
            raise ValueError('synthetic retained proof/schema slot linkage differs')
        return transcripts()._inspect_transcript(raw, original=self.env.original, phase=phase,
            proof=proof, schema=schema, issuance=issued, attachments=run)


class PolicyEnvironment:
    def __init__(self, tmp_path, monkeypatch, *, request_limit=50, event_limit=128):
        self.state = {'retired': False, 'during_schema': None, 'duplicate': False, 'body_pad': 0, 'huge_schema': False}
        self.schema = {'type': 'object', 'properties': dict(EXCLUSION_SPEC.fields),
                       'required': list(EXCLUSION_SPEC.required_fields)}
        self.catalog = None
        def handler(request):
            if request.url.path == '/input/v1/data_type':
                rows = [{'id': CONTROL, 'name': 'Synthetic control', 'api_version': 'v1',
                         'recordable': True, 'deprecated': self.state['retired'], 'record_spec': {'type': 'event'}}]
                raw = (' ' * self.state['body_pad'] + json.dumps(rows, indent=1)).encode()
                self.catalog = raw
                return httpx.Response(200, content=raw,
                    headers=[('x-total-count', '1')] * (2 if self.state['duplicate'] else 1))
            if request.url.path.endswith('/schema'):
                if self.state['during_schema']:
                    self.state['during_schema']()
                raw = (' ' * (1024 * 1024 if self.state['huge_schema'] else 0) + json.dumps(self.schema, indent=2)).encode()
                return httpx.Response(200, content=raw)
            assert request.url.path == '/data/v1/records'
            return httpx.Response(200, content=self.state.get('control_body', b''))
        self.env = synthetic_client(monkeypatch, handler, request_limit=request_limit, event_limit=event_limit,
                                    store=PolicyFixture())
        self.namespace = SyntheticAttachments(self.env)
        self.env.retained.namespace = self.namespace
        self.env.client.client._attachments = self.namespace
        self.env.client.client._admission_ref = ArtifactRef(uuid4(), 'b' * 64)
        self.env.client.client._service_semantics_ref = ArtifactRef(uuid4(), 'c' * 64)
        self.path = tmp_path / 'synthetic-journal.sqlite'
        writable = CreationJournal(self.path)
        writable.resolved(ACCOUNT, EXCLUSION_SPEC, CONTROL)
        self.journal = CreationJournal(self.path, read_only=True)
        self.env.client.client._journal_identity = 'synthetic-original-journal'
        self.context = replace(self.env.context, observation_id=uuid4(), purpose='policy-read',
                               destination_data_type=CONTROL,
                               requested_target_ids=(UUID(CONTROL_ID),),
                               control_projection=((UUID(IDENTIFIER), UUID(CONTROL_ID)),))

    def capture(self, phase='before'):
        return transcripts()._capture_verifier(client=self.env.client.client, journal=self.journal,
            data_type=CONTROL, phase=phase, context=replace(self.context, observation_id=uuid4()),
            schema_context=_schema_context(self.context), on_observation=self.env.sink,
            request_guard=self.env.guard, request_budget=self.env.budget)

    def proof(self, phase='before'):
        captured, resolved = self.capture(phase)
        doc = json.loads(captured.canonical)
        context = captured.context
        proof = _retain(PolicyBindingProof, context, doc['started_at'], doc['completed_at'],
            {'control_data_type': CONTROL, 'control_schema_ref': doc['schema_ref'],
             'owned_binding_ref': {'artifact_id': doc['artifact_id'], 'sha256': hashlib.sha256(captured.canonical).hexdigest()},
             'phase': phase, 'parent_target_context_hash': self.env.original.sha256},
            captured.reservation, self.env.sink)
        schema = self.env.retained.lookup(EvidenceRef(UUID(doc['schema_ref']['observation_id']), doc['schema_ref']['sha256']))
        ref = ArtifactRef(UUID(doc['artifact_id']), hashlib.sha256(captured.canonical).hexdigest())
        checked = self.namespace.resolve(ref, phase=phase, proof=self.env.retained.lookup(proof.ref), schema=schema)
        return captured, resolved, proof, schema, checked


def test_actual_repeated_verifier_retains_raw_bytes_and_local_claim(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    captured, result, proof, schema, inspected = env.proof()
    doc = json.loads(captured.canonical)
    assert result.data_type == CONTROL and result.schema == schema.observation.schema
    assert [o['request_kind'] for o in doc['operations']] == [
        'account-proof', 'owned-list', 'account-proof', 'schema-get', 'account-proof', 'owned-list', 'account-proof']
    assert env.env.budget._remaining == 43 and len(env.env.calls) == 3
    blobs = {b['sha256']: __import__('base64').b64decode(b['bytes_base64']) for b in doc['raw_blobs']}
    assert env.catalog in blobs.values()
    assert b'fulcra_userid' not in env.catalog
    assert any(o['result'].get('validation') == 'local-unverified-jwt-claim' for o in doc['operations'])
    assert inspected.canonical == captured.canonical
    assert proof.document['details']['control_schema_ref'] == {'observation_id': str(schema.observation.ref.observation_id), 'sha256': schema.observation.sha256}


@pytest.mark.parametrize('change', ['retired', 'journal', 'missing', 'schema'])
def test_actual_repeated_checks_fail_without_healing(tmp_path, monkeypatch, change):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    def mutate():
        if change == 'retired':
            env.state['retired'] = True
        elif change == 'missing':
            env.path.unlink()
        elif change == 'journal':
            with env.journal._connect() as db:
                pass  # read-only handle cannot mutate; use original fixture writer below
            with CreationJournal(env.path)._connect() as db:
                db.execute("UPDATE creations SET status='pending',destination=NULL")
        else:
            env.schema['properties'].pop('action_id')
    env.state['during_schema'] = mutate
    with pytest.raises((ValueError, FileNotFoundError)):
        env.capture()
    assert not env.namespace.saved['issued']
    assert env.env.retained._load()['slots'][0]['state'] == 'started'
    assert next(iter(env.namespace.saved['runs'].values()))['fragments']


def test_duplicate_headers_are_preserved_in_failed_audit_not_proof(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    env.state['duplicate'] = True
    with pytest.raises(ValueError):
        env.capture()
    run = next(iter(env.namespace.saved['runs'].values()))
    fragment = json.loads(run['fragments'][-1])
    assert fragment['result']['response_header_occurrences'].count(['x-total-count', '1']) == 2
    assert not run['complete'] and not env.namespace.saved['issued']


@pytest.mark.parametrize('mutation', ['raw', 'issuance', 'slot', 'schema', 'proof', 'unfinished'])
def test_resolver_reopens_and_rejects_mutated_or_borrowed_lineage(tmp_path, monkeypatch, mutation):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    captured, _, proof, schema, _ = env.proof()
    saved = json.loads(json.dumps(env.namespace.saved))
    reopened = SyntheticAttachments(env.env, saved)
    doc = json.loads(captured.canonical)
    ref = ArtifactRef(UUID(doc['artifact_id']), hashlib.sha256(captured.canonical).hexdigest())
    assert reopened.resolve(ref, phase='before', proof=env.env.retained.lookup(proof.ref), schema=schema).canonical == captured.canonical
    issued = saved['issued'][doc['artifact_id']]
    run = saved['runs'][doc['invocation_id']]
    if mutation == 'raw':
        issued['canonical'] += ' '
    elif mutation == 'issuance':
        saved['issued'].clear()
    elif mutation == 'slot':
        run['reservation_id'] = str(uuid4())
    elif mutation == 'schema':
        schema = env.proof('after')[3]
    elif mutation == 'proof':
        proof = replace(proof, canonical=proof.canonical.replace(b'"before"', b'"after"'))
    else:
        run['complete'] = False
    with pytest.raises(ValueError):
        reopened.resolve(ref, phase='before', proof=env.env.retained.lookup(proof.ref), schema=schema)


@pytest.mark.parametrize('failure', ['append', 'ack', 'finish'])
def test_uncertain_attachment_blocks_next_operation_and_reopen(tmp_path, monkeypatch, failure):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    env.namespace.fail = failure
    with pytest.raises(ValueError):
        env.capture()
    assert not env.namespace.saved['issued']
    run = next(iter(env.namespace.saved['runs'].values()))
    assert not run['complete']
    assert env.env.retained._load()['slots'][0]['state'] == 'started'
    if failure in {'append', 'ack'}:
        assert not env.env.calls


@pytest.mark.parametrize('limit', range(7))
def test_each_nested_phase_budget_stops_without_hidden_request(tmp_path, monkeypatch, limit):
    env = PolicyEnvironment(tmp_path, monkeypatch, request_limit=limit)
    with pytest.raises(ValueError):
        env.capture()
    assert env.env.budget._remaining == 0
    assert len(env.env.calls) == sum(index < limit for index in [1, 3, 5])
    assert not env.namespace.saved['issued']


def test_proof_reservation_precedes_all_preparation_at_full_capacity(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    for _ in range(128):
        env.env.budget.reserve_events(1).start()
    with pytest.raises(ValueError):
        env.capture()
    assert not env.namespace.saved['runs'] and not env.env.calls
    assert env.env.budget._remaining == 50
    reopened = PolicyFixture(env.env.retained.journal)
    with pytest.raises(ValueError):
        RequestBudget(50, retained=reopened, original=env.env.original).reserve_events(1)


@pytest.mark.parametrize('kind', ['transcript', 'schema'])
def test_evidence_overflow_refuses_without_truncation(tmp_path, monkeypatch, kind):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    env.state['body_pad' if kind == 'transcript' else 'huge_schema'] = 1600000 if kind == 'transcript' else True
    with pytest.raises(ValueError):
        env.capture()
    assert not env.namespace.saved['issued']


def test_before_control_after_has_six_documents_and_sixteen_charges(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    before = env.proof('before')
    control_context = replace(env.context, observation_id=uuid4())
    control = env.env.client.observe_records(CONTROL, (CONTROL_ID,), **env.env.kwargs(control_context))
    after = env.proof('after')
    assert _validated_exclusion_rows(CONTROL, DESTINATION, {IDENTIFIER: IDENTIFIER}, {}, account_id=ACCOUNT) == {}
    reservation = env.env.budget.reserve_events(1)
    reservation.start()
    result_context = replace(env.env.context, observation_id=uuid4(), purpose='policy-read',
                             control_projection=((UUID(IDENTIFIER), UUID(CONTROL_ID)),))
    _retain(PolicyObservation, result_context, '2026-10-25T22:00:00.000000Z', '2026-10-25T22:00:00.000000Z',
        {'target_to_control_ids': [[IDENTIFIER, CONTROL_ID]], 'valid_exclusion_rows': [], 'excluded_target_ids': [],
         'record_read_ref': {'observation_id': str(control.ref.observation_id), 'sha256': control.sha256},
         'binding_before_ref': {'observation_id': str(before[2].ref.observation_id), 'sha256': before[2].sha256},
         'binding_after_ref': {'observation_id': str(after[2].ref.observation_id), 'sha256': after[2].sha256},
         'validation': 'validated-permanent-policy-rows'}, reservation, env.env.sink)
    assert env.env.budget._remaining == 34
    assert len(env.env.retained._load()['events']) == 6
    assert before[2].ref != after[2].ref and before[3].observation.ref != after[3].observation.ref
    assert before[1].schema == after[1].schema
    documents = [event['canonical'].encode() for event in env.env.retained._load()['events']]
    attachments = [run['canonical'].encode() for run in env.namespace.saved['issued'].values()]
    attachments += [fragment.encode() for run in env.namespace.saved['runs'].values() for fragment in run['fragments']]
    transcripts()._check_policy_chain_size(documents=documents, attachments=attachments)


def test_same_tuple_journal_replacement_is_not_original_opener(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    original_bytes = env.path.read_bytes()
    def replace_journal():
        replacement = tmp_path / 'replacement.sqlite'
        replacement.write_bytes(original_bytes)
        replacement.chmod(0o600)
        replacement.replace(env.path)
    env.state['during_schema'] = replace_journal
    with pytest.raises(ValueError, match='journal'):
        env.capture()
    assert not env.namespace.saved['issued']


def test_resolver_rejects_raw_schema_with_bool_substituted_in_acknowledged_schema(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    captured, _, proof, schema, _ = env.proof()
    # A tampered but internally acknowledged schema must not borrow a raw response digest.
    changed = schema.observation.document
    changed['details']['schema']['properties']['schema_version']['const'] = True
    changed['details']['schema_sha256'] = hashlib.sha256(_canonical(changed['details']['schema'])).hexdigest()
    from fulcra_common.record_observations import SchemaObservation, RetainedObservation
    fabricated = SchemaObservation(_canonical(changed))
    changed_schema = RetainedObservation(fabricated, replace(schema.commit, ref=fabricated.ref))
    raw = json.loads(captured.canonical)
    raw['schema_ref']['sha256'] = fabricated.sha256
    proof_doc = proof.document
    proof_doc['details']['control_schema_ref']['sha256'] = fabricated.sha256
    # Recreate all mutable fixture registry links, but raw body remains exactly received.
    raw_bytes = _canonical(raw)
    proof_doc['details']['owned_binding_ref']['sha256'] = hashlib.sha256(raw_bytes).hexdigest()
    changed_proof = PolicyBindingProof(_canonical(proof_doc))
    retained_proof = RetainedObservation(changed_proof, replace(env.env.retained.lookup(proof.ref).commit, ref=changed_proof.ref))
    issued = env.namespace.saved['issued'][raw['artifact_id']]
    issued.update(canonical=raw_bytes.decode(), sha256=hashlib.sha256(raw_bytes).hexdigest())
    with pytest.raises(ValueError, match='schema'):
        env.namespace.resolve(ArtifactRef(UUID(raw['artifact_id']), hashlib.sha256(raw_bytes).hexdigest()),
            phase='before', proof=retained_proof, schema=changed_schema)


def test_expiry_after_schema_sink_retains_received_fragments_only(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    env.env.after_sink = lambda observation: setattr(env.env, 'active', False)
    with pytest.raises(ValueError):
        env.capture()
    run = next(iter(env.namespace.saved['runs'].values()))
    assert len(run['fragments']) == 4
    assert [request.url.path for request in env.env.calls] == [
        '/input/v1/data_type', f'/data/v1/catalog/{CONTROL}/v1/schema']
    events = env.env.retained._load()['events']
    assert len(events) == 1 and json.loads(events[0]['canonical'])['kind'] == 'served-schema'
    assert not env.namespace.saved['issued']


@pytest.mark.parametrize('state', ['127', 'corrupt', 'uncertain'])
def test_retained_slot_competition_never_resets_before_nested_schema(tmp_path, monkeypatch, state):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    if state == '127':
        for _ in range(127):
            env.env.budget.reserve_events(1).start()
    else:
        slot = env.env.budget.reserve_events(1)
        if state == 'corrupt':
            env.env.retained.journal.bytes = b'{}'
        else:
            saved = env.env.retained._load()
            saved['slots'][slot.indices[0]]['state'] = 'uncertain'
            env.env.retained._save(saved)
    with pytest.raises((ValueError, KeyError)):
        env.capture()
    assert not env.namespace.saved['issued']
    if state == '127':
        assert len(env.env.retained._load()['slots']) == 128
        assert len(env.env.calls) == 1
        assert env.env.budget._remaining == 48
    else:
        assert not env.namespace.saved['runs'] and not env.env.calls


def test_overlarge_event_ceiling_does_not_prepare_client(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        RequestBudget(50, retained=env.env.retained, original=env.env.original, event_limit=129)
    assert not env.env.calls and not env.namespace.saved['runs']


def test_fixture_resolver_checks_actual_consumed_proof_slot_on_reopen(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    captured, _, proof, schema, _ = env.proof()
    state = env.env.retained._load()
    event = next(event for event in state['events'] if event['event_id'] == str(proof.ref.observation_id))
    state['slots'][event['slot']]['reservation_id'] = str(uuid4())
    env.env.retained._save(state)
    doc = json.loads(captured.canonical)
    with pytest.raises(ValueError, match='slot'):
        env.namespace.resolve(ArtifactRef(UUID(doc['artifact_id']), hashlib.sha256(captured.canonical).hexdigest()),
            phase='before', proof=env.env.retained.lookup(proof.ref), schema=schema)



def _retained_chain_payloads(env):
    documents = [event['canonical'].encode() for event in env.env.retained._load()['events']]
    attachments = [issued['canonical'].encode() for issued in env.namespace.saved['issued'].values()]
    attachments += [fragment.encode() for run in env.namespace.saved['runs'].values()
                    for fragment in run['fragments']]
    return documents, attachments


def _complete_actual_policy_chain(env):
    before = env.proof('before')
    control = env.env.client.observe_records(CONTROL, (CONTROL_ID,),
        **env.env.kwargs(replace(env.context, observation_id=uuid4())))
    after = env.proof('after')
    assert before[1].schema == after[1].schema
    rows = {row['id']: row for row in control.records}
    validated = _validated_exclusion_rows(CONTROL, DESTINATION, {CONTROL_ID: IDENTIFIER}, rows, account_id=ACCOUNT)
    reservation = env.env.budget.reserve_events(1)
    reservation.start()
    context = replace(env.env.context, observation_id=uuid4(), purpose='policy-read',
                      control_projection=((UUID(IDENTIFIER), UUID(CONTROL_ID)),))
    return _retain(PolicyObservation, context, '2026-10-25T22:00:00.000000Z', '2026-10-25T22:00:00.000000Z',
        {'target_to_control_ids': [[IDENTIFIER, CONTROL_ID]], 'valid_exclusion_rows': list(validated.values()),
         'excluded_target_ids': list(validated),
         'record_read_ref': {'observation_id': str(control.ref.observation_id), 'sha256': control.sha256},
         'binding_before_ref': {'observation_id': str(before[2].ref.observation_id), 'sha256': before[2].sha256},
         'binding_after_ref': {'observation_id': str(after[2].ref.observation_id), 'sha256': after[2].sha256},
         'validation': 'validated-permanent-policy-rows'}, reservation, env.env.sink)


def test_whole_chain_limit_precedes_actual_large_phase_finish(tmp_path, monkeypatch):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    env.schema['description'] = 'x' * 600000
    env.state['body_pad'] = 400000
    env.proof('before')
    earlier = tuple(_retained_chain_payloads(env)[0])
    rejected = None
    try:
        env.proof('after')
    except ValueError as error:
        rejected = error
    documents, attachments = _retained_chain_payloads(env)
    total = sum(map(len, documents + attachments))
    assert total <= 8388608, f'actual retained chain exceeded ceiling: {total}'
    assert rejected is not None and str(rejected) == 'policy chain retention exceeds evidence limit'
    assert tuple(documents[:len(earlier)]) == earlier
    assert len(env.namespace.saved['issued']) == 1
    assert len(env.env.calls) == 6
    assert env.env.retained._load()['slots'][-2]['state'] == 'started'


@pytest.mark.parametrize('evidence_size,overflows', [(0, False), (1200000, True)])
def test_six_document_retention_checks_actual_rows_before_result_append(tmp_path, monkeypatch, evidence_size, overflows):
    from fulcra_common.record_exclusions import _record
    env = PolicyEnvironment(tmp_path, monkeypatch)
    env.schema['description'] = 'x' * 500000
    env.state['body_pad'] = 300000
    if evidence_size:
        row = _record(CONTROL, DESTINATION, IDENTIFIER,
            action_id='bbbbbbbb-bbbb-4ccc-8ddd-eeeeeeeeeeee', requested_at=_fixture.NOW,
            reason='operator', evidence=['x' * evidence_size])
        assert row['id'] == CONTROL_ID
        env.state['control_body'] = _canonical(row)
    rejected = None
    try:
        _complete_actual_policy_chain(env)
    except ValueError as error:
        rejected = error
    documents, attachments = _retained_chain_payloads(env)
    total = sum(map(len, documents + attachments))
    assert total <= 8388608, f'actual six-document retention exceeded ceiling: {total}'
    if overflows:
        assert rejected is not None and str(rejected) == 'policy chain retention exceeds evidence limit'
        assert len(documents) == 5
        assert json.loads(documents[-1])['kind'] == 'policy-binding-proof'
        assert env.env.retained._load()['slots'][-1]['state'] == 'consumed'
        assert not env.env.retained._load()['slots'][-1]['committed']
    else:
        assert rejected is None
        assert len(documents) == 6
    assert len(env.env.calls) == 7


@pytest.mark.parametrize('replacement', [False, 0.0])
def test_retained_fragment_requires_exact_canonical_bytes(tmp_path, monkeypatch, replacement):
    env = PolicyEnvironment(tmp_path, monkeypatch)
    captured, _, proof, schema, _ = env.proof()
    doc = json.loads(captured.canonical)
    run = env.namespace.saved['runs'][doc['invocation_id']]
    fragment = json.loads(run['fragments'][1])
    assert type(fragment['result']['request_body']['byte_length']) is int
    fragment['result']['request_body']['byte_length'] = replacement
    run['fragments'][1] = _canonical(fragment).decode()
    reached = []
    inspector = transcripts()._inspect_transcript
    def inspect(*args, **kwargs):
        reached.append(True)
        return inspector(*args, **kwargs)
    monkeypatch.setattr(transcripts(), '_inspect_transcript', inspect)
    with pytest.raises(ValueError, match='^transcript attachment bytes differ from operation$'):
        env.namespace.resolve(ArtifactRef(UUID(doc['artifact_id']), hashlib.sha256(captured.canonical).hexdigest()),
            phase='before', proof=env.env.retained.lookup(proof.ref), schema=schema)
    assert reached == [True]
