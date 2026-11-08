"""Private raw historical exports and conservative window-coverage evidence.

This offline ledger does not query/upload records, authorize a destination
or finalize history. Raw variants and overlapping observations
survive restart; unknown/capped reads never become clean empty coverage.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from fulcra_common.v1 import _canonical_uuid
from .migration_plan import MigrationPlan, _time
from .record_actions import ActionJournal, _json, _hash

_STATES = frozenset({'exported', 'converted', 'quarantined'})
_TABLES = frozenset({'migration_meta', 'migration_plan', 'migration_windows',
                     'migration_entries', 'migration_observations'})
_MAX_WINDOWS = 100000
_MAX_WINDOW_JSON = 16 * 1024 * 1024


def _instant(value):
    return _time(value).isoformat().replace('+00:00', 'Z')


def _covers(intervals, start, end):
    current = start
    for left, right in sorted(intervals):
        if left > current:
            return False
        if right > current:
            current = right
    return current >= end


@dataclass(frozen=True)
class ExportedEntry:
    entry_id: str
    legacy_id: str | None
    raw: dict
    status: str
    reason: str | None
    source_id: str | None = None
    target_record_id: str | None = None
    _payload_json: str | None = None

    @property
    def payload(self):
        return None if self._payload_json is None else json.loads(self._payload_json)


class MigrationLedger:
    def __init__(self, path: Path, plan: MigrationPlan):
        # Revalidate rather than trusting a caller-supplied dataclass instance.
        self.plan = MigrationPlan.from_document(plan.document)
        self._plan_json = _json(self.plan.document)
        self._start = _time(self.plan.document['start_time'])
        self._end = _time(self.plan.document['end_time'])
        self.path = Path(path)
        self._converter = None
        self.storage = ActionJournal(self.path)
        with self.storage._db(write=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables & _TABLES:
                if not _TABLES <= tables:
                    raise ValueError('migration ledger schema is incomplete')
                versions = db.execute('SELECT version FROM migration_meta').fetchall()
                if len(versions) != 1 or versions[0][0] not in {1, 2}:
                    raise ValueError('migration ledger schema version is unsupported')
                self._guard(db, allow_export_only=True)
                if versions[0][0] == 1:
                    unexpected = db.execute("SELECT 1 FROM migration_entries WHERE status NOT IN ('exported','quarantined') "
                        'OR source_id IS NOT NULL OR target_record_id IS NOT NULL OR payload_json IS NOT NULL '
                        'OR payload_hash IS NOT NULL LIMIT 1').fetchone()
                    if unexpected:
                        raise ValueError('export-only migration schema contains unsupported conversion state')
                    db.execute('UPDATE migration_meta SET version=2')
            else:
                db.execute('CREATE TABLE migration_meta(version INTEGER NOT NULL)')
                db.execute('INSERT INTO migration_meta VALUES (2)')
                db.execute('CREATE TABLE migration_plan(document TEXT NOT NULL,fingerprint TEXT NOT NULL)')
                db.execute('INSERT INTO migration_plan VALUES (?,?)',
                           (self._plan_json, self.plan.fingerprint))
                db.execute('CREATE TABLE migration_windows(window_id TEXT PRIMARY KEY,start_time TEXT NOT NULL,'
                           'end_time TEXT NOT NULL,status TEXT NOT NULL,evidence TEXT NOT NULL,evidence_hash TEXT NOT NULL)')
                db.execute('CREATE TABLE migration_entries(entry_id TEXT PRIMARY KEY,legacy_id TEXT,'
                    'raw_json TEXT NOT NULL,raw_hash TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,'
                    'source_id TEXT,target_record_id TEXT,payload_json TEXT,payload_hash TEXT,'
                    'evidence_json TEXT NOT NULL,evidence_hash TEXT NOT NULL)')
                db.execute('CREATE INDEX migration_originals ON migration_entries(legacy_id)')
                db.execute('CREATE TABLE migration_observations(window_id TEXT NOT NULL REFERENCES migration_windows(window_id),'
                    'ordinal INTEGER NOT NULL,entry_id TEXT NOT NULL REFERENCES migration_entries(entry_id),'
                    'PRIMARY KEY(window_id,ordinal))')
            db.execute('CREATE INDEX IF NOT EXISTS migration_targets ON migration_entries(target_record_id)')

    def _guard(self, db, *, allow_export_only=False):
        versions = db.execute('SELECT version FROM migration_meta').fetchall()
        allowed = {1, 2} if allow_export_only else {2}
        if len(versions) != 1 or versions[0][0] not in allowed:
            raise ValueError('migration ledger schema version is unsupported')
        rows = db.execute('SELECT document,fingerprint FROM migration_plan').fetchall()
        if (len(rows) != 1 or rows[0]['document'] != self._plan_json
                or rows[0]['fingerprint'] != self.plan.fingerprint):
            raise ValueError('migration plan scope changed or is unreadable; preserve the original ledger')

    def _window_row(self, db, row):
        try:
            _canonical_uuid(row['window_id'])
            start, end = _instant(row['start_time']), _instant(row['end_time'])
            evidence = json.loads(row['evidence'])
            if (start != row['start_time'] or end != row['end_time'] or _time(end) <= _time(start)
                    or _time(start) < self._start or _time(end) > self._end
                    or row['status'] not in {'open', 'complete', 'split'}
                    or _json(evidence) != row['evidence'] or _hash(row['evidence']) != row['evidence_hash']):
                raise ValueError
            if row['status'] == 'complete':
                count = db.execute('SELECT COUNT(*),MIN(ordinal),MAX(ordinal) FROM migration_observations '
                                   'WHERE window_id=?', (row['window_id'],)).fetchone()
                if (set(evidence) != {'complete', 'provider_count', 'cap', 'observation_hash'} or evidence['complete'] is not True
                        or type(evidence['provider_count']) is not int or type(evidence['cap']) is not int
                        or not 1 <= evidence['cap'] <= 4000 or not 0 <= evidence['provider_count'] < evidence['cap']
                        or count[0] != evidence['provider_count']
                        or (count[0] and (count[1] != 0 or count[2] != count[0] - 1))
                        or evidence['observation_hash'] != self._observations_hash(db, row['window_id'], count[0])):
                    raise ValueError
            elif row['status'] == 'open' and evidence:
                raise ValueError
            elif row['status'] == 'split' and set(evidence) != {'children'}:
                raise ValueError
            return {'start': _time(start), 'end': _time(end), 'status': row['status'], 'evidence': evidence}
        except (ValueError, TypeError, KeyError):
            raise ValueError('migration window coverage is corrupt or unreadable') from None

    def _window(self, db, identity):
        row = db.execute('SELECT * FROM migration_windows WHERE window_id=?', (identity,)).fetchone()
        return self._window_row(db, row) if row else None

    def _observations_hash(self, db, window_id, count):
        digest, seen = hashlib.sha256(), 0
        for row in db.execute('SELECT observations.ordinal,entries.entry_id,entries.raw_hash '
                'FROM migration_observations observations JOIN migration_entries entries '
                'ON entries.entry_id=observations.entry_id WHERE observations.window_id=? '
                'ORDER BY observations.ordinal LIMIT 4001', (window_id,)):
            if type(row['ordinal']) is not int or row['ordinal'] != seen:
                raise ValueError('migration observation ordinals are not contiguous integers')
            digest.update((_json({'ordinal': row['ordinal'], 'entry_id': row['entry_id'],
                                  'raw_hash': row['raw_hash']}) + '\n').encode())
            seen += 1
        if seen != count or seen > 4000:
            raise ValueError('migration observation coverage is incomplete')
        return digest.hexdigest()

    def _windows(self, db):
        windows = {}
        try:
            for row in db.execute('SELECT * FROM migration_windows LIMIT ?', (_MAX_WINDOWS + 1,)):
                if len(windows) >= _MAX_WINDOWS:
                    raise ValueError('migration window coverage exceeds its read bound')
                windows[row['window_id']] = self._window_row(db, row)
            for identity, window in windows.items():
                if window['status'] != 'split':
                    continue
                evidence = window['evidence']
                if set(evidence) != {'children'}:
                    raise ValueError
                self._split_proof(identity, evidence['children'], windows)
        except (ValueError, TypeError, KeyError):
            raise ValueError('migration window coverage is corrupt or unreadable') from None
        return windows

    @staticmethod
    def _split_children(children):
        if (not isinstance(children, list) or not 2 <= len(children) <= 64
                or any(not isinstance(child, str) for child in children) or len(set(children)) != len(children)):
            raise ValueError('migration split children are malformed')
        for child in children:
            _canonical_uuid(child)

    def _split_proof(self, parent, children, windows):
        self._split_children(children)
        original = windows[parent]
        intervals = []
        for identity in children:
            if identity not in windows or identity == parent:
                raise ValueError('migration split child is unavailable')
            child = windows[identity]
            if (child['start'] < original['start'] or child['end'] > original['end']
                    or child['end'] - child['start'] >= original['end'] - original['start']):
                raise ValueError('migration split child does not narrow its parent')
            intervals.append((child['start'], child['end']))
        if not _covers(intervals, original['start'], original['end']):
            raise ValueError('migration split children leave a coverage gap')

    def open_window(self, window_id, *, start_time, end_time):
        with self.storage._db(write=True) as db:
            self._guard(db)
            self._open_window(db, window_id, start_time, end_time)

    def _open_window(self, db, window_id, start_time, end_time):
        _canonical_uuid(window_id)
        start, end = _instant(start_time), _instant(end_time)
        if _time(start) < self._start or _time(end) > self._end or _time(end) <= _time(start):
            raise ValueError('migration window is outside the immutable plan range')
        existing = self._window(db, window_id)
        if existing is not None:
            if (existing['start'], existing['end']) != (_time(start), _time(end)):
                raise ValueError('migration window range changed')
            return
        if db.execute('SELECT COUNT(*) FROM migration_windows').fetchone()[0] >= _MAX_WINDOWS:
            raise ValueError('migration window count exceeds its bound')
        db.execute('INSERT INTO migration_windows VALUES (?,?,?,?,?,?)',
                   (window_id, start, end, 'open', '{}', _hash('{}')))

    def window(self, window_id):
        """Read one plan-bound window's metadata for resume, without a raw audit."""
        _canonical_uuid(window_id)
        with self.storage._db() as db:
            self._guard(db)
            row = self._window(db, window_id)
            if row is None:
                return None
            if row['status'] == 'split':
                children = row['evidence']['children']
                self._split_children(children)
                windows = {window_id: row}
                for identity in children:
                    child = self._window(db, identity)
                    if child is not None:
                        windows[identity] = child
                self._split_proof(window_id, children, windows)
            return {'window_id': window_id, 'start_time': _instant(row['start'].isoformat()),
                'end_time': _instant(row['end'].isoformat()), 'status': row['status'],
                'evidence': row['evidence'], 'raw_integrity': 'not-scanned'}

    def _entry(self, db, entry_id, *, verify_target=True):
        row = db.execute('SELECT * FROM migration_entries WHERE entry_id=?', (entry_id,)).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row['raw_json'])
            expected = _hash(_json({'source_type': self.plan.source_data_type,
                                   'legacy_id': row['legacy_id'], 'raw_hash': row['raw_hash']}))
            if (row['entry_id'] != expected or _json(raw) != row['raw_json'] or _hash(row['raw_json']) != row['raw_hash']
                    or row['status'] not in _STATES or row['legacy_id'] != self._legacy_id(raw)):
                raise ValueError
            evidence = json.loads(row['evidence_json'])
            if _json(evidence) != row['evidence_json'] or _hash(row['evidence_json']) != row['evidence_hash']:
                raise ValueError
            self._verify_entry_conversion(row, raw, evidence)
            if verify_target and row['status'] == 'converted':
                for peer in self._target_entries(db, row['target_record_id']):
                    if peer.status != 'converted' or peer._payload_json != row['payload_json']:
                        raise ValueError('migration target has conflicting proposals')
        except (TypeError, ValueError, KeyError):
            raise ValueError('migration entry is corrupt or unreadable') from None
        return ExportedEntry(entry_id, row['legacy_id'], raw, row['status'], row['reason'],
                             row['source_id'], row['target_record_id'], row['payload_json'])

    def _target_entries(self, db, target):
        identities, size = [], 0
        for row in db.execute('SELECT entry_id,length(CAST(raw_json AS BLOB)),length(CAST(payload_json AS BLOB)) '
                'FROM migration_entries WHERE target_record_id=? LIMIT 1001', (target,)):
            size += row[1] + (row[2] or 0)
            if len(identities) >= 1000 or size > 4 * 1024 * 1024:
                raise ValueError('migration target collision group exceeds its read bound')
            identities.append(row[0])
        # Validate every peer, including the identical-payload fast path. Never
        # infer agreement from mutable hash/target metadata alone.
        return [self._entry(db, identity, verify_target=False) for identity in identities]

    def _codec(self):
        if self._converter is None:
            from .migration_codecs import resolve_converter
            self._converter = resolve_converter(self.plan)
        return self._converter

    def _verify_entry_conversion(self, row, raw, evidence):
        fields = ('source_id', 'target_record_id', 'payload_json', 'payload_hash')
        if row['status'] == 'exported' or (row['legacy_id'] is None and row['reason'] == 'missing-record-id'):
            if (evidence or any(row[key] is not None for key in fields)
                    or row['legacy_id'] is None and row['status'] != 'quarantined'
                    or row['status'] == 'exported' and (row['reason'] is not None or row['legacy_id'] is None)):
                raise ValueError
            return
        result = self._codec().convert(raw)
        proof = {'converter': self.plan.document['converter'], 'raw_hash': row['raw_hash']}
        if result.status == 'quarantined':
            if (row['status'] != 'quarantined' or row['reason'] != result.reason
                    or any(row[key] is not None for key in fields) or evidence != proof):
                raise ValueError
            return
        payload = _json(result.payload)
        proof['payload_hash'] = _hash(payload)
        if row['status'] == 'quarantined' and row['reason'] == 'target-collision':
            proof['collision'] = True
        elif row['status'] != 'converted' or row['reason'] is not None:
            raise ValueError
        if (row['source_id'] != result.source_id or row['target_record_id'] != result.payload['id']
                or row['payload_json'] != payload or row['payload_hash'] != _hash(payload) or evidence != proof):
            raise ValueError

    def convert_entry(self, entry_id):
        """Freeze a supported offline conversion, quarantining conflicting targets.

        All raw variants survive. A collision makes every prepared proposal for
        that target ineligible; identical payloads may converge. This never
        grants upload authority or certifies history/cutover completeness.
        """
        codec = self._codec()
        with self.storage._db(write=True) as db:
            self._guard(db)
            entry = self._entry(db, entry_id)
            if entry is None:
                raise ValueError('migration entry is unavailable')
            if entry.status != 'exported':
                return entry
            result = codec.convert(entry.raw)
            payload = _json(result.payload) if result.payload is not None else None
            payload_hash = _hash(payload) if payload is not None else None
            proof = {'converter': self.plan.document['converter'], 'raw_hash': result.raw_hash}
            if payload is not None:
                proof['payload_hash'] = payload_hash
            evidence = _json(proof)
            target = result.payload['id'] if payload is not None else None
            db.execute('UPDATE migration_entries SET status=?,reason=?,source_id=?,target_record_id=?,'
                'payload_json=?,payload_hash=?,evidence_json=?,evidence_hash=? WHERE entry_id=?',
                (result.status, result.reason, result.source_id, target, payload, payload_hash,
                 evidence, _hash(evidence), entry_id))
            if target is not None:
                matches = self._target_entries(db, target)
                if any(prior._payload_json != payload or prior.reason == 'target-collision' for prior in matches):
                    for prior in matches:
                        conflict = _json({'converter': self.plan.document['converter'],
                            'raw_hash': _hash(_json(prior.raw)), 'payload_hash': _hash(prior._payload_json),
                            'collision': True})
                        db.execute("UPDATE migration_entries SET status='quarantined',reason='target-collision',"
                            'evidence_json=?,evidence_hash=? WHERE entry_id=?',
                            (conflict, _hash(conflict), prior.entry_id))
            return self._entry(db, entry_id, verify_target=False)

    @staticmethod
    def _legacy_id(raw):
        value = raw.get('id')
        return value if isinstance(value, str) and 1 <= len(value.encode()) <= 512 and value.strip() else None

    def entry(self, entry_id):
        if not isinstance(entry_id, str) or len(entry_id) != hashlib.sha256().digest_size * 2:
            raise ValueError('migration entry identifier is malformed')
        with self.storage._db() as db:
            self._guard(db)
            return self._entry(db, entry_id)

    def _prepare_export(self, raw):
        encoded = _json(raw)
        original = json.loads(encoded)
        legacy_id, raw_hash = self._legacy_id(original), _hash(encoded)
        entry_id = _hash(_json({'source_type': self.plan.source_data_type, 'legacy_id': legacy_id, 'raw_hash': raw_hash}))
        return entry_id, legacy_id, encoded, raw_hash

    def _capture_export(self, db, window_id, ordinal, prepared, *, sealed=False, verify_target=True):
        entry_id = prepared[0]
        observation = db.execute('SELECT entry_id FROM migration_observations WHERE window_id=? AND ordinal=?',
                                 (window_id, ordinal)).fetchone()
        if observation is not None:
            if observation['entry_id'] != entry_id:
                raise ValueError('migration observation changed; retain its original export')
            retained = self._entry(db, entry_id, verify_target=verify_target)
            if retained is None:
                raise ValueError('migration observation raw entry is unavailable or corrupt')
            return retained
        if sealed:
            raise ValueError('migration sealed observation changed')
        self._retain_export(db, prepared, verify_target=verify_target)
        db.execute('INSERT INTO migration_observations VALUES (?,?,?)', (window_id, ordinal, entry_id))
        return self._entry(db, entry_id, verify_target=verify_target)

    def _retain_export(self, db, prepared, *, verify_target=True):
        """Retain one raw version; its actor separately binds origin evidence."""
        entry_id, legacy_id, encoded, raw_hash = prepared
        if self._entry(db, entry_id, verify_target=verify_target) is None:
            status, reason = ('exported', None) if legacy_id else ('quarantined', 'missing-record-id')
            db.execute('INSERT INTO migration_entries VALUES (?,?,?,?,?,?,NULL,NULL,NULL,NULL,?,?)',
                (entry_id, legacy_id, encoded, raw_hash, status, reason, '{}', _hash('{}')))
        return self._entry(db, entry_id, verify_target=verify_target)

    def capture_export(self, window_id, *, ordinal, raw):
        _canonical_uuid(window_id)
        if type(ordinal) is not int or not 0 <= ordinal < 4000:
            raise ValueError('migration observation ordinal exceeds its bounded window')
        prepared = self._prepare_export(raw)
        with self.storage._db(write=True) as db:
            self._guard(db)
            window = self._window(db, window_id)
            if window is None or window['status'] != 'open':
                raise ValueError('migration export window is missing or already complete/sealed')
            return self._capture_export(db, window_id, ordinal, prepared)

    @staticmethod
    def _complete_read(complete, provider_count, cap):
        if (complete is not True or type(provider_count) is not int or type(cap) is not int
                or not 1 <= cap <= 4000 or not 0 <= provider_count < cap):
            raise ValueError('migration read is incomplete, saturated or outside its row bound')

    def capture_window(self, window_id, *, records, complete, provider_count, cap):
        """Atomically retain one bounded complete snapshot and its window seal.

        The reader must already retain its original private response artifact
        and establish the complete-read assertion; this method cannot prove a
        provider's completeness. A crash rolls back every new observation and
        the seal together. Returned IDs grant no conversion/upload authority.
        """
        _canonical_uuid(window_id)
        self._complete_read(complete, provider_count, cap)
        if not isinstance(records, (list, tuple)) or len(records) != provider_count:
            raise ValueError('migration provider count does not match the raw snapshot')
        prepared, size = [], 0
        for raw in records:
            row = self._prepare_export(raw)
            size += len(row[2].encode())
            if size > _MAX_WINDOW_JSON:
                raise ValueError('migration raw snapshot exceeds its JSON byte bound; preserve the response artifact')
            prepared.append(row)
        with self.storage._db(write=True) as db:
            self._guard(db)
            window = self._window(db, window_id)
            if window is None or window['status'] not in {'open', 'complete'}:
                raise ValueError('migration window is unavailable for snapshot completion')
            for ordinal, row in enumerate(prepared):
                # Raw snapshot capture is not a target eligibility read. Check
                # each raw/codec proof without revalidating the whole peer group
                # for every identical target; entry() remains the authority read.
                self._capture_export(db, window_id, ordinal, row,
                    sealed=window['status'] == 'complete', verify_target=False)
            self._seal_window(db, window_id, provider_count, cap)
        return {'observations': provider_count, 'entry_ids': [row[0] for row in prepared]}

    def seal_window(self, window_id, *, complete, provider_count, cap):
        _canonical_uuid(window_id)
        self._complete_read(complete, provider_count, cap)
        with self.storage._db(write=True) as db:
            self._guard(db)
            self._seal_window(db, window_id, provider_count, cap)

    def _seal_window(self, db, window_id, provider_count, cap):
        window = self._window(db, window_id)
        if window is None or window['status'] == 'split':
            raise ValueError('migration window is unavailable for completion')
        count = db.execute('SELECT COUNT(*),MIN(ordinal),MAX(ordinal) FROM migration_observations '
                           'WHERE window_id=?', (window_id,)).fetchone()
        if count[0] != provider_count or (provider_count and (count[1] != 0 or count[2] != provider_count - 1)):
            raise ValueError('migration provider count does not match retained contiguous observations')
        for observation in db.execute('SELECT DISTINCT entry_id FROM migration_observations WHERE window_id=?',
                                      (window_id,)):
            # A window seal proves retained raw observations, not conversion
            # eligibility. Validate raw/proposal bytes once per unique entry.
            self._entry(db, observation['entry_id'], verify_target=False)
        evidence = _json({'complete': True, 'provider_count': provider_count, 'cap': cap,
                          'observation_hash': self._observations_hash(db, window_id, count[0])})
        if window['status'] == 'complete':
            if _json(window['evidence']) != evidence:
                raise ValueError('migration completion evidence changed')
            return
        db.execute("UPDATE migration_windows SET status='complete',evidence=?,evidence_hash=? WHERE window_id=?",
                   (evidence, _hash(evidence), window_id))

    def mark_split(self, window_id, *, children):
        _canonical_uuid(window_id)
        self._split_children(children)
        with self.storage._db(write=True) as db:
            self._guard(db)
            self._mark_split(db, window_id, children)

    def _mark_split(self, db, window_id, children):
        parent = self._window(db, window_id)
        if parent is None or parent['status'] == 'complete':
            raise ValueError('migration parent window is missing or complete')
        windows = {window_id: parent}
        for identity in children:
            child = self._window(db, identity)
            if child is not None:
                windows[identity] = child
        self._split_proof(window_id, children, windows)
        evidence = _json({'children': sorted(children)})
        if parent['status'] == 'split':
            if _json(parent['evidence']) != evidence:
                raise ValueError('migration split evidence changed')
            return
        db.execute("UPDATE migration_windows SET status='split',evidence=?,evidence_hash=? WHERE window_id=?",
                   (evidence, _hash(evidence), window_id))

    def split_window(self, window_id, *, children):
        """Create exact child ranges and link their parent in one transaction."""
        _canonical_uuid(window_id)
        if (not isinstance(children, list) or not 2 <= len(children) <= 64
                or any(not isinstance(child, dict) or set(child) != {'window_id', 'start_time', 'end_time'}
                       for child in children)):
            raise ValueError('migration split child windows are malformed')
        identities = [child['window_id'] for child in children]
        self._split_children(identities)
        with self.storage._db(write=True) as db:
            self._guard(db)
            for child in children:
                self._open_window(db, child['window_id'], child['start_time'], child['end_time'])
            self._mark_split(db, window_id, identities)
        return {'children': sorted(identities)}

    def status(self):
        with self.storage._db() as db:
            self._guard(db)
            windows = self._windows(db)
            counts = {}
            for row in db.execute('SELECT status,COUNT(*) AS count FROM migration_entries GROUP BY status'):
                if row['status'] not in _STATES:
                    raise ValueError('migration entry classification is unknown or corrupt')
                counts[row['status']] = row['count']
            if db.execute("SELECT 1 FROM migration_entries WHERE status='converted' AND "
                    '(reason IS NOT NULL OR legacy_id IS NULL OR source_id IS NULL OR target_record_id IS NULL '
                    'OR payload_json IS NULL OR payload_hash IS NULL) LIMIT 1').fetchone():
                raise ValueError('migration converted classification is corrupt')
            observations = db.execute('SELECT COUNT(*) FROM migration_observations').fetchone()[0]
            variants = db.execute('SELECT COUNT(*) FROM (SELECT legacy_id FROM migration_entries '
                'WHERE legacy_id IS NOT NULL GROUP BY legacy_id HAVING COUNT(*)>1)').fetchone()[0]
            complete = bool(windows) and not any(row['status'] == 'open' for row in windows.values()) and _covers(
                [(row['start'], row['end']) for row in windows.values() if row['status'] == 'complete'],
                _time(self.plan.document['start_time']), _time(self.plan.document['end_time']))
            return {'counts': counts, 'observations': observations, 'original_variants': variants,
                    'coverage_complete': complete, 'raw_integrity': 'not-scanned', 'historical_complete': False}
