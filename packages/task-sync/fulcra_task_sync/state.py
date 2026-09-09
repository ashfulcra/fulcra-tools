"""Bounded schema-2 KV records with atomic manifest pointers and source epochs."""
from copy import deepcopy
import hashlib
import json
import uuid

KEY_BYTES = 256
VALUE_BYTES = 65536
CHUNK_BYTES = 24000
MAX_IDS = 10000
MAX_ID_BYTES = 1024
MAX_HISTORY = 500
MAX_VERSION_BYTES = 512


class StateError(ValueError):
    pass


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':'), sort_keys=True).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def validate_record(key, value):
    if not isinstance(key, str) or not 0 < len(key.encode()) <= KEY_BYTES:
        raise StateError('checkpoint key limit')
    if len(encoded(value)) > VALUE_BYTES:
        raise StateError('checkpoint value limit')


def bounded_strings(values, count, size):
    if (not isinstance(values, list) or len(values) > count
            or any(not isinstance(v, str) or not v or len(v.encode()) > size for v in values)
            or len(set(values)) != len(values)):
        raise StateError('checkpoint collection limit')
    return values


class StateStore:
    """Immutable list chunks precede mutable manifests; orphan chunks are harmless.

    The active record belongs to an account, never to a particular selected set.
    Per-task records carry that active epoch, so A→B→A cannot revive A's old state.
    """
    def __init__(self, account, selection, load, save, guard, *, dry_run=False):
        self.prefix = 'task-sync:v2:' + digest(account)
        self.load = load
        self.save = save
        self.guard = guard
        self.dry_run = dry_run
        self.key = self.prefix + ':active'
        current = self._read(self.key)
        if current is not None and (not isinstance(current, dict) or current.get('schema') != 2):
            raise StateError('invalid active checkpoint')
        self.changed = current is None or current.get('selection') != digest(selection)
        self.active = ({'schema': 2, 'selection': digest(selection), 'epoch': uuid.uuid4().hex,
                        'index': [], 'next_id': None} if self.changed else current)
        self.ids = self._read_list(self.active['index'], MAX_IDS, MAX_ID_BYTES)

    def _read(self, key):
        result = deepcopy(self.load(key))
        if result is not None:
            validate_record(key, result)
        return result

    def _write(self, key, value):
        validate_record(key, value)
        if not self.dry_run:
            self.guard()
            if self.save(key, deepcopy(value)) is False:
                raise StateError('checkpoint rejected')

    def _read_list(self, refs, count, size):
        bounded_strings(refs, 600, 64)
        result = []
        for ref in refs:
            chunk = self._read(self.prefix + ':chunk:' + ref)
            if not isinstance(chunk, list) or digest(chunk) != ref:
                raise StateError('invalid checkpoint chunk')
            result.extend(chunk)
        return bounded_strings(result, count, size)

    def _write_list(self, values, count, size):
        bounded_strings(values, count, size)
        refs, chunk, used = [], [], 2
        for value in values:
            cost = len(encoded(value)) + 1
            if used + cost > CHUNK_BYTES and chunk:
                refs.append(self._chunk(chunk))
                chunk, used = [], 2
            chunk.append(value)
            used += cost
        if chunk:
            refs.append(self._chunk(chunk))
        return refs

    def _chunk(self, chunk):
        ref = digest(chunk)
        key = self.prefix + ':chunk:' + ref
        existing = self._read(key)
        if existing is None:
            self._write(key, chunk)
        elif existing != chunk:
            raise StateError('checkpoint chunk mismatch')
        return ref

    def activate(self, ids):
        """Publish a complete tracking index before any task can be mutated."""
        refs = self._write_list(sorted(ids), MAX_IDS, MAX_ID_BYTES)
        if self.changed or refs != self.active['index']:
            self.active['index'] = refs
            self._write(self.key, self.active)
            self.changed = False

    def advance(self, next_id):
        self.active['next_id'] = next_id
        self._write(self.key, self.active)

    def _task_key(self, task_id):
        return self.prefix + ':task:' + digest(task_id)

    def task(self, task_id):
        value = self._read(self._task_key(task_id))
        if value is None:
            return None
        if not isinstance(value, dict) or value.get('schema') != 2:
            raise StateError('invalid task checkpoint')
        if value.get('epoch') != self.active['epoch']:
            return None
        value['seen'] = self._read_list(value['seen'], MAX_HISTORY, MAX_VERSION_BYTES)
        value['baselines'] = self._read_list(value['baselines'], MAX_HISTORY, 64)
        return value

    def save_task(self, task_id, value):
        value = deepcopy(value)
        # Validate both collections before emitting any pieces of the new record.
        bounded_strings(value['seen'], MAX_HISTORY, MAX_VERSION_BYTES)
        bounded_strings(value['baselines'], MAX_HISTORY, 64)
        value['seen'] = self._write_list(value['seen'], MAX_HISTORY, MAX_VERSION_BYTES)
        value['baselines'] = self._write_list(value['baselines'], MAX_HISTORY, 64)
        value.update(schema=2, epoch=self.active['epoch'])
        self._write(self._task_key(task_id), value)
