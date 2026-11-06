"""Private Trakt current-credential observations; never historical attribution.

Probes are detached from mutable bindings. Only locked acceptance publishes a
selection pin; current nested guards use local admission evidence without I/O to
Trakt. Eight seconds is a cooperative deadline, not hard cancellation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from uuid import uuid4

import httpx

from .config_leases import task_mutation_scope
from .trakt_source import (SourceHold, _hash, _hold, _strict_tree,
                           _provider_identifier as _identifier, _IDENTITY_ENDPOINT as ENDPOINT,
                           _validate_identity_observation)

MISMATCH = 'Trakt account differs from the saved source identity; source selection review required.'
MAX_BYTES = 1024 * 1024
PROBE_SECONDS = 8.0


def _unknown():
    raise SourceHold('Trakt source identity unavailable; preserve pending work') from None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _unknown()
        result[key] = value
    return result


def parse_identity(raw):
    try:
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            _unknown()
        value = json.loads(raw.decode('utf-8', errors='strict'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: _unknown())
        _strict_tree(value)
        if (not isinstance(value, dict) or not isinstance(value.get('user'), dict)
                or not isinstance(value['user'].get('ids'), dict)):
            _unknown()
        return _identifier(value['user']['ids'].get('uuid'))
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        _unknown()


@dataclass(frozen=True, repr=False)
class ProbeWindow:
    bounds: object
    deadline: float = field(default_factory=lambda: time.monotonic() + PROBE_SECONDS)

    def check(self):
        self.bounds.check()
        if time.monotonic() >= self.deadline:
            _unknown()

    def timeout(self):
        self.check()
        return min(self.bounds.timeout(), self.deadline - time.monotonic())


@dataclass(frozen=True, repr=False)
class ProbeSubject:
    selection: str
    consent: str
    generation: str
    digest: str
    successor: str | None
    bearer: str
    client: str
    # Canonical immutable complete row/tuple encodings allow exact comparison,
    # including unrelated legacy values. Both stay in private memory only.
    row: bytes
    values: bytes
    window: ProbeWindow

    def provenance(self):
        return {k: getattr(self, k) for k in
                ('selection', 'consent', 'generation', 'digest', 'successor')}

    def headers(self):
        return {'Authorization': 'Bearer ' + self.bearer, 'trakt-api-key': self.client,
                'trakt-api-version': '2', 'Content-Type': 'application/json',
                'Accept-Encoding': 'identity'}


def snapshot_probe_subject(binding, window):
    from .trakt_source import _encoded
    window.check()
    binding.authority()
    window.check()
    with task_mutation_scope(), binding.owner._locked() as journal:
        binding.owner._check(binding, journal)
        binding.local()
        window.check()
        subject = ProbeSubject(binding.owner.key, binding.row['consent'],
            binding.row['generation'], binding.row['digest'], binding.row.get('successor'),
            binding.values['access_token'], binding.values['client_id'],
            _encoded(binding.row), _encoded(binding.values), window)
    binding.local()
    window.check()
    return subject


@dataclass(frozen=True, repr=False)
class Observation:
    subject: ProbeSubject
    identifier: str
    endpoint: str
    observed_at: float

    def record(self):
        return {'subject': self.subject.provenance(), 'identifier': self.identifier,
                'endpoint': self.endpoint, 'observed_at': self.observed_at}


def probe_identity(subject, bounds, *, transport=None):
    window = subject.window
    if window.bounds is not bounds:
        _unknown()
    try:
        window.check()
        bounds.charge_request()
        with httpx.Client(transport=transport, follow_redirects=False) as client:
            window.check()
            with client.stream('GET', ENDPOINT, headers=subject.headers(), timeout=window.timeout()) as response:
                window.check()
                if str(response.url) != ENDPOINT or response.history:
                    _unknown()
                encoding = response.headers.get_list('Content-Encoding')
                if encoding and (len(encoding) != 1 or encoding[0].lower() != 'identity'):
                    _unknown()
                lengths = response.headers.get_list('Content-Length')
                transfers = response.headers.get_list('Transfer-Encoding')
                if ('Content-Range' in response.headers
                        or (transfers and (lengths or len(transfers) != 1
                                           or transfers[0].lower() != 'chunked'))):
                    _unknown()
                declared = None
                if lengths:
                    if (len(lengths) != 1 or not lengths[0].isascii()
                            or not lengths[0].isdecimal() or len(lengths[0]) > 10):
                        _unknown()
                    declared = int(lengths[0])
                    if declared > MAX_BYTES:
                        _unknown()
                chunks, size = [], 0
                for chunk in response.iter_raw():
                    window.check()
                    bounds.charge_bytes(len(chunk))
                    size += len(chunk)
                    if size > MAX_BYTES:
                        _unknown()
                    chunks.append(chunk)
                    window.check()
                if declared is not None and declared != size:
                    _unknown()
                window.check()
                raw = b''.join(chunks)
            window.check()
            if response.status_code in (401, 403):
                raise httpx.HTTPStatusError('Trakt source authorization failed',
                                           request=response.request, response=response)
            if response.status_code != 200:
                _unknown()
            identifier = parse_identity(raw)
            window.check()
            return Observation(subject, identifier, ENDPOINT, time.time())
    except (httpx.HTTPStatusError, SourceHold):
        raise
    except Exception:
        _unknown()


def _same(binding, subject, journal):
    from .trakt_source import _encoded
    binding.owner._check(binding, journal)
    if (binding.owner.key != subject.selection or _encoded(binding.row) != subject.row
            or _encoded(binding.values) != subject.values):
        _hold()
    binding.local()


def accept_identity(binding, subject, observation):
    window = subject.window
    window.check()
    if not isinstance(observation, Observation) or observation.subject is not subject:
        _unknown()
    _validate_identity_observation(observation.record(), subject.selection)
    binding.authority()
    window.check()
    with task_mutation_scope(), binding.owner._locked() as journal:
        _same(binding, subject, journal)
        window.check()
        pins = journal['identities']
        pin = pins.get(subject.selection)
        if pin is None:
            pin = {'version': 1, 'first': observation.record(), 'revision': uuid4().hex, 'state': 'active'}
            pins[subject.selection] = pin
            binding.owner._save(journal)
        elif pin['state'] == 'held':
            raise SourceHold(MISMATCH)
        elif pin['first']['identifier'] != observation.identifier:
            pin.update(state='held', revision=uuid4().hex, conflict=observation.record())
            binding.owner._save(journal)
            raise SourceHold(MISMATCH)
        binding.local()
        window.check()
        proof = IdentityAdmission(subject, pin['revision'], _hash(observation.identifier))
    binding.local()
    window.check()
    return proof


@dataclass(frozen=True, repr=False)
class IdentityAdmission:
    subject: ProbeSubject
    revision: str
    identity_hash: str

    def check_local(self, binding):
        with binding.owner._locked() as journal:
            _same(binding, self.subject, journal)
            pin = journal['identities'].get(self.subject.selection)
            if pin is not None and pin['state'] == 'held':
                raise SourceHold(MISMATCH)
            if (pin is None or pin['state'] != 'active' or pin['revision'] != self.revision
                    or _hash(pin['first']['identifier']) != self.identity_hash):
                _unknown()
        binding.local()


class CurrentIdentity:
    """Owned run handle: acquisition local checks, then mandatory terminal proof."""
    def __init__(self, binding):
        self.binding = binding
        self.admission = None
        self.required = False

    def check_local_after_authority(self):
        if self.required:
            if self.admission is None:
                _unknown()
            self.admission.check_local(self.binding)
        else:
            self.binding.check_local_after_authority()

    def check_before_current(self):
        self.binding.authority()
        if not self.required:
            _unknown()
        self.check_local_after_authority()

    def observe(self, bounds, *, window=None, transport=None, terminal=False):
        subject = snapshot_probe_subject(self.binding, window or ProbeWindow(bounds))
        observation = probe_identity(subject, bounds, transport=transport)
        proof = accept_identity(self.binding, subject, observation)
        if terminal:
            self.admission = proof
            self.required = True
            self.check_local_after_authority()
        return proof
