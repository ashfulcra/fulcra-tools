"""Profile-local standalone media authority. Legacy state is evidence only."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import tempfile
import time
from urllib.parse import quote
from uuid import UUID

from fulcra_collect import config, db as collect_db
from fulcra_collect.config_leases import _file_lock
from fulcra_common.data_types import CreationJournal
from fulcra_common.json_values import snapshot_json
from fulcra_common.v1 import _destination
from .standalone_v1 import open_media
from .v1 import media_spec

CATEGORIES = ('watched', 'listened', 'read')
FILENAME = 'cli-media-v1.json'


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key; retain original evidence')
        result[key] = value
    return result


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1048576:
            raise ValueError('media configuration must be a bounded regular file')
        result = json.load(stream, object_pairs_hook=_pairs,
                         parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
        if result is None:
            raise ValueError('existing JSON null is UNKNOWN, not absent')
        return snapshot_json(result)


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise ValueError('expected canonical UUID')
    return value


def legacy_selection(path):
    raw = _read(path)
    if raw is None:
        return dict.fromkeys(CATEGORIES)
    if not isinstance(raw, dict):
        raise ValueError('legacy state UNKNOWN; retain original evidence')
    if any(key + '_definition_id' not in raw for key in CATEGORIES):
        raise ValueError('legacy selection fields incomplete; preserve original evidence')
    selected = {key: raw[key + '_definition_id'] for key in CATEGORIES}
    for value in selected.values():
        if value is not None:
            _uuid(value)
    return selected


def read_config(path):
    raw = _read(path)
    if raw is None:
        return None
    if (not isinstance(raw, dict) or set(raw) != {'version', 'account_id', 'v1_destination_ids', 'legacy_selection'}
            or type(raw['version']) is not int or raw['version'] != 1):
        raise ValueError('standalone media configuration UNKNOWN; preserve original evidence')
    _uuid(raw['account_id'])
    for field in ('v1_destination_ids', 'legacy_selection'):
        if not isinstance(raw[field], dict) or set(raw[field]) != set(CATEGORIES):
            raise ValueError('standalone media configuration categories incomplete')
    for pin in raw['v1_destination_ids'].values():
        _destination(pin, 'Event')
    for value in raw['legacy_selection'].values():
        if value is not None:
            _uuid(value)
    return raw


def _status_schema(db):
    # Inspect existing schema only. Never run the writer's initializing opener.
    expected = {
        'schema_version': [('version', 'INTEGER', 0, None, 1), ('applied_at', 'TEXT', 1, None, 0)],
        'record_outboxes': [('plugin_id', 'TEXT', 1, None, 1), ('key', 'TEXT', 1, None, 2),
                           ('value_json', 'TEXT', 1, None, 0), ('updated_at', 'TEXT', 1, None, 0)],
    }
    for table, columns in expected.items():
        rows = db.execute('PRAGMA table_info(' + table + ')').fetchmany(len(columns) + 1)
        if [row[1:] for row in rows] != columns:
            raise ValueError('existing outbox schema UNKNOWN')
    versions = db.execute('SELECT version FROM schema_version ORDER BY version LIMIT ?',
                          (collect_db.LATEST_VERSION + 1,)).fetchall()
    values = [row[0] for row in versions]
    if (not values or type(values[-1]) is not int or not 8 <= values[-1] <= collect_db.LATEST_VERSION
            or values != list(range(1, values[-1] + 1))):
        raise ValueError('existing state schema version UNKNOWN')


def pending_status(root):
    path = root / 'state.db'
    if not path.exists() and not path.is_symlink():
        return {'state': 'absent'}
    try:
        if path.is_symlink():
            raise ValueError('symlink')
        with sqlite3.connect('file:' + quote(str(path), safe='/') + '?mode=ro', uri=True,
                             timeout=1) as db:
            deadline = time.monotonic() + 1
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            db.execute('BEGIN')
            _status_schema(db)
            sizes = db.execute("SELECT length(CAST(value_json AS BLOB)) FROM record_outboxes WHERE plugin_id=? AND key=? LIMIT 2",
                               ('cli-media', 'v1_media_pending')).fetchall()
            if not sizes:
                return {'state': 'absent'}
            if len(sizes) != 1 or not isinstance(sizes[0][0], int) or sizes[0][0] > collect_db.RECORD_OUTBOX_MAX_VALUE_BYTES:
                raise ValueError('outbox size UNKNOWN')
            raw = db.execute("SELECT value_json FROM record_outboxes WHERE plugin_id=? AND key=? LIMIT 1",
                             ('cli-media', 'v1_media_pending')).fetchone()[0]
            if raw == 'null':
                return {'state': 'absent'}
            # Only a digest escapes this read boundary. Queued records contain
            # private titles, notes and enrichment and must never render here.
            encoded = raw.encode('utf-8')
            return {'state': 'UNKNOWN', 'retained': True, 'bytes': len(encoded),
                    'sha256': hashlib.sha256(encoded).hexdigest(),
                    'note': 'retained payload requires guarded recovery; not live verified'}
    except Exception:
        return {'state': 'UNKNOWN', 'note': 'existing outbox unreadable'}


def _journal_check(root, account, pins):
    path = root / 'v1-type-creations.sqlite'
    if not path.exists() and not path.is_symlink():
        return
    journal = CreationJournal(path, read_only=True)
    for category, pin in pins.items():
        row = journal.read(account, media_spec(category))
        if row and (row[1] != 'resolved' or row[2] != pin):
            raise ValueError('media creation binding conflicts or is unresolved; preserve original intent')


def _save(path, candidate, original, *, guard=lambda: None):
    if read_config(path) != original:
        raise RuntimeError('media configuration changed; retain original evidence')
    fd, name = tempfile.mkstemp(prefix='.cli-media-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(candidate, stream, allow_nan=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        guard()
        if read_config(path) != original:
            raise RuntimeError('media configuration changed; retain original evidence')
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def configured_media(legacy_path, *, client=None, check_only=False, bootstrap=False, pins=None,
                     lease_timeout_s=10):
    if (type(lease_timeout_s) not in (int, float) or not math.isfinite(lease_timeout_s)
            or lease_timeout_s < 0):
        raise ValueError('media settings timeout must be finite nonnegative numeric seconds')
    root = config.profile_path()
    with config.pinned_profile(root):
        with _file_lock(config.config_dir() / '.cli-media-settings.lock',
                        time.monotonic() + min(lease_timeout_s, 30), mode=fcntl.LOCK_EX):
            path = root / FILENAME
            saved = read_config(path)
            if saved is None:
                if pending_status(root)['state'] != 'absent':
                    raise RuntimeError('missing media configuration with retained pending state; preserve original authority')
                if not bootstrap:
                    raise RuntimeError('Run `fulcra-media bootstrap` first')
            settings = saved or {}
            def before_resume(ctx, session):
                if saved and session.account_id != saved['account_id']:
                    raise RuntimeError('media account mismatch; retain original pending work')
                original_guard = session.guard
                def guarded(**kwargs):
                    original_guard(**kwargs)
                    if read_config(path) != saved:
                        raise RuntimeError('media configuration changed; retain original evidence')
                    _journal_check(root, session.account_id, settings.get('v1_destination_ids', {}))
                session.guard = guarded
                guarded()
            with open_media(settings, client=client, check_only=check_only,
                            before_resume=before_resume) as run:
                selected = legacy_selection(legacy_path)
                if not bootstrap:
                    if selected != saved['legacy_selection']:
                        raise RuntimeError('legacy selection changed; run bootstrap with explicit v1 mappings')
                    run.writer.prepare(CATEGORIES)
                else:
                    requested = {k: v for k, v in (pins or {}).items() if v is not None}
                    for category, pin in requested.items():
                        media_spec(category)
                        _destination(pin, 'Event')
                    for category, legacy in selected.items():
                        if legacy and (saved is None or selected != saved['legacy_selection']) and category not in requested:
                            raise RuntimeError('map every retained legacy selection with --watched/--listened/--read Event/UUID')
                    desired = {**(saved['v1_destination_ids'] if saved else {}), **requested}
                    _journal_check(root, run.session.account_id, desired)
                    resolved = {}
                    for category in CATEGORIES:
                        pin = desired.get(category)
                        run.session.guard()
                        result = run.session.resolve_destination(media_spec(category), pinned_id=pin,
                                                                 allow_create=pin is None)
                        _journal_check(root, run.session.account_id, {category: result.data_type})
                        resolved[category] = result.data_type
                    candidate = {'version': 1, 'account_id': run.session.account_id,
                                 'v1_destination_ids': resolved, 'legacy_selection': selected}
                    def local_commit_guard():
                        run.session._local_guard()
                        _journal_check(root, run.session.account_id, resolved)
                        if legacy_selection(legacy_path) != selected:
                            raise RuntimeError('legacy selection changed; preserve original evidence')
                    def persist():
                        local_commit_guard()
                        _save(path, candidate, saved, guard=local_commit_guard)
                    run.session.commit(persist)
                def source_local_authority():
                    run.session._local_guard()
                    if read_config(path) != saved:
                        raise RuntimeError('media configuration changed; retain original evidence')
                    _journal_check(root, run.session.account_id, settings.get('v1_destination_ids', {}))
                    if legacy_selection(legacy_path) != selected:
                        raise RuntimeError('legacy selection changed; retain original evidence')
                    run.session._local_guard()
                run.ctx._media_local_authority = source_local_authority
                try:
                    yield run
                finally:
                    del run.ctx._media_local_authority


def cached_status(legacy_path):
    """Read only existing files and SQLite, including a live WAL when present."""
    root = config.profile_path()
    result = {'verification': 'cached only; not live verified', 'profile': str(root)}
    with config.pinned_profile(root):
        fd = None
        try:
            try:
                fd = os.open(root / '.cli-media-settings.lock', os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                pass
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            result['pending'] = pending_status(root)
            for key, loader in [('v1', lambda: read_config(root / FILENAME)),
                                ('retained_legacy', lambda: legacy_selection(legacy_path))]:
                try:
                    result[key] = loader()
                except Exception:
                    result[key] = {'state': 'UNKNOWN'}
            if config.profile_path() != root:
                raise RuntimeError('profile changed')
        except Exception:
            result.update(v1={'state': 'UNKNOWN'}, pending={'state': 'UNKNOWN'},
                          retained_legacy={'state': 'UNKNOWN'})
        finally:
            if fd is not None:
                os.close(fd)
    return result
