"""Completion-only reconciliation with explicit version IDs and durable intent."""
from contextlib import nullcontext
from dataclasses import dataclass, field
import hashlib
import time
import uuid

from .documents import DocumentError, parse, render, task_path
from .models import Task
from .state import StateStore, MAX_ID_BYTES, MAX_VERSION_BYTES, digest

MAX_TASKS = 10_000
MAX_VERSIONS = 500


class SyncConflict(RuntimeError):
    pass


class _Stopped(RuntimeError):
    pass


@dataclass
class SyncResult:
    counts: dict[str, int] = field(default_factory=lambda: {
        'imported': 0, 'updated': 0, 'completed': 0, 'unchanged': 0,
        'missing': 0, 'skipped': 0, 'blocked_recurring': 0, 'planned': 0})
    errors: list[str] = field(default_factory=list)
    remaining: int = 0
    partial: bool = False


def _bounded(iterable, maximum):
    result = []
    for value in iterable:
        if len(result) >= maximum:
            raise SyncConflict('snapshot cap')
        result.append(value)
    return result


def _valid_task(task):
    if not isinstance(task, Task):
        raise SyncConflict('invalid task')
    for value in (task.id, task.collection_id):
        if not isinstance(value, str) or not value or len(value.encode()) > MAX_ID_BYTES:
            raise SyncConflict('invalid identity')
    if (type(task.completed) is not bool or type(task.recurring) is not bool
            or not isinstance(task.revision, str)):
        raise SyncConflict('invalid task state')
    return task


def run_sync(provider, selected_ids, vault, load_state, save_state, *, dry_run=False,
             still_selected=None, deadline_s=600, selection_epoch="", mutation_scope=None):
    """See README for account scoping, callbacks, bounds and concurrency limits."""
    result = SyncResult()
    mutation_scope = mutation_scope or nullcontext
    selected = set(selected_ids)
    if not selected:
        return result
    deadline = time.monotonic() + min(max(float(deadline_s), 0), 600)
    if hasattr(vault, 'set_deadline'):
        vault.set_deadline(deadline)

    def guard(collection_id=None):
        if time.monotonic() >= deadline:
            raise _Stopped('deadline')
        if not dry_run and still_selected and set(still_selected()) != selected:
            raise _Stopped('selection changed')
        if collection_id is not None and collection_id not in selected:
            raise _Stopped('selection changed')

    try:
        guard()
        # Provider implementations must themselves bound blocking calls. Materialize
        # the entire snapshot before any source, file or checkpoint mutation.
        collections = _bounded(provider.collections(), MAX_TASKS)
        allowed = {c.id for c in collections if c.writable}
        if not selected <= allowed:
            raise SyncConflict('selected collection unavailable or read-only')
        tasks = _bounded(provider.tasks(selected), MAX_TASKS)
        snapshot = {}
        for task in tasks:
            _valid_task(task)
            if task.id in snapshot or task.collection_id not in selected:
                raise SyncConflict('invalid source snapshot')
            snapshot[task.id] = task
        namespace = getattr(provider, 'namespace', None)
        if not isinstance(namespace, str) or not namespace or not vault.namespace:
            raise SyncConflict('account identity missing')
        store = StateStore([vault.namespace, namespace, provider.name],
                           [sorted(selected), selection_epoch], load_state, save_state,
                           guard, dry_run=dry_run)
        ids = sorted(set(snapshot) | set(store.ids))
        if len(ids) > MAX_TASKS:
            raise SyncConflict('tracking index cap')
        if not dry_run:
            store.activate(ids)
    except _Stopped:
        result.partial, result.remaining = True, 1
        return result
    except Exception as exc:
        result.errors.append('snapshot:' + type(exc).__name__)
        result.partial, result.remaining = True, 1
        return result

    if store.active.get('next_id') in ids:
        cursor = ids.index(store.active['next_id'])
        ids = ids[cursor:] + ids[:cursor]
    def advance(index):
        guard()
        if not dry_run:
            store.advance(ids[(index + 1) % len(ids)])

    for index, task_id in enumerate(ids):
        try:
            guard()
            task = snapshot.get(task_id)
            if task is None:
                task = provider.get_task(task_id)
            if task is None:
                result.counts['missing'] += 1
                advance(index)
                continue
            _valid_task(task)
            if task.id != task_id:
                raise SyncConflict('source identity changed')
            if task.collection_id not in selected:
                result.counts['skipped'] += 1
                advance(index)
                continue
            guard(task.collection_id)
            previous = store.task(task_id)
            _sync_task(provider, vault, task, previous, store, guard, result, dry_run,
                       mutation_scope)
            guard(task.collection_id)
            advance(index)
        except _Stopped:
            result.remaining += len(ids) - index
            result.partial = True
            break
        except Exception as exc:
            result.errors.append(hashlib.sha256(task_id.encode()).hexdigest()[:12] + ':' + type(exc).__name__)
            result.remaining += 1
            result.partial = True
    return result


def _fingerprint(task):
    return {'source_revision': task.revision, 'source_due': task.due,
            'source_completed': task.completed, 'list_id': task.collection_id}


def _baseline(task):
    return {'completed': task.completed, 'recurring': task.recurring,
            'collection': digest(task.collection_id), 'due': digest(task.due),
            'revision': digest(task.revision), 'content': digest([task.title, task.notes])}


