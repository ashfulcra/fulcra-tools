"""Untrusted historical verifier transcripts and strict inspection mechanics.

There is deliberately no owned-policy issuer, trusted namespace, admission gate
or production factory here. Fixture namespaces alone compose policy proofs.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

import httpx

from ._historical_observed_transport import _ClosedObservedClient, _ReceivedFragment, _blob, _closed, _ref
from .record_observations import (
    ArtifactRef, _HEADERS, _canonical, _fields, _hash, _hooks, _now,
    _integer, _response, _time, _url_origin, _wire_uuid,
)

_LIMIT = 2 * 1024 * 1024
_CHAIN_LIMIT = 8 * 1024 * 1024
_SEQUENCE = ('account-proof', 'owned-list', 'account-proof', 'schema-get',
             'account-proof', 'owned-list', 'account-proof')


@dataclass(frozen=True, repr=False)
class _UntrustedOwnedTranscript:
    canonical: bytes
    context: object = field(default=None, repr=False)
    reservation: object = field(default=None, repr=False)


def _decoded(value):
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        raise ValueError('transcript bytes encoding is invalid') from None
    if base64.b64encode(raw).decode('ascii') != value:
        raise ValueError('transcript bytes encoding is noncanonical')
    return raw


def _load(raw):
    from .data_types import _proof_json
    doc = _proof_json(raw, limit=_LIMIT)
    if _canonical(doc) != raw:
        raise ValueError('transcript bytes are noncanonical')
    return doc


class _VerifierCapture:
    """Actual helper observer; all its output remains untrusted inspection data."""
    def __init__(self, *, client, journal, phase, context, schema_context,
                 on_observation, request_guard, request_budget, reservation):
        self.client, self.journal, self.phase = client, journal, phase
        info = journal.path.stat(follow_symlinks=False)
        self.journal_file_identity = (info.st_dev, info.st_ino)
        self.context, self.schema_context = context, schema_context
        self.sink, self.guard, self.budget = on_observation, request_guard, request_budget
        self.reservation = reservation
        self.invocation_id, self.artifact_id = uuid4(), uuid4()
        self.started_at = _now()
        self.journal_before = self.journal_after = self.schema = None
        self.operations, self.raw_blobs = [], []
        self.ordinal, self.revision = 0, -1
        self.writer = client._attachments.begin(invocation_id=self.invocation_id, context=context,
            phase=phase, proof_reservation=reservation, admission_ref=client._admission_ref)
        contract = Path(__file__).with_name('record_exclusions_contract.json').read_bytes()
        self.raw_blobs.append({'role': 'packaged-exclusion-contract', **_blob(contract)})

    @property
    def hooks(self):
        return {'request_guard': self.guard, 'request_budget': self.budget,
                'observation_context': self.context, '_capture': self}

    def journal_received(self, prior, *, final):
        from .record_exclusions import EXCLUSION_SPEC
        info = self.journal.path.stat(follow_symlinks=False)
        if (info.st_dev, info.st_ino) != self.journal_file_identity:
            raise ValueError("original journal file identity changed")
        if prior is None:
            raise ValueError('original journal binding is missing')
        snapshot = {'journal_identity': self.client._journal_identity, 'account_id': self.context.account_id,
                    'spec_key': EXCLUSION_SPEC.key, 'spec_fingerprint': prior[0],
                    'status': prior[1], 'destination': prior[2]}
        if final:
            self.journal_after = snapshot
        else:
            self.journal_before = snapshot

    def account_received(self, actual, *, started_at):
        self.append_received(_ReceivedFragment(_canonical({'request_kind': 'account-proof',
            'admission_ref': _ref(self.client._admission_ref), 'started_at': started_at, 'completed_at': _now(),
            'result': {'decoded_account': actual, 'validation': 'local-unverified-jwt-claim'}})))

    def append_received(self, fragment):
        if type(fragment) is not _ReceivedFragment:
            raise ValueError('received fragment type is invalid')
        operation = _load(fragment.canonical)
        _fields(operation, 'request_kind admission_ref started_at completed_at result')
        if self.ordinal >= len(_SEQUENCE) or operation['request_kind'] != _SEQUENCE[self.ordinal]:
            raise ValueError('verifier operation sequence changed')
        operation['operation_id'] = str(uuid4())
        if operation['request_kind'] != 'account-proof':
            for name in ('request_body', 'response_body'):
                blob = operation['result'][name]
                self.raw_blobs.append({'role': f'{self.ordinal}-{name}', **blob})
                operation['result'][name] = {'sha256': blob['sha256'], 'byte_length': blob['byte_length']}
        self.operations.append(operation)
        # Per-phase bound first; the writer must also atomically bound all
        # retained chain documents/staged/final attachments before appending.
        self._check_size()
        ack = self.writer.append_received(fragment)
        expected = {'reservation_id': self.writer.reservation_id, 'invocation_id': str(self.invocation_id),
                    'ordinal': self.ordinal, 'sha256': hashlib.sha256(fragment.canonical).hexdigest()}
        _fields(ack, 'reservation_id invocation_id ordinal sha256 retained_revision retention_mode')
        if (any(ack[key] != value for key, value in expected.items())
                or type(ack['retained_revision']) is not int or ack['retained_revision'] <= self.revision
                or ack['retention_mode'] != 'audit-only'):
            raise ValueError('attachment acknowledgement differs or is unknown')
        self.revision = ack['retained_revision']
        self.ordinal += 1

    def read_schema(self):
        from .data_types import _observe_schema_core
        return _observe_schema_core(client=self.client, account_id=self.context.account_id,
            data_type=self.context.destination_data_type, context=self.schema_context,
            on_observation=self.sink, request_guard=self.guard, request_budget=self.budget,
            _capture=self)[0]

    def schema_received(self, observation):
        self.schema = observation

    def _document(self):
        return {'version': 1, 'kind': 'collect-owned-binding-transcript', 'artifact_id': str(self.artifact_id),
            'invocation_id': str(self.invocation_id), 'original_target_sha256': self.context.original_target.sha256,
            'account_id': self.context.account_id, 'control_data_type': self.context.destination_data_type,
            'phase': self.phase, 'proof_observation_id': str(self.context.observation_id),
            'schema_ref': None if self.schema is None else {'observation_id': str(self.schema.ref.observation_id), 'sha256': self.schema.sha256},
            'admission_ref': _ref(self.client._admission_ref), 'service_semantics_ref': _ref(self.client._service_semantics_ref),
            'journal_before': self.journal_before, 'journal_after': self.journal_after,
            'operations': self.operations, 'raw_blobs': self.raw_blobs,
            'started_at': self.started_at, 'completed_at': _now()}

    def _check_size(self):
        doc = self._document()
        # Exact proof envelope shape, with fixed-length hashes/IDs even before finish.
        proof = {**self.context.document, 'kind': 'policy-binding-proof', 'started_at': self.started_at,
            'received_at': doc['completed_at'], 'details': {'control_data_type': self.context.destination_data_type,
            'control_schema_ref': doc['schema_ref'] or {'observation_id': str(self.schema_context.observation_id), 'sha256': '0' * 64},
            'owned_binding_ref': {'artifact_id': str(self.artifact_id), 'sha256': '0' * 64},
            'phase': self.phase, 'parent_target_context_hash': self.context.original_target.sha256}}
        if len(_canonical(doc)) + len(_canonical(proof)) > _LIMIT:
            raise ValueError('transcript plus proof exceeds evidence limit')

    def finish(self):
        if (self.ordinal != 7 or self.schema is None or self.journal_before != self.journal_after
                or self.journal_before is None):
            raise ValueError('incomplete verifier transcript')
        self._check_size()
        raw = _canonical(self._document())
        ack = self.writer.finish(raw)
        _fields(ack, 'sha256 invocation_id reservation_id retained_revision')
        if (ack['sha256'] != hashlib.sha256(raw).hexdigest() or ack['invocation_id'] != str(self.invocation_id)
                or ack['reservation_id'] != self.writer.reservation_id
                or type(ack['retained_revision']) is not int or ack['retained_revision'] < self.revision):
            raise ValueError('transcript finish acknowledgement differs or is unknown')
        return _UntrustedOwnedTranscript(raw, self.context, self.reservation)


def _capture_verifier(*, client, journal, data_type, phase, context, schema_context,
                      on_observation, request_guard, request_budget):
    from .record_exclusions import _verify_exclusion_type_core
    if (type(client) is not _ClosedObservedClient or not _closed(client._http)
            or phase not in {'before', 'after'} or not journal.read_only
            or data_type != context.destination_data_type or context.account_id != client._admission.account_id
            or schema_context.purpose != 'schema-read' or schema_context.requested_target_ids
            or schema_context.destination_data_type != data_type
            or schema_context.original_target != context.original_target
            or schema_context.observation_id == context.observation_id):
        raise ValueError('inactive verifier capture scope differs')
    _hooks(request_guard, request_budget, context)
    reservation = request_budget.reserve_events(1)
    request_guard(kind='owned-list', context=context)
    reservation.start()
    capture = _VerifierCapture(client=client, journal=journal, phase=phase, context=context,
        schema_context=schema_context, on_observation=on_observation, request_guard=request_guard,
        request_budget=request_budget, reservation=reservation)
    http = client._client()
    if http._capture is not None:
        raise ValueError('nested verifier capture is unsupported')
    http._capture = capture
    try:
        resolved = _verify_exclusion_type_core(client=client, account_id=context.account_id,
            journal=journal, data_type=data_type, _capture=capture)
        return capture.finish(), resolved
    finally:
        http._capture = None


def _inspect_transcript(raw, *, original, phase, proof, schema, issuance, attachments):
    """Strict UNTRUSTED inspection. Caller-supplied fixture records grant no authority."""
    from .data_types import _parse_owned_types, _proof_json, _resolve_candidate
    from .record_exclusions import EXCLUSION_SPEC
    from .v1 import _json_equal
    doc = _load(raw)
    _fields(doc, 'version kind artifact_id invocation_id original_target_sha256 account_id control_data_type phase '
        'proof_observation_id schema_ref admission_ref service_semantics_ref journal_before journal_after operations raw_blobs started_at completed_at')
    if type(doc['version']) is not int or doc['version'] != 1 or doc['kind'] != 'collect-owned-binding-transcript':
        raise ValueError('transcript version/kind is invalid')
    for name in ('artifact_id', 'invocation_id', 'proof_observation_id'):
        _wire_uuid(doc[name])
    _hash(doc['original_target_sha256'])
    _time(doc['started_at'])
    _time(doc['completed_at'])
    if doc['started_at'] > doc['completed_at']:
        raise ValueError('transcript times are reversed')
    _fields(issuance, 'canonical invocation_id reservation_id sha256')
    if (issuance['canonical'].encode() != raw or issuance['sha256'] != hashlib.sha256(raw).hexdigest()
            or issuance['invocation_id'] != doc['invocation_id']
            or attachments['invocation'] != doc['invocation_id'] or attachments['phase'] != phase
            or attachments['reservation_id'] != issuance['reservation_id']
            or attachments['original'] != original.sha256 or attachments['complete'] is not True
            or len(attachments['fragments']) != 7):
        raise ValueError('transcript issuance/slot linkage differs')
    details = proof.observation.document['details']
    schema_ref = {'observation_id': str(schema.observation.ref.observation_id), 'sha256': schema.observation.sha256}
    if (doc['original_target_sha256'] != original.sha256 or doc['account_id'] != original.document['account_id']
            or doc['phase'] != phase or details['phase'] != phase
            or doc['proof_observation_id'] != str(proof.observation.ref.observation_id)
            or details['control_data_type'] != doc['control_data_type']
            or doc['schema_ref'] != schema_ref or details['control_schema_ref'] != schema_ref
            or details['owned_binding_ref'] != {'artifact_id': doc['artifact_id'], 'sha256': hashlib.sha256(raw).hexdigest()}
            or proof.observation.document['original_target'] != original.document
            or schema.observation.document['original_target'] != original.document):
        raise ValueError('transcript proof/schema/original linkage differs')
    for name in ('admission_ref', 'service_semantics_ref'):
        _fields(doc[name], 'artifact_id sha256')
        ArtifactRef(_wire_uuid(doc[name]['artifact_id']), doc[name]['sha256'])
    journal = doc['journal_before']
    _fields(journal, 'journal_identity account_id spec_key spec_fingerprint status destination')
    if (journal != doc['journal_after'] or not isinstance(journal['journal_identity'], str) or not journal['journal_identity']
            or journal['account_id'] != doc['account_id'] or journal['spec_key'] != EXCLUSION_SPEC.key
            or journal['spec_fingerprint'] != EXCLUSION_SPEC.fingerprint or journal['status'] != 'resolved'
            or journal['destination'] != doc['control_data_type']):
        raise ValueError('transcript journal binding differs')
    blobs = {}
    for blob in doc['raw_blobs']:
        _fields(blob, 'role sha256 byte_length bytes_base64')
        body = _decoded(blob['bytes_base64'])
        if (hashlib.sha256(body).hexdigest() != blob['sha256'] or type(blob['byte_length']) is not int
                or len(body) != blob['byte_length'] or not isinstance(blob['role'], str)):
            raise ValueError('transcript raw blob differs')
        blobs[blob['sha256']] = body
    contract = Path(__file__).with_name('record_exclusions_contract.json').read_bytes()
    if not any(blob['role'] == 'packaged-exclusion-contract' and blobs[blob['sha256']] == contract for blob in doc['raw_blobs']):
        raise ValueError('transcript packaged contract differs')
    operations = doc['operations']
    if len(operations) != 7 or tuple(op['request_kind'] for op in operations) != _SEQUENCE:
        raise ValueError('transcript operation sequence differs')
    catalogs, served, operation_ids = [], None, set()
    previous = doc['started_at']
    for index, operation in enumerate(operations):
        _fields(operation, 'operation_id request_kind admission_ref started_at completed_at result')
        identifier = _wire_uuid(operation['operation_id'])
        if identifier in operation_ids:
            raise ValueError('transcript operation identity repeated')
        operation_ids.add(identifier)
        _time(operation['started_at'])
        _time(operation['completed_at'])
        if not previous <= operation['started_at'] <= operation['completed_at'] <= doc['completed_at']:
            raise ValueError('transcript operation times differ')
        previous = operation['completed_at']
        if operation['admission_ref'] != doc['admission_ref']:
            raise ValueError('transcript admission linkage differs')
        result = operation['result']
        staged_raw = attachments['fragments'][index].encode()
        _load(staged_raw)  # retain strict parsing/canonical-shape validation
        expected_staged = {key: value for key, value in operation.items() if key != 'operation_id'}
        if operation['request_kind'] == 'account-proof':
            _fields(result, 'decoded_account validation')
            if result != {'decoded_account': doc['account_id'], 'validation': 'local-unverified-jwt-claim'}:
                raise ValueError('transcript local account differs')
        else:
            _fields(result, 'request response raw_request_target request_body response_body response_header_occurrences')
            for name in ('request_body', 'response_body'):
                ref = result[name]
                _fields(ref, 'sha256 byte_length')
                _hash(ref['sha256'])
                _integer(ref['byte_length'])
                if ref['sha256'] not in blobs or len(blobs[ref['sha256']]) != ref['byte_length']:
                    raise ValueError('transcript body reference differs')
                expected_staged = json.loads(_canonical(expected_staged))
                expected_staged['result'][name] = _blob(blobs[ref['sha256']])
            request, response = result['request'], result['response']
            _fields(request, 'method origin path ordered_query destination_data_type requested_target_ids')
            path = '/input/v1/data_type' if operation['request_kind'] == 'owned-list' else f"/data/v1/catalog/{doc['control_data_type']}/v1/schema"
            query = [['include_deprecated', 'true']] if operation['request_kind'] == 'owned-list' else []
            if request['method'] != 'GET' or request['path'] != path or request['ordered_query'] != query:
                raise ValueError('transcript HTTP scope differs')
            if request['destination_data_type'] != doc['control_data_type'] or blobs[result['request_body']['sha256']] != b'':
                raise ValueError('transcript request scope/body differs')
            expected_ids = [] if operation['request_kind'] == 'schema-get' else proof.observation.document['requested_target_ids']
            if (request['requested_target_ids'] != expected_ids
                    or request['origin'] != schema.observation.document['details']['request']['origin']):
                raise ValueError('transcript original HTTP scope differs')
            origin = request['origin']
            _fields(origin, 'scheme host port')
            if origin['scheme'] != 'https':
                raise ValueError('transcript origin differs')
            port = '' if origin['port'] is None else ':' + str(origin['port'])
            target = httpx.URL(f"https://{origin['host']}{port}" + _decoded(result['raw_request_target']).decode('ascii'))
            if _url_origin(target) != origin or target.path != path or [list(pair) for pair in target.params.multi_items()] != query:
                raise ValueError('transcript raw target differs')
            seen = {}
            for name, value in result['response_header_occurrences']:
                if name not in _HEADERS or name in seen or not isinstance(value, str):
                    raise ValueError('transcript response headers repeated or unsupported')
                seen[name] = value
            if response['headers'] != {name: seen.get(name) for name in _HEADERS}:
                raise ValueError('transcript response headers differ')
            _response(response, origin=origin, path=path, query=query, account=doc['account_id'], status={200})
            body = blobs[result['response_body']['sha256']]
            if response['body_sha256'] != hashlib.sha256(body).hexdigest() or response['body_byte_length'] != len(body):
                raise ValueError('transcript actual response digest differs')
            if operation['request_kind'] == 'owned-list':
                catalogs.append(_parse_owned_types(body, account=doc['account_id'], totals=seen))
            else:
                served = _proof_json(body, limit=1024 * 1024)
                if (not _json_equal(served, schema.observation.schema)
                        or response != schema.observation.document['details']['response']):
                    raise ValueError('transcript actual served schema differs')
        if staged_raw != _canonical(expected_staged):
            raise ValueError('transcript attachment bytes differ from operation')
    candidates = [[row for row in rows if row['id'] == doc['control_data_type']] for rows in catalogs]
    if len(candidates) != 2 or len(candidates[0]) != 1 or candidates[0] != candidates[1]:
        raise ValueError('transcript repeated candidate differs')
    _resolve_candidate(None, None, candidates[0][0], EXCLUSION_SPEC, doc['account_id'], served_schema=served)
    if len(raw) + len(proof.observation.canonical) > _LIMIT:
        raise ValueError('transcript plus proof exceeds evidence limit')
    return _UntrustedOwnedTranscript(raw)


def _check_chain_retention(*, documents, attachments):
    """Check actual prospective retained bytes BEFORE their storage mutation.

    The bound writer/sink supplies a fresh complete read of the chain's documents
    and every staged/final attachment, including the proposed append. This pure
    untrusted check supplies neither persistence nor authority; the owning store
    must hold its mutation exclusion across the read/check/write.
    """
    payloads = (*documents, *attachments)
    if (len(documents) > 6 or any(type(raw) is not bytes for raw in payloads)
            or sum(len(raw) for raw in payloads) > _CHAIN_LIMIT):
        raise ValueError('policy chain retention exceeds evidence limit')


def _check_policy_chain_size(*, documents, attachments):
    """Inspect a complete supplied chain; storage also checks each prospective append."""
    if len(documents) != 6:
        raise ValueError('six-document policy chain is incomplete')
    _check_chain_retention(documents=documents, attachments=attachments)
