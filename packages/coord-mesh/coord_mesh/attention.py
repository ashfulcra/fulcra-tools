"""Durable request attention; a transport cursor is never a handled-work ledger.

Caller supplies already-authorized source records and independently verified reply
readback. This store grants no authority, sends nothing, and creates no listener.
"""
from contextlib import contextmanager
import json
import os
import sqlite3
from pathlib import Path

REQUEST_KINDS = frozenset(('directive', 'query', 'message', 'request'))


def canonical(value):
    text = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(text.encode()) > 256 * 1024:
        raise ValueError('mesh attention envelope too large')
    return text


class AttentionStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            self.db.close()
            raise ValueError('unsupported attention schema')
        if version == 0 and self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
            self.db.close()
            raise ValueError('not an attention database')
        if version == 1:
            columns = {r['name'] for r in self.db.execute('PRAGMA table_info(attention)')}
            if columns != {'source_user_id', 'record_id', 'envelope_json', 'state', 'receiver_evidence', 'reply_json', 'reply_state', 'reply_record_id'}:
                self.db.close()
                raise ValueError('unreadable attention schema')
        self.db.execute('PRAGMA journal_mode=WAL')
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS attention (
              source_user_id TEXT NOT NULL, record_id TEXT NOT NULL,
              envelope_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              receiver_evidence TEXT, reply_json TEXT, reply_state TEXT,
              reply_record_id TEXT, PRIMARY KEY(source_user_id, record_id))''')
            self.db.execute('PRAGMA user_version=1')

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    def _entry(self, peer, record_id):
        row = self.db.execute('SELECT * FROM attention WHERE source_user_id=? AND record_id=?', (peer, record_id)).fetchone()
        if row is None:
            raise ValueError('attention entry absent')
        return row

    @contextmanager
    def _write(self):
        # Lock before SELECT: sqlite's connection context alone starts only on DML.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def ingest(self, peer, record_id, envelope, recipient, *, retracted=False):
        if not isinstance(envelope, dict):
            raise ValueError('invalid attention envelope')
        text = canonical(envelope)
        with self._write():
            row = self.db.execute('SELECT envelope_json FROM attention WHERE source_user_id=? AND record_id=?', (peer, record_id)).fetchone()
            if row:
                if row['envelope_json'] != text:
                    raise ValueError('conflicting attention record; retain original and reconcile')
                return False
            if retracted or envelope.get('kind') not in REQUEST_KINDS or envelope.get('to_user') != recipient:
                return False
            if envelope.get('v') != 1 or not peer or not record_id or not recipient:
                raise ValueError('invalid attention binding')
            self.db.execute('INSERT INTO attention(source_user_id,record_id,envelope_json) VALUES(?,?,?)', (peer, record_id, text))
        return True

    def acknowledge(self, peer, record_id, evidence):
        if not isinstance(evidence, str) or not evidence:
            raise ValueError('receiver evidence required')
        with self._write():
            self._entry(peer, record_id)
            self.db.execute('UPDATE attention SET receiver_evidence=? WHERE source_user_id=? AND record_id=?', (evidence, peer, record_id))
        # Receipt deliberately does not dispose of the request.

    def prepare_reply(self, peer, record_id, reply):
        with self._write():
            row = self._entry(peer, record_id)
            incoming = json.loads(row['envelope_json'])
            if not incoming.get('mid') or reply.get('in_reply_to') != incoming['mid'] or reply.get('to_user') != peer or reply.get('kind') != 'response' or not reply.get('mid'):
                raise ValueError('reply binding mismatch')
            text = canonical(reply)
            if row['reply_json']:
                if row['reply_json'] != text:
                    raise ValueError('conflicting reply; reconciliation required')
                return
            self.db.execute("UPDATE attention SET reply_json=?,reply_state='prepared' WHERE source_user_id=? AND record_id=?", (text, peer, record_id))

    def claim_reply(self, peer, record_id):
        with self._write():
            row = self._entry(peer, record_id)
            if row['reply_state'] != 'prepared':
                raise ValueError('reply reconciliation required; do not resend')
            changed = self.db.execute("UPDATE attention SET reply_state='claimed' WHERE source_user_id=? AND record_id=? AND reply_state='prepared'", (peer, record_id)).rowcount
            if changed != 1:
                raise ValueError('reply reconciliation required; do not resend')
            return json.loads(row['reply_json'])

    def verify_reply(self, peer, record_id, actual_envelope, reply_record_id):
        with self._write():
            row = self._entry(peer, record_id)
            if row['reply_state'] not in ('claimed', 'verified') or not reply_record_id or canonical(actual_envelope) != row['reply_json']:
                raise ValueError('reply readback mismatch')
            if row['reply_record_id'] and row['reply_record_id'] != reply_record_id:
                raise ValueError('conflicting reply readback')
            self.db.execute("UPDATE attention SET state='responded',reply_state='verified',reply_record_id=? WHERE source_user_id=? AND record_id=?", (reply_record_id, peer, record_id))

    def verify_legacy_reply(self, peer, record_id, actual_envelope, reply_record_id, evidence):
        """Reconcile a source-read old ACK with an exact textual message anchor.

        No send or invented in_reply_to field. Never replace a pending send claim.
        """
        if not isinstance(actual_envelope, dict) or not reply_record_id or not isinstance(evidence, str) or not evidence:
            raise ValueError('independent legacy reply source evidence required')
        with self._write():
            row = self._entry(peer, record_id)
            incoming = json.loads(row['envelope_json'])
            mid = incoming.get('mid')
            if not mid or actual_envelope.get('kind') != 'response' or not actual_envelope.get('mid') or actual_envelope.get('to_user') != peer or actual_envelope.get('slug') != incoming.get('slug', '') + '-ack' or not str(actual_envelope.get('body', '')).startswith('In reply to ' + mid + ':'):
                raise ValueError('legacy reply binding mismatch')
            text = canonical(actual_envelope)
            if row['reply_json'] and (row['reply_state'] != 'verified' or row['reply_json'] != text or row['reply_record_id'] != reply_record_id):
                raise ValueError('conflicting or uncertain reply; reconciliation required')
            self.db.execute("UPDATE attention SET state='responded',receiver_evidence=?,reply_json=?,reply_state='verified',reply_record_id=? WHERE source_user_id=? AND record_id=?", (evidence, text, reply_record_id, peer, record_id))

    def snapshot(self):
        entries = []
        for row in self.db.execute('SELECT * FROM attention ORDER BY source_user_id,record_id'):
            entry = dict(row)
            entry['envelope'] = json.loads(entry.pop('envelope_json'))
            entry['reply'] = json.loads(entry.pop('reply_json')) if entry['reply_json'] else None
            # pop the null column too, without short-circuit leftovers.
            entry.pop('reply_json', None)
            entries.append(entry)
        return {'schema': 'mesh-attention/1', 'entries': entries, 'pending': [r for r in entries if r['state'] != 'responded']}

    def pending(self):
        return self.snapshot()['pending']
