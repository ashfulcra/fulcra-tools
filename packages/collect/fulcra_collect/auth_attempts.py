"""Single active interactive sign-in, fenced across processes and restart.

Only hashes and ordering metadata live here. Device codes and credentials never
enter this database. A claimed grant is ambiguous after a crash and requires a
fresh sign-in; it is never silently replayed. The short final callback must only
commit local credentials under the account-transition gate, without network IO.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import fcntl
import hashlib
import math
import os
import re
from pathlib import Path
import sqlite3
import stat
import time
from uuid import UUID, uuid4


class AuthAttemptError(ValueError):
    """Stale, expired, ambiguous, or unreadable sign-in; start again."""


def _refuse():
    raise AuthAttemptError('Sign-in changed, expired, or could not be verified. Start again.')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _code_hash(code):
    if not isinstance(code, str) or not code or len(code) > 4096:
        _refuse()
    try:
        return hashlib.sha256(code.encode()).hexdigest()
    except UnicodeError:
        _refuse()


class AuthAttempts:
    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def _private_fd(self, path):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                _refuse()
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _lease(self, path):
        with self._private_fd(path) as fd:
            deadline = time.monotonic() + 10
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        _refuse()
                    time.sleep(0.02)
            yield

    @contextmanager
    def refresh_scope(self):
        """Serialize rotating refresh grants across worker processes."""
        with self._lease(self.path.with_suffix('.refresh.lock')):
            yield

    @contextmanager
    def _locked(self):
        # Own this lock before account-transition/config locks; callers must not
        # enter it while holding those locks. No provider request runs here.
        with self._lease(self.path.with_suffix('.lock')):
            with self._private_fd(self.path):
                pass
            with sqlite3.connect(self.path, timeout=5) as db:
                db.row_factory = sqlite3.Row
                db.execute('PRAGMA synchronous=FULL')
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not tables:
                    db.execute('''CREATE TABLE auth_state (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL,
                        attempt TEXT, kind TEXT, status TEXT, code_hash TEXT,
                        created REAL, expires REAL, interval REAL, next_poll REAL,
                        committed_attempt TEXT, committed_kind TEXT, refresh_claim TEXT)''')
                    db.execute('INSERT INTO auth_state(singleton,version) VALUES(1,1)')
                    db.commit()
                rows = db.execute('SELECT * FROM auth_state').fetchall()
                if len(rows) != 1 or rows[0]['version'] != 1 or rows[0]['singleton'] != 1:
                    _refuse()
                yield db

    def _active(self, db, attempt, statuses):
        try:
            if str(UUID(attempt)) != attempt:
                _refuse()
        except (ValueError, TypeError, AttributeError):
            _refuse()
        row = db.execute('SELECT * FROM auth_state').fetchone()
        now = self.clock()
        if (row['attempt'] != attempt or row['status'] not in statuses or not _number(now)
                or not _number(row['created']) or now < row['created']):
            _refuse()
        if row['kind'] == 'device' and row['status'] != 'starting':
            if not _number(row['expires']) or now >= row['expires']:
                _refuse()
        return row, now

    def begin(self, kind):
        if kind not in ('device', 'token', 'clear'):
            _refuse()
        attempt, now = str(uuid4()), self.clock()
        if not _number(now):
            _refuse()
        with self._locked() as db:
            db.execute('''UPDATE auth_state SET attempt=?,kind=?,status='starting',code_hash=NULL,
                created=?,expires=NULL,interval=NULL,next_poll=NULL''', (attempt, kind, now))
        return attempt

    def bind(self, attempt, code, expires_in, interval):
        digest = _code_hash(code)
        if (not _number(expires_in) or not 0 < expires_in <= 1800
                or not _number(interval) or not 1 <= interval <= 60):
            _refuse()
        with self._locked() as db:
            row, now = self._active(db, attempt, {'starting'})
            if row['kind'] != 'device':
                _refuse()
            db.execute("""UPDATE auth_state SET status='pending',code_hash=?,expires=?,
                interval=?,next_poll=?""", (digest, now + expires_in, max(interval, 5), now))

    def claim(self, attempt, code):
        digest = _code_hash(code)
        with self._locked() as db:
            row, now = self._active(db, attempt, {'pending'})
            if row['kind'] != 'device' or row['code_hash'] != digest:
                _refuse()
            if (not _number(row['next_poll']) or not _number(row['interval'])
                    or not 5 <= row['interval'] <= 60):
                _refuse()
            if now < row['next_poll']:
                return min(60, max(1, math.ceil(row['next_poll'] - now)))
            db.execute("UPDATE auth_state SET status='polling'")
        return None

    def pending(self, attempt, *, slow=False):
        with self._locked() as db:
            row, now = self._active(db, attempt, {'polling'})
            interval = row['interval']
            if not _number(interval) or not 5 <= interval <= 60:
                _refuse()
            interval = min(60, interval * 2) if slow else interval
            db.execute("UPDATE auth_state SET status='pending',interval=?,next_poll=?",
                       (interval, now + interval))
        return interval

    def fail(self, attempt):
        with self._locked() as db:
            db.execute("UPDATE auth_state SET status='failed' WHERE attempt=?", (attempt,))

    def cancel(self, attempt):
        with self._locked() as db:
            db.execute("""UPDATE auth_state SET status='failed'
                WHERE attempt=? AND status IN ('starting','pending','polling')""", (attempt,))

    def finish(self, attempt, commit, *, transition=nullcontext):
        with self._locked() as db:
            row, _ = self._active(db, attempt, {'starting', 'polling'})
            if row['kind'] == 'device' and row['status'] != 'polling':
                _refuse()
            # Persist the no-retry state BEFORE a potentially partial keychain
            # commit. A crash cannot turn that commit into fresh authority.
            db.execute("UPDATE auth_state SET status='committing'")
            db.commit()
            try:
                with transition():
                    commit()
                    db.execute("""UPDATE auth_state SET status='complete',refresh_claim=NULL,
                        committed_attempt=?,committed_kind=?""", (attempt, row['kind']))
                    db.commit()
            except BaseException:
                db.execute("UPDATE auth_state SET status='failed'")
                db.commit()
                raise

    def committed(self):
        with self._locked() as db:
            row = db.execute('SELECT committed_attempt,committed_kind FROM auth_state').fetchone()
            return tuple(row)

    @contextmanager
    def _refresh_db(self, *, write=False):
        # Refresh may run inside an existing account-shared mutation scope.
        # Never acquire the interactive-auth lease here: its holder may be
        # waiting for account EX. Only finish changes committed authority, and
        # it now does so INSIDE account EX. Caller holds account SH throughout
        # a refresh claim/final commit; SQLite serializes these short writes.
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        except FileNotFoundError:
            if write:
                _refuse()
            yield None
            return
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                _refuse()
        finally:
            os.close(fd)
        mode = 'rw' if write else 'ro'
        with sqlite3.connect(self.path.absolute().as_uri() + '?mode=' + mode,
                             uri=True, timeout=5) as db:
            db.row_factory = sqlite3.Row
            if write:
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT * FROM auth_state').fetchall()
            if len(rows) != 1 or rows[0]['version'] != 1 or rows[0]['singleton'] != 1:
                _refuse()
            yield db

    def read_committed(self):
        """Read-only revision snapshot; caller uses account SH to fence writes."""
        with self._refresh_db() as db:
            if db is None:
                return None, None
            row = db.execute('SELECT committed_attempt,committed_kind FROM auth_state').fetchone()
            return tuple(row)

    def claim_refresh(self, revision, fingerprint):
        if not isinstance(fingerprint, str) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
            _refuse()
        with self._refresh_db(write=True) as db:
            row = db.execute('SELECT * FROM auth_state').fetchone()
            if ((row['committed_attempt'], row['committed_kind']) != revision
                    or row['refresh_claim'] is not None):
                _refuse()
            db.execute('UPDATE auth_state SET refresh_claim=?', (fingerprint,))

    @contextmanager
    def refresh_commit_scope(self, revision, fingerprint):
        with self._refresh_db(write=True) as db:
            row = db.execute('SELECT * FROM auth_state').fetchone()
            if ((row['committed_attempt'], row['committed_kind']) != revision
                    or row['refresh_claim'] != fingerprint):
                _refuse()
            # Clear only after every local credential write succeeded. Failure
            # or crash preserves the consumed/unknown grant marker.
            yield
            db.execute('UPDATE auth_state SET refresh_claim=NULL')
