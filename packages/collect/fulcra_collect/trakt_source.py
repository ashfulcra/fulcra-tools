"""Trakt-only local source ownership and recoverable rotating-pair publication.

The journal is nonsecret. Secret receipts have deterministic attempt locators.
A consumed refresh fingerprint is permanent, including after successful commit.
No provider I/O or session.guard is called while the short source lock is held.
This fences participating local writers only. Selection pins are private current
credential observations, never historical source attribution.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import stat
import tempfile
import time
from uuid import uuid4

from . import config
from .config_leases import settings_update_scope, task_mutation_scope

_FIELDS = ('client_id', 'access_token', 'refresh_token', 'client_secret', 'redirect_uri',
           'created_at', 'expires_in')
_REQUIRED = frozenset(('client_id', 'access_token', 'refresh_token'))
_PAIR = frozenset(('access_token', 'refresh_token'))
_LIMIT = 1048576


class SourceHold(RuntimeError):
    """Source is changed, incomplete, disconnected, or UNKNOWN; do not replay."""


def _hold():
    raise SourceHold('Trakt source authority unavailable; preserve pending work and reconnect explicitly')


def _os_user_root():
    # Deliberately independent of HOME, config_dir(), profile and client ID.
    return Path(pwd.getpwuid(os.geteuid()).pw_dir) / '.local/state/fulcra-trakt'


_IDENTITY_ENDPOINT = 'https://api.trakt.tv/users/settings'


def _provider_identifier(value):
    if (not isinstance(value, str) or not value.strip()
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _hold()
    if len(value.encode('utf-8')) > 1024:
        _hold()
    return value


def _strict_tree(value, depth=0):
    if depth > 128:
        _hold()
    if isinstance(value, str):
        value.encode('utf-8', errors='strict')
    elif isinstance(value, float) and not math.isfinite(value):
        _hold()
    elif isinstance(value, dict):
        for k, v in value.items():
            _strict_tree(k, depth + 1)
            _strict_tree(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _strict_tree(v, depth + 1)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _hold()
        result[key] = value
    return result


def _decode(raw):
    try:
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8', errors='strict')
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: _hold())
        _strict_tree(value)
        return value
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        _hold()


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _read_json(path, *, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT
                or (private and (info.st_uid != os.geteuid() or info.st_mode & 0o077))):
            _hold()
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        _hold()
    return _decode(raw)


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = _encoded(value)
    if len(raw) > _LIMIT:
        _hold()
    fd, name = tempfile.mkstemp(prefix='.trakt-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _values(raw, *, complete=True, file=False):
    if not isinstance(raw, dict):
        _hold()
    for key in _FIELDS:
        value = raw.get(key)
        if key in _REQUIRED:
            if complete and (not isinstance(value, str) or not value.strip()):
                _hold()
        if value is not None:
            if key in ('created_at', 'expires_in'):
                try:
                    if type(value) is bool or not math.isfinite(float(value)):
                        _hold()
                except (ValueError, TypeError, OverflowError):
                    _hold()
            elif not isinstance(value, str) or len(value.encode()) > 65536:
                _hold()
    if file and complete:
        if not isinstance(raw.get('client_secret'), str) or not raw['client_secret']:
            _hold()
        for key in ('created_at', 'expires_in'):
            if type(raw.get(key)) not in (int, float):
                _hold()
    # Validate unrelated legacy values too, without discarding them.
    try:
        _encoded(raw)
    except (ValueError, TypeError):
        _hold()
    return deepcopy(raw)


def _hex(value, length):
    return isinstance(value, str) and len(value) == length and all(c in '0123456789abcdef' for c in value)


def _validate_journal(journal):
    if not isinstance(journal, dict) or type(journal.get('version')) is not int:
        _hold()
    fields = {'version', 'sources', 'attempts', 'consumed'}
    if journal['version'] == 2:
        fields.add('identities')
    elif journal['version'] != 1:
        _hold()
    if set(journal) != fields or any(not isinstance(journal[k], dict) for k in fields - {'version'}):
        _hold()
    for key, row in journal['sources'].items():
        if (not _hex(key, 64) or not isinstance(row, dict)
                or not {'consent', 'generation', 'status'} <= set(row)
                or not set(row) <= {'consent', 'generation', 'status', 'digest', 'pending', 'attempted', 'attempt', 'successor'}
                or not _hex(row['consent'], 32) or not _hex(row['generation'], 32)
                or row['status'] not in ('committed', 'editing', 'disconnected', 'refresh-claimed', 'publishing', 'authorizing')):
            _hold()
        allowed = {'consent', 'generation', 'status'}
        if row['status'] in ('editing', 'disconnected'):
            required_row, allowed_row = allowed | {'pending'}, allowed | {'pending', 'attempted'}
        elif row['status'] == 'committed':
            required_row, allowed_row = allowed | {'digest'}, allowed | {'digest', 'successor'}
        else:
            required_row = allowed | {'digest', 'attempt'}
            allowed_row = required_row | ({'successor'} if row['status'] != 'authorizing' else set())
        if not required_row <= set(row) <= allowed_row:
            _hold()
        if row['status'] in ('committed', 'refresh-claimed', 'publishing', 'authorizing') and not _hex(row.get('digest'), 64):
            _hold()
        for name in ('attempt', 'successor'):
            if name in row and (not _hex(row[name], 32) or row[name] not in journal['attempts']):
                _hold()
        if row['status'] in ('refresh-claimed', 'publishing', 'authorizing') and 'attempt' not in row:
            _hold()
        if 'attempted' in row and row['attempted'] not in _FIELDS:
            _hold()
        if 'pending' in row:
            pending = row['pending']
            if isinstance(pending, list):  # supported old intent-only shape, never verified credit
                if any(k not in _PAIR for k in pending):
                    _hold()
            elif (not isinstance(pending, dict) or not set(pending) <= _PAIR
                    or any(not _hex(v, 64) for v in pending.values())):
                _hold()
    for key, attempt in journal['attempts'].items():
        required = {'id', 'store', 'kind', 'consent', 'generation', 'digest', 'status', 'receipt'}
        if (not _hex(key, 32) or not isinstance(attempt, dict) or not required <= set(attempt)
                or not set(attempt) <= required | {'state', 'exchanged', 'setup'}
                or attempt['id'] != key or attempt['receipt'] != key
                or not _hex(attempt['store'], 64) or not _hex(attempt['consent'], 32)
                or not _hex(attempt['generation'], 32) or not _hex(attempt['digest'], 64)
                or attempt['kind'] not in ('refresh', 'authorization')
                or attempt['status'] not in ('claimed', 'committed', 'quarantined')):
            _hold()
        if attempt['store'] not in journal['sources']:
            _hold()  # writers retain the store row, including for superseded history
        if attempt['kind'] == 'authorization':
            if not _hex(attempt.get('state'), 64) or type(attempt.get('exchanged')) is not bool:
                _hold()
            # The pre-setup save is legitimate only before exchange. Every
            # receipt-producing path first persists setup and exchanged=True.
            if ((attempt['exchanged'] and 'setup' not in attempt)
                    or (attempt['status'] != 'claimed' and not attempt['exchanged'])):
                _hold()
            if 'setup' in attempt:
                setup = attempt['setup']
                if (not isinstance(setup, dict) or set(setup) != {'profile', 'epoch', 'settings', 'expires'}
                        or any(not isinstance(setup[k], str) for k in ('profile', 'epoch', 'settings'))
                        or type(setup['expires']) not in (float, int) or not math.isfinite(setup['expires'])):
                    _hold()
        elif set(attempt) != required:
            _hold()
    for key, attempt in journal['consumed'].items():
        if not _hex(key, 64) or not _hex(attempt, 32) or attempt not in journal['attempts'] or journal['attempts'][attempt]['kind'] != 'refresh':
            _hold()
    consumed_attempts = set(journal['consumed'].values())
    if any(key not in consumed_attempts for key, attempt in journal['attempts'].items()
           if attempt['kind'] == 'refresh'):
        _hold()

    for key, row in journal['sources'].items():
        if 'attempt' in row:
            attempt = journal['attempts'][row['attempt']]
            # In-flight rows still describe the exact predecessor, including
            # after a failed publication quarantines the referenced attempt.
            if (attempt['store'] != key or attempt['consent'] != row['consent']
                    or attempt['generation'] != row['generation'] or attempt['digest'] != row['digest']
                    or attempt['status'] not in ('claimed', 'quarantined')
                    or (row['status'] == 'authorizing' and attempt['kind'] != 'authorization')
                    or (row['status'] == 'refresh-claimed' and attempt['kind'] != 'refresh')
                    or (row['status'] == 'publishing' and attempt['kind'] == 'authorization'
                        and (not attempt['exchanged'] or 'successor' in row))):
                _hold()
        if 'successor' in row:
            attempt = journal['attempts'][row['successor']]
            # Publication changes generation but retains store/consent. The
            # result digest is in the secret receipt, not the attempt's old
            # digest. A later failed re-publication can quarantine this same
            # successor; historical attempts need no current-row reference.
            if (attempt['store'] != key or attempt['consent'] != row['consent']
                    or attempt['generation'] == row['generation']
                    or attempt['status'] not in ('committed', 'quarantined')):
                _hold()

    for key, pin in journal.get('identities', {}).items():
        if (not _hex(key, 64) or not isinstance(pin, dict) or type(pin.get('version')) is not int
                or pin['version'] != 1 or pin.get('state') not in ('active', 'held')
                or set(pin) != {'version', 'first', 'revision', 'state'} | ({'conflict'} if pin.get('state') == 'held' else set())
                or not _hex(pin['revision'], 32)):
            _hold()
        _validate_identity_observation(pin['first'], key)
        if pin['state'] == 'held':
            _validate_identity_observation(pin['conflict'], key)
            if pin['conflict']['identifier'] == pin['first']['identifier']:
                _hold()


def _validate_identity_observation(value, selection):
    if (not isinstance(value, dict) or set(value) != {'subject', 'identifier', 'endpoint', 'observed_at'}
            or value['endpoint'] != _IDENTITY_ENDPOINT or type(value['observed_at']) not in (float, int)
            or not math.isfinite(value['observed_at'])):
        _hold()
    _provider_identifier(value['identifier'])
    subject = value['subject']
    if (not isinstance(subject, dict) or set(subject) != {'selection', 'consent', 'generation', 'digest', 'successor'}
            or subject['selection'] != selection or not _hex(subject['consent'], 32)
            or not _hex(subject['generation'], 32) or not _hex(subject['digest'], 64)
            or (subject['successor'] is not None and not _hex(subject['successor'], 32))):
        _hold()

class KeychainStore:
    key = 'keychain:fulcra-collect:trakt'
    file = False

    def __init__(self, *, _quiet_write=None):
        self._quiet_write = _quiet_write

    def _background(self, key, value):
        from .credentials import _mutate_trakt_secret_quiet
        (self._quiet_write or _mutate_trakt_secret_quiet)(key, value)

    def snapshot(self):
        from . import credentials
        # Every consumed optional field is a strict quiet read. UNKNOWN propagates.
        return _values({key: credentials.get_secret('trakt', key) for key in _FIELDS}, complete=False)

    def authorize(self, key):
        from . import credentials
        # Explicit UI authorization only; the owner calls this outside locks.
        # False is confirmed absence, not denied/UNKNOWN (which must raise).
        return credentials.authorize_secret('trakt', key)

    def set(self, key, value):
        self._background(key, str(value) if value is not None else None)

    def publish(self, values):
        current = self.snapshot()
        for key in _FIELDS:
            if current.get(key) != values.get(key):
                self._background(key, values.get(key))

    def save_receipt(self, attempt, value):
        raw = _encoded(value).decode()
        if len(raw.encode()) > 65536:
            _hold()
        self._background('source-receipt-' + attempt, raw)
        if self.receipt(attempt) != value:
            _hold()

    def receipt(self, attempt):
        from . import credentials
        raw = credentials.get_secret('trakt', 'source-receipt-' + attempt)
        if not isinstance(raw, str) or len(raw.encode()) > _LIMIT:
            _hold()
        return _decode(raw)


class FileStore:
    file = True

    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        self.key = 'file:' + str(self.path)

    def snapshot(self):
        return _values(_read_json(self.path), complete=False)

    def publish(self, values):
        _atomic_json(self.path, values)

    def authorize(self, key):
        return True  # explicit file mode has no OS authorization UI

    def set(self, key, value):
        values = self.snapshot()
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
        self.publish(values)

    def _receipt_path(self, attempt):
        return self.path.with_name('.' + self.path.name + '.receipt-' + attempt)

    def save_receipt(self, attempt, value):
        _atomic_json(self._receipt_path(attempt), value)
        if self.receipt(attempt) != value:
            _hold()

    def receipt(self, attempt):
        return _read_json(self._receipt_path(attempt), private=True)


class Owner:
    def __init__(self, store, *, _root=None):
        self.store = store
        self.root = Path(_root) if _root is not None else _os_user_root()
        self.key = hashlib.sha256(store.key.encode()).hexdigest()

    @contextmanager
    def _lease(self, name):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077):
            _hold()
        fd = os.open(self.root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                _hold()
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        _hold()
                    time.sleep(.02)
            yield
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self):
        with self._lease('source.lock'):
            try:
                journal = _read_json(self.root / 'journal.json', private=True)
            except FileNotFoundError:
                journal = {'version': 2, 'sources': {}, 'attempts': {}, 'consumed': {}, 'identities': {}}
            _validate_journal(journal)
            if journal['version'] == 1:
                # Validate the complete predecessor before publishing any upgrade.
                journal = {**journal, 'version': 2, 'identities': {}}
                self._save(journal)
            yield journal

    def _save(self, journal):
        _atomic_json(self.root / 'journal.json', journal)

    def _snapshot(self, *, complete=True):
        return _values(self.store.snapshot(), complete=complete, file=self.store.file)

    def capture(self, authority, local):
        authority()
        with task_mutation_scope(), self._locked() as journal:
            row = journal['sources'].get(self.key)
            if row is not None and not isinstance(row, dict):
                _hold()
            recovery = row and row.get('status') in ('refresh-claimed', 'publishing', 'authorizing')
            if not recovery:
                values = self._snapshot()
                local()
                if row is None:
                    row = {'consent': uuid4().hex, 'generation': uuid4().hex,
                           'status': 'committed', 'digest': _hash(values)}
                    journal['sources'][self.key] = row
                    self._save(journal)
                if row.get('status') != 'committed' or row.get('digest') != _hash(values):
                    _hold()
                binding = Binding(self, authority, local, deepcopy(row), values)
        if recovery:
            # Deterministic locator works even if receipt-write acknowledgement was lost.
            attempt = row.get('attempt')
            successor = self._publish(attempt, authority, local)
            binding = self._bind_successor(successor, authority, local,
                                           consent=row['consent'], attempt=attempt)
        local()
        return binding

    def _bind_successor(self, successor, authority, local, *, consent, attempt):
        # Publication evidence is an exact baseline, never permission to capture
        # whichever consent happens to be current after the publication lock.
        if (not isinstance(successor, _CommittedSuccessor)
                or successor.consent != consent or successor.attempt != attempt):
            _hold()
        authority()
        with task_mutation_scope(), self._locked() as journal:
            row = journal['sources'].get(self.key)
            if (row != successor.row or row.get('status') != 'committed'
                    or row.get('consent') != consent or row.get('successor') != attempt
                    or row.get('digest') != _hash(successor.values)
                    or self._snapshot() != successor.values):
                _hold()
            local()
            binding = Binding(self, authority, local, deepcopy(row), deepcopy(successor.values))
        local()
        return binding

    def _check(self, binding, journal):
        row = journal['sources'].get(self.key)
        if row != binding.row or row.get('status') != 'committed':
            _hold()
        if self._snapshot() != binding.values:
            _hold()

    def edit(self, key, value):
        if key not in _FIELDS:
            _hold()
        # Control edits never wait for the long grant lease.
        profile = str(config.profile_path())
        with settings_update_scope(), self._locked() as journal:
            cfg = config.load()
            if cfg.account_transition:
                _hold()
            old = journal['sources'].get(self.key, {})
            pending = deepcopy(old.get('pending', {})) if old.get('status') == 'editing' else {}
            # Older field-name lists record intent only, never verified credit.
            if isinstance(pending, list):
                pending = {}
            if (not isinstance(pending, dict) or not set(pending) <= _PAIR
                    or any(not isinstance(v, str) or len(v) != 64 for v in pending.values())):
                _hold()
            pending.pop(key, None)  # an attempted rewrite revokes its old credit
            if key == 'client_id':
                pending.clear()
            preserved = old.get('digest') if old.get('status') == 'committed' and key not in _REQUIRED else None
            row = {'consent': uuid4().hex, 'generation': uuid4().hex,
                   'status': 'disconnected' if value is None and key in _REQUIRED else 'editing',
                   'pending': pending, 'attempted': key}
            journal['sources'][self.key] = row
            self._save(journal)  # revoke before the first secret mutation
            cfg.rotate_plugin_epoch('trakt')
            config._save_locked(cfg, config._config_path())
            before = self._snapshot(complete=False)
            setup = {'profile': profile, 'epoch': cfg.plugin_epochs.get('trakt', ''),
                     'settings': config.settings_fingerprint(cfg.plugin_settings.get('trakt', {}))}
            _manual_local(setup)
        # UI may wait indefinitely, but owns no account/config/source lock and
        # performs no active-field write. A newer action can supersede it.
        if type(self.store.authorize(key)) is not bool:
            _hold()
        with settings_update_scope(), self._locked() as journal:
            if journal['sources'].get(self.key) != row:
                _hold()
            row = journal['sources'][self.key]
            _manual_local(setup)
            if self._snapshot(complete=False) != before:
                _hold()
            _manual_local(setup)
            self.store.set(key, value)  # bounded quiet mutation; no UI fallback
            after = self._snapshot(complete=False)
            _manual_local(setup)
            expected = deepcopy(before)
            if value is None and self.store.file:
                expected.pop(key, None)
            else:
                expected[key] = value
            if after != expected:
                _hold()
            # Positive credit is published only after the exact supplied delta
            # read back. A predecessor that drifted loses its credit too.
            pending = {field: digest for field, digest in pending.items()
                       if digest == _hash(after.get(field))}
            if key in _PAIR and value is not None:
                pending[key] = _hash(value)
            row['pending'] = pending
            row.pop('attempted', None)
            ready = False
            if row['status'] != 'disconnected' and (set(pending) == _PAIR or preserved == _hash(before)):
                _values(after, file=self.store.file)
                row.update(status='committed', digest=_hash(after))
                row.pop('pending', None)
                ready = True
            self._save(journal)
            return {'ok': True, 'ready': ready, 'status': row['status']}

    def begin_authorization(self, state, verifier, redirect):
        if not all(isinstance(v, str) and v for v in (state, verifier, redirect)):
            _hold()
        profile = str(config.profile_path())
        with settings_update_scope(), self._locked() as journal:
            cfg = config.load()
            if cfg.account_transition:
                _hold()
            values = self._snapshot(complete=False)
            if not isinstance(values.get('client_id'), str) or not values['client_id']:
                _hold()
            attempt = uuid4().hex
            row = {'consent': uuid4().hex, 'generation': uuid4().hex,
                   'status': 'authorizing', 'attempt': attempt, 'digest': _hash(values)}
            journal['sources'][self.key] = row
            record = {'id': attempt, 'store': self.key, 'kind': 'authorization',
                      'consent': row['consent'], 'generation': row['generation'],
                      'digest': row['digest'], 'status': 'claimed', 'receipt': attempt,
                      'state': hashlib.sha256(state.encode()).hexdigest(), 'exchanged': False}
            journal['attempts'][attempt] = record
            self._save(journal)
            cfg.rotate_plugin_epoch('trakt')
            config._save_locked(cfg, config._config_path())
            setup = {'profile': profile, 'epoch': cfg.plugin_epochs.get('trakt', ''),
                     'settings': config.settings_fingerprint(cfg.plugin_settings.get('trakt', {})),
                     'expires': time.time() + 600}
            record['setup'] = setup
            self._save(journal)
        return Authorization(self, deepcopy(record), values, verifier, redirect, setup, time.monotonic())

    def _claim(self, binding, before_claim):
        with task_mutation_scope(), self._locked() as journal:
            self._check(binding, journal)
            binding.local()
            if before_claim is not None:
                before_claim()  # pure cooperative deadline, no auth or I/O
            fingerprint = hashlib.sha256(binding.values['refresh_token'].encode()).hexdigest()
            if fingerprint in journal['consumed']:
                _hold()
            attempt = uuid4().hex
            row = journal['sources'][self.key]
            record = {'id': attempt, 'store': self.key, 'kind': 'refresh',
                      'consent': row['consent'], 'generation': row['generation'],
                      'digest': row['digest'], 'status': 'claimed', 'receipt': attempt}
            journal['attempts'][attempt] = record
            journal['consumed'][fingerprint] = attempt
            row.update(status='refresh-claimed', attempt=attempt)
            self._save(journal)
            return deepcopy(record)

    def _receipt(self, attempt, values, original):
        envelope = {'attempt': attempt, 'values': values, 'digest': _hash(values),
                    'original': deepcopy(original)}
        self.store.save_receipt(attempt['id'], envelope)

    def _publish(self, attempt_id, authority, local):
        envelope = self.store.receipt(attempt_id)
        if not isinstance(envelope, dict) or set(envelope) != {'attempt', 'values', 'digest', 'original'}:
            _hold()
        values = _values(envelope['values'], file=self.store.file)
        original = _values(envelope['original'], complete=False)
        if envelope['digest'] != _hash(values):
            _hold()
        # Quarantine superseded responses even when the old full guard already
        # fails. This short lineage check never calls that guard or a provider.
        with self._locked() as journal:
            attempt = journal['attempts'].get(attempt_id)
            row = journal['sources'].get(self.key)
            if (not attempt or attempt.get('store') != self.key
                    or attempt.get('status') not in ('claimed', 'committed')
                    or attempt.get('digest') != _hash(original)
                    or envelope['attempt'] != {**attempt, 'status': 'claimed'}):
                _hold()
            successor = (row and row.get('status') == 'committed' and row.get('successor') == attempt_id)
            if not successor and (not row or row.get('consent') != attempt['consent']
                    or row.get('generation') != attempt['generation'] or row.get('attempt') != attempt_id):
                attempt['status'] = 'quarantined'
                self._save(journal)
                _hold()
        authority()  # receipt is durable BEFORE any slow proof or deadline hold
        with task_mutation_scope(), self._locked() as journal:
            attempt = journal['attempts'].get(attempt_id)
            row = journal['sources'].get(self.key)
            if (not attempt or attempt.get('store') != self.key
                    or attempt.get('status') not in ('claimed', 'committed')
                    or attempt.get('digest') != _hash(original)
                    or envelope['attempt'] != {**attempt, 'status': 'claimed'}):
                _hold()
            if (row and row.get('status') == 'committed' and row.get('successor') == attempt_id
                    and row.get('consent') == attempt['consent']
                    and row.get('digest') == _hash(values) and self._snapshot() == values):
                local()
                return _CommittedSuccessor(attempt_id, attempt['consent'], deepcopy(row), deepcopy(values))
            if (not row or row.get('consent') != attempt['consent']
                    or row.get('generation') != attempt['generation'] or row.get('attempt') != attempt_id
                    or row.get('status') not in ('refresh-claimed', 'authorizing', 'publishing')):
                attempt['status'] = 'quarantined'
                self._save(journal)
                _hold()
            current = self._snapshot(complete=False)
            if row['status'] == 'publishing':
                # A resumed Keychain mirror may contain only the exact old/new
                # field values from this receipt; a file publication is whole.
                coherent = (current in (original, values) if self.store.file else
                    set(current) == set(values) == set(original) and
                    all(current[k] in (original[k], values[k]) for k in current))
            else:
                coherent = current == original
            if not coherent:
                attempt['status'] = 'quarantined'
                self._save(journal)
                _hold()
            if attempt['kind'] == 'authorization':
                _setup_local(attempt.get('setup'))
            local()
            row['status'] = 'publishing'
            self._save(journal)  # all readers refuse partial mirrors
            self.store.publish(values)
            if self._snapshot() != values:
                _hold()
            local()
            row.update(status='committed', generation=uuid4().hex, digest=_hash(values), successor=attempt_id)
            row.pop('attempt', None)
            attempt['status'] = 'committed'
            self._save(journal)
            return _CommittedSuccessor(attempt_id, attempt['consent'], deepcopy(row), deepcopy(values))


@dataclass(frozen=True, repr=False)
class _CommittedSuccessor:
    attempt: str
    consent: str
    row: dict
    values: dict


@dataclass
class Binding:
    owner: Owner
    authority: object
    local: object
    row: dict
    values: dict

    def check_local_after_authority(self):
        with self.owner._locked() as journal:
            self.owner._check(self, journal)
        self.local()

    def check_before_source(self):
        self.authority()
        self.check_local_after_authority()

    def refresh(self, grant, *, _before_claim=None):
        self.check_before_source()
        with self.owner._lease('grant.lock'):
            self.check_before_source()
            attempt = self.owner._claim(self, _before_claim)
            returned = grant(deepcopy(self.values))  # exactly one call, outside short locks
            if not isinstance(returned, dict) or any(not isinstance(returned.get(k), str)
                    or not returned[k] for k in _PAIR):
                _hold()
            values = deepcopy(self.values)
            if self.owner.store.file and any(k not in returned for k in ('created_at', 'expires_in')):
                _hold()
            values.update({k: returned.get(k) for k in
                           ('access_token', 'refresh_token', 'created_at', 'expires_in')})
            if not self.owner.store.file:
                values = {k: str(v) if v is not None else None for k, v in values.items()}
            _values(values, file=self.owner.store.file)
            self.owner._receipt(attempt, values, self.values)
            successor = self.owner._publish(attempt['id'], self.authority, self.local)
            replacement = self.owner._bind_successor(successor, self.authority, self.local,
                                                       consent=self.row['consent'], attempt=attempt['id'])
            self.row, self.values = replacement.row, replacement.values
        self.check_before_source()
        return self.values


def _manual_local(setup):
    cfg = config.load()
    if (cfg.account_transition or str(config.profile_path()) != setup['profile']
            or cfg.plugin_epochs.get('trakt', '') != setup['epoch']
            or config.settings_fingerprint(cfg.plugin_settings.get('trakt', {})) != setup['settings']):
        _hold()


def _setup_local(setup):
    cfg = config.load()
    if (not isinstance(setup, dict) or cfg.account_transition
            or time.time() > setup['expires']
            or str(config.profile_path()) != setup['profile']
            or cfg.plugin_epochs.get('trakt', '') != setup['epoch']
            or config.settings_fingerprint(cfg.plugin_settings.get('trakt', {})) != setup['settings']):
        _hold()


@dataclass
class Authorization:
    owner: Owner
    attempt: dict
    values: dict
    verifier: str
    redirect: str
    setup: dict
    started: float

    def local(self):
        if time.monotonic() - self.started > 600:
            _hold()
        _setup_local(self.setup)

    def exchange(self, code, grant):
        self.local()
        with task_mutation_scope(), self.owner._locked() as journal:
            row = journal['sources'].get(self.owner.key, {})
            attempt = journal['attempts'].get(self.attempt['id'])
            if (row.get('status') != 'authorizing' or row.get('attempt') != self.attempt['id']
                    or attempt != self.attempt or attempt.get('exchanged')
                    or self.owner._snapshot(complete=False) != self.values):
                _hold()
            self.local()
            attempt['exchanged'] = True
            self.attempt = deepcopy(attempt)
            self.owner._save(journal)
        returned = grant({'code': code, 'client_id': self.values['client_id'],
                          'redirect_uri': self.redirect, 'grant_type': 'authorization_code',
                          'code_verifier': self.verifier})
        if (not isinstance(returned, dict) or set(returned) !=
                {'access_token', 'refresh_token', 'created_at', 'expires_in', 'redirect_uri'}
                or returned['redirect_uri'] != self.redirect
                or any(not returned.get(k) for k in ('created_at', 'expires_in'))):
            _hold()
        values = deepcopy(self.values)
        values.update(returned)
        values = {k: str(v) if v is not None else None for k, v in values.items()}
        _values(values)
        self.owner._receipt(self.attempt, values, self.values)
        # Publication verifies lineage before checking captured setup authority,
        # ensuring late responses can be quarantined without overwriting anything.
        self.owner._publish(self.attempt['id'], lambda: None, self.local)
        self.local()
        return values


@contextmanager
def current_work_scope(ctx, session, local):
    """Owned by the recovered run; includes final cursor and restores on exit."""
    previous_guard = session.guard
    names = ('_register_current_work_guard', '_trakt_authority', '_trakt_local')
    absent = object()
    previous = {name: getattr(ctx, name, absent) for name in names}
    callback = None
    def register(check):
        nonlocal callback
        if callback is not None or not callable(check):
            _hold()
        callback = check
    def guarded(**kwargs):
        previous_guard(**kwargs)
        if callback is not None:
            callback()
    ctx._register_current_work_guard = register
    ctx._trakt_authority = previous_guard
    ctx._trakt_local = local
    session.guard = guarded
    try:
        yield
    finally:
        session.guard = previous_guard
        for name, value in previous.items():
            if value is absent:
                delattr(ctx, name)
            else:
                setattr(ctx, name, value)