def _sync_task(provider, vault, task, previous, store, guard, result, dry_run, mutation_scope):
    path = task_path(provider.name, task.id)
    versions = _bounded(vault.versions(path), MAX_VERSIONS)
    if len(set(versions)) != len(versions) or any(not isinstance(v, str) or not v
            or len(v.encode()) > MAX_VERSION_BYTES for v in versions):
        raise SyncConflict('invalid versions')
    old_task = previous['baseline'] if previous else None
    generation = previous['generation'] if previous else uuid.uuid4().hex
    journal = previous.get('journal') if previous else None
    if (journal and not task.completed and digest(task.revision) != journal['revision']):
        # A lost completion response followed by another open revision may be
        # a reopen. Invalidate uncertain intent rather than complete it again.
        generation = uuid.uuid4().hex
    if (old_task and ((old_task['completed'] and not task.completed)
            or old_task['collection'] != digest(task.collection_id)
            or (task.recurring and old_task['due'] != digest(task.due)))):
        generation = uuid.uuid4().hex
    trusted = previous['baselines'] if previous else []
    if previous and generation != previous['generation']:
        trusted = []
    seen = set(previous.get('seen', [])) if previous else set()
    documents = {}
    # Inspect *all* unseen versions. A racing resolution may be older than the
    # latest own upload; timestamps and an own-write cursor cannot represent it.
    for version in versions:
        if version not in seen or version == versions[0]:
            guard(task.collection_id)
            text = vault.read_version(version)
            documents[version] = (text, parse(text, provider.name, task.id))
    latest = documents[versions[0]][0] if versions else None
    if previous and versions and documents[versions[0]][1].metadata['generation'] != previous['generation']:
        # Another active scope/host has superseded this local journal. Never
        # revive it merely because the source revision has not changed.
        generation, trusted, journal = uuid.uuid4().hex, [], None
    resolve = False
    for version, (_, doc) in documents.items():
        if version in seen:
            continue
        data = doc.metadata
        expected_collection = old_task['collection'] if old_task else digest(task.collection_id)
        if digest(data['list_id']) != expected_collection:
            raise DocumentError('collection identity mismatch')
        if previous and data['generation'] == generation:
            fingerprint = {k: data[k] for k in _fingerprint(task)}
            if digest(fingerprint) not in trusted:
                raise SyncConflict('document source baseline changed')
            if data['status'] == 'resolved' and not data['source_completed']:
                resolve = True
    if journal and journal['generation'] == generation:
        resolve = True
    if resolve and not task.completed:
        if task.recurring:
            result.counts['blocked_recurring'] += 1
            # Keep blocked requests unseen so each pass reports the unresolved
            # source action and preserves its visible Fulcra resolution. A source
            # occurrence change rotates generation and consumes them as stale.
            if old_task == _baseline(task):
                return
        else:
            if len(seen | set(versions)) >= MAX_VERSIONS or len(trusted) >= MAX_VERSIONS:
                raise SyncConflict('no checkpoint capacity for completion')
            fresh = provider.get_task(task.id)
            if fresh is None:
                raise SyncConflict('source disappeared')
            _valid_task(fresh)
            guard(fresh.collection_id)
            if (fresh.id != task.id or fresh.collection_id != task.collection_id
                    or fresh.revision != task.revision or fresh.recurring != task.recurring
                    or fresh.due != task.due or fresh.completed != task.completed):
                raise SyncConflict('source changed before completion')
            if dry_run:
                result.counts['planned'] += 1
                return
            record = {
                'baseline': old_task, 'generation': generation, 'seen': sorted(seen),
                'baselines': trusted,
                'journal': {'generation': generation, 'revision': digest(fresh.revision),
                            'collection': digest(fresh.collection_id)}}
            guard(fresh.collection_id)
            store.save_task(task.id, record)  # Durable intent before source completion.
            with mutation_scope():
                guard(fresh.collection_id)
                provider.complete(fresh.id, fresh.revision, fresh.collection_id)
            confirmed = provider.get_task(fresh.id)
            if (confirmed is None or not confirmed.completed or confirmed.id != fresh.id
                    or confirmed.collection_id != fresh.collection_id):
                raise SyncConflict('completion unconfirmed')
            task = _valid_task(confirmed)
            result.counts['completed'] += 1
    rendered = render(provider.name, task, generation, existing=latest)
    if rendered != latest:
        if dry_run:
            result.counts['planned'] += 1
            return
        if len(seen | set(versions)) >= MAX_VERSIONS:
            raise SyncConflict('no checkpoint capacity for upload')
        fingerprint = digest(_fingerprint(task))
        if fingerprint not in trusted:
            trusted = trusted + [fingerprint]
        if len(trusted) > MAX_VERSIONS:
            raise SyncConflict('published baseline cap')
        # Persist the exact planned baseline before upload; a lost upload reply
        # must not turn our own real version into an unrecognized source edit.
        record = {'baseline': _baseline(task), 'generation': generation,
                                  'seen': sorted(seen), 'baselines': trusted, 'journal': None}
        guard(task.collection_id)
        store.save_task(task.id, record)
        with mutation_scope():
            guard(task.collection_id)
            version = vault.write(path, rendered)
        if not isinstance(version, str) or not version or len(version.encode()) > MAX_VERSION_BYTES:
            raise SyncConflict('upload unconfirmed')
        # Only the exact returned version is observed; a concurrent bot version
        # remains unseen and will be processed on the next run.
        seen.add(version)
        result.counts['updated' if previous else 'imported'] += 1
    else:
        result.counts['unchanged'] += 1
    if not dry_run:
        record = {'baseline': _baseline(task), 'generation': generation,
                                  'seen': sorted(seen | set(versions)),
                                  'baselines': trusted, 'journal': None}

        store.save_task(task.id, record)
