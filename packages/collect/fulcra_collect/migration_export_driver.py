"""Bounded paired legacy exports; provider completeness is an explicit input.

This orchestrator does no writes to Fulcra. An accepted provider contract is a
caller assertion, not inferred from HTTP 200 or matching ascending/descending
reads. Cutoff, queues, late deltas and historical application remain separate.
"""
from contextlib import contextmanager
from datetime import timedelta
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from uuid import UUID, uuid5

from fulcra_common.v1 import _canonical_uuid
from .migration_export import _private, _EXPORT_FORMAT_VERSION
from .migration_ledger import _MAX_WINDOW_JSON
from .migration_plan import _time
from .record_actions import _json, _hash

_TABLES = {'migration_export_pass', 'migration_window_reads'}


def _contract(value):
    try:
        value = json.loads(_json(value))
        if (set(value) != {'version', 'semantics', 'row_cap', 'basis'}
                or type(value['version']) is not int or value['version'] != 1
                or value['semantics'] != 'complete-below-row-cap'
                or type(value['row_cap']) is not int or not 1 <= value['row_cap'] <= 4000
                or not isinstance(value['basis'], str) or not value['basis'].strip()
                or len(value['basis'].encode()) > 4096):
            raise ValueError
        return value
    except (ValueError, TypeError, KeyError):
        raise ValueError('historical export requires an explicit provider completeness contract') from None


def _iso(value):
    return value.isoformat().replace('+00:00', 'Z')


class ExportDriver:
    """One immutable pass per fresh ledger; bounded indexed resume, no uploads.

    The supplied contract asserts that this provider returns every record when
    the response is below its row cap, in either direction. It must have a real
    external basis. This class cannot establish that claim. Matched reads detect
    many truncation/change cases but cannot prove an undocumented provider cap.
    ``run`` reports queue progress only; it never certifies historical coverage.
    """
    def __init__(self, ledger, reader, *, provider_contract, clock=time.monotonic):
        contract = _contract(provider_contract)
        if ledger.plan.fingerprint != reader.plan.fingerprint or not callable(clock):
            raise ValueError('historical exporter reader/ledger plan scope differs')
        self.ledger, self.reader, self.clock = ledger, reader, clock
        self.pass_id = _canonical_uuid(reader.pass_id)
        self._contract_json = _json(contract)
        self.cap = contract['row_cap']
        self._scope = _json({'export_protocol_version': _EXPORT_FORMAT_VERSION, 'pass_id': self.pass_id, 'plan_fingerprint': ledger.plan.fingerprint,
                            'provider_contract': contract})
        document = ledger.plan.document
        self.root_window_id = self._window_id(document['start_time'], document['end_time'])
        with ledger.storage._db(write=True) as db:
            ledger._guard(db)
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables & _TABLES:
                if not _TABLES <= tables:
                    raise ValueError('historical export pass schema is incomplete')
                self._guard(db)
            else:
                if db.execute('SELECT 1 FROM migration_windows LIMIT 1').fetchone():
                    raise ValueError('historical export cannot adopt existing unbound windows')
                db.execute('CREATE TABLE migration_export_pass(version INTEGER NOT NULL,document TEXT NOT NULL)')
                db.execute('INSERT INTO migration_export_pass VALUES (?,?)', (_EXPORT_FORMAT_VERSION, self._scope))
                db.execute('CREATE TABLE migration_window_reads(window_id TEXT PRIMARY KEY '
                    'REFERENCES migration_windows(window_id),document TEXT NOT NULL,fingerprint TEXT NOT NULL)')
                ledger._open_window(db, self.root_window_id, document['start_time'], document['end_time'])
            db.execute('CREATE INDEX IF NOT EXISTS migration_pending_windows '
                       'ON migration_windows(status,start_time,window_id)')

    def _guard(self, db):
        self.ledger._guard(db)
        try:
            rows = db.execute('SELECT version,document FROM migration_export_pass').fetchall()
            if (len(rows) != 1 or type(rows[0]['version']) is not int or rows[0]['version'] != _EXPORT_FORMAT_VERSION
                    or rows[0]['document'] != self._scope
                    or db.execute('SELECT 1 FROM migration_windows WHERE window_id=?',
                                  (self.root_window_id,)).fetchone() is None):
                raise ValueError
        except (ValueError, sqlite3.DatabaseError):
            raise ValueError('historical export pass version/scope is changed or unreadable') from None

    def _window_id(self, start, end):
        canonical = _json({'plan': self.ledger.plan.fingerprint, 'start': _iso(_time(start)), 'end': _iso(_time(end))})
        return str(uuid5(UUID(self.pass_id), canonical))

    def _next(self):
        with self.ledger.storage._db() as db:
            self._guard(db)
            states = {row[0] for row in db.execute('SELECT DISTINCT status FROM migration_windows LIMIT 4')}
            if states - {'open', 'complete', 'split'}:
                raise ValueError('historical export window status is unknown')
            row = db.execute("SELECT window_id FROM migration_windows WHERE status='open' "
                             'ORDER BY start_time,window_id LIMIT 1').fetchone()
            if row is None:
                return None
            window = self.ledger._window(db, row['window_id'])
            if row['window_id'] != self._window_id(_iso(window['start']), _iso(window['end'])):
                raise ValueError('historical export window identity does not match pass scope')
            return {'window_id': row['window_id'], 'start_time': _iso(window['start']), 'end_time': _iso(window['end'])}

    @contextmanager
    def _lease(self):
        path = self.ledger.path.with_name(self.ledger.path.name + '.export-lock')
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            _private(os.fstat(fd))
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('historical export pass is already running') from None
            yield
        finally:
            os.close(fd)

    def _children(self, window):
        start, end = _time(window['start_time']), _time(window['end_time'])
        if end - start < timedelta(microseconds=4):
            raise RuntimeError('historical export minimum window cannot narrow; coverage is unknown')
        middle, overlap = start + (end - start) // 2, timedelta(microseconds=1)
        return [{'window_id': self._window_id(_iso(left), _iso(right)),
                 'start_time': _iso(left), 'end_time': _iso(right)}
                for left, right in ((start, middle + overlap), (middle - overlap, end))]

    def window_proof(self, window_id):
        """Hash-check persisted pair metadata, without a fresh artifact audit."""
        _canonical_uuid(window_id)
        with self.ledger.storage._db() as db:
            self._guard(db)
            row = db.execute('SELECT * FROM migration_window_reads WHERE window_id=?', (window_id,)).fetchone()
            if row is None:
                return None
            try:
                value = json.loads(row['document'])
                if (_json(value) != row['document'] or _hash(row['document']) != row['fingerprint']
                        or value['pass_scope_sha256'] != _hash(self._scope) or value['window_id'] != window_id
                        or _json(value['provider_contract']) != self._contract_json):
                    raise ValueError
                self._validate_proof(db, value)
                return value
            except (ValueError, TypeError, KeyError):
                raise ValueError('historical export pair proof is corrupt or unreadable') from None

    def _validate_proof(self, db, proof):
        fields = {'version', 'window_id', 'pass_scope_sha256', 'provider_contract', 'outcome',
                  'provider_count', 'asc', 'desc', 'multiset_sha256', 'entry_ids'}
        if set(proof) != fields or type(proof['version']) is not int or proof['version'] != _EXPORT_FORMAT_VERSION:
            raise ValueError
        def digest(value):
            return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value)
        counts = []
        for key in ('asc', 'desc'):
            read = proof[key]
            if (not isinstance(read, dict) or set(read) != {'attempt_id', 'request_sha256', 'body_sha256', 'receipt_sha256', 'snapshot_sha256', 'count'}
                    or type(read['count']) is not int or not 0 <= read['count'] <= 4000
                    or any(not digest(read[field]) for field in ('request_sha256', 'body_sha256', 'receipt_sha256', 'snapshot_sha256'))):
                raise ValueError
            _canonical_uuid(read['attempt_id'])
            counts.append(read['count'])
        if proof['asc']['attempt_id'] == proof['desc']['attempt_id']:
            raise ValueError
        entries = proof['entry_ids']
        if (not isinstance(entries, list) or len(entries) > sum(counts) or len(entries) != len(set(entries))
                or any(not digest(identity) for identity in entries)):
            raise ValueError
        for identity in entries:
            if db.execute('SELECT 1 FROM migration_entries WHERE entry_id=?', (identity,)).fetchone() is None:
                raise ValueError
        matched = digest(proof['multiset_sha256'])
        if matched:
            if type(proof['provider_count']) is not int or counts[0] != counts[1] or proof['provider_count'] != counts[0]:
                raise ValueError
        elif proof['multiset_sha256'] is not None or proof['provider_count'] is not None:
            raise ValueError
        window = self.ledger._window(db, proof['window_id'])
        if window is None:
            raise ValueError
        outcome = proof['outcome']
        if outcome == 'sealed-matched-below-cap':
            if (not matched or max(counts) >= self.cap or window['status'] != 'complete'
                    or window['evidence']['provider_count'] != counts[0] or window['evidence']['cap'] != self.cap):
                raise ValueError
            observed = db.execute('SELECT observations.entry_id,entries.raw_hash FROM migration_observations observations '
                'JOIN migration_entries entries ON entries.entry_id=observations.entry_id '
                'WHERE observations.window_id=? ORDER BY observations.ordinal LIMIT 4001',
                (proof['window_id'],)).fetchall()
            expected_ids = sorted({row['entry_id'] for row in observed})
            expected_multiset = _hash(_json({'rows': sorted(row['raw_hash'] for row in observed)}))
            if (len(observed) != counts[0] or expected_ids != entries
                    or any(not digest(row['raw_hash']) for row in observed)
                    or expected_multiset != proof['multiset_sha256']):
                raise ValueError
        elif outcome in {'split-saturated', 'split-inconsistent'}:
            if window['status'] != 'split' or (outcome == 'split-saturated') != (max(counts) >= self.cap):
                raise ValueError
            if outcome == 'split-inconsistent' and matched:
                raise ValueError
            children = window['evidence']['children']
            self.ledger._split_children(children)
            windows = {proof['window_id']: window}
            for child in children:
                value = self.ledger._window(db, child)
                if value is not None:
                    windows[child] = value
            self.ledger._split_proof(proof['window_id'], children, windows)
        else:
            raise ValueError

    def _finish(self, window, records, proof, children, retained, deadline):
        prepared = [self.ledger._prepare_export(row) for row in records] if children is None else []
        if (sum(len(row[2].encode()) for row in prepared) > _MAX_WINDOW_JSON
                or sum(len(row[2].encode()) for row in retained) > 2 * _MAX_WINDOW_JSON):
            raise ValueError('historical export snapshot exceeds its JSON bound')
        document = _json(proof)
        self.reader._guard()
        with self.ledger.storage._db(write=True) as db:
            self._guard(db)
            current = self.ledger._window(db, window['window_id'])
            if current is None or current['status'] != 'open':
                raise ValueError('historical export window changed during paired read')
            self.reader._guard()
            db.execute('INSERT INTO migration_window_reads VALUES (?,?,?)',
                       (window['window_id'], document, _hash(document)))
            # Every successfully observed version remains conversion-visible,
            # including saturated/inconsistent parents. Later empty child reads
            # cannot erase it. The pair proof binds these content-addressed IDs;
            # only complete leaves carry ordered coverage observations.
            for row in retained:
                self.ledger._retain_export(db, row, verify_target=False)
            if children is not None:
                for child in children:
                    self.ledger._open_window(db, child['window_id'], child['start_time'], child['end_time'])
                self.ledger._mark_split(db, window['window_id'], [child['window_id'] for child in children])
            else:
                self.ledger._complete_read(True, len(records), self.cap)
                for ordinal, row in enumerate(prepared):
                    self.ledger._capture_export(db, window['window_id'], ordinal, row, verify_target=False)
                self.ledger._seal_window(db, window['window_id'], len(records), self.cap)
            self._validate_proof(db, proof)
            self.reader._guard()
            if self.clock() >= deadline:
                raise RuntimeError('historical export commit deadline exceeded; transaction rolled back')

    def run(self, *, max_windows=16, budget_seconds=120):
        """Process at most N windows, reserving two reader timeouts per pair.

        Raw attempts survive every failure. Pair proof + observations/seal or
        child creation/split commit in one transaction. No fresh all-history
        audit is performed by this bounded work-queue operation.
        """
        if (type(max_windows) is not int or not 1 <= max_windows <= 64
                or isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float))
                or not math.isfinite(budget_seconds) or not 2 * self.reader.timeout + 1 <= budget_seconds <= 300):
            raise ValueError('historical export work/budget bounds are invalid')
        deadline = self.clock() + budget_seconds
        counts = {'processed': 0, 'sealed': 0, 'split': 0}
        reason = 'window-limit'
        with self._lease():
            while counts['processed'] < max_windows:
                window = self._next()
                if window is None:
                    reason = 'queue-drained'
                    break
                if deadline - self.clock() < 2 * self.reader.timeout + 1:
                    reason = 'time-budget'
                    break
                options = {key: window[key] for key in ('start_time', 'end_time')}
                asc = self.reader.read(window['window_id'], **options, order='asc')
                if deadline - self.clock() < self.reader.timeout + 1:
                    raise RuntimeError('historical export work deadline leaves no second-read budget; window open')
                desc = self.reader.read(window['window_id'], **options, order='desc')
                asc_proof = self.reader.verify_result(asc, window['window_id'], **options, order='asc')
                desc_proof = self.reader.verify_result(desc, window['window_id'], **options, order='desc')
                if self.clock() >= deadline:
                    raise RuntimeError('historical export work deadline exceeded; paired artifacts retained, window open')
                left, right = asc.records, desc.records
                retained = {}
                for record in left + right:
                    prepared = self.ledger._prepare_export(record)
                    retained[prepared[0]] = prepared
                fingerprints = [sorted(_hash(_json(row)) for row in rows) for rows in (left, right)]
                saturated = len(left) >= self.cap or len(right) >= self.cap
                matched = fingerprints[0] == fingerprints[1]
                children = self._children(window) if saturated or not matched else None
                outcome = ('split-saturated' if saturated else 'split-inconsistent') if children else 'sealed-matched-below-cap'
                proof = {'version': _EXPORT_FORMAT_VERSION, 'window_id': window['window_id'], 'pass_scope_sha256': _hash(self._scope),
                    'provider_contract': json.loads(self._contract_json), 'outcome': outcome,
                    'provider_count': len(left) if matched else None, 'asc': asc_proof, 'desc': desc_proof,
                    'entry_ids': sorted(retained),
                    'multiset_sha256': hashlib.sha256(_json({'rows': fingerprints[0]}).encode()).hexdigest() if matched else None}
                self._finish(window, left, proof, children, list(retained.values()), deadline)
                counts['processed'] += 1
                counts['split' if children else 'sealed'] += 1
            empty = self._next() is None
        return {**counts, 'window_queue_empty': empty, 'reason': 'queue-drained' if empty else reason,
                'coverage_complete': False, 'historical_complete': False, 'raw_integrity': 'not-scanned'}
