"""All fixtures are invented; no source or account was contacted."""
from copy import deepcopy
from dataclasses import replace

import pytest

from fulcra_task_sync.models import Collection, Task
from fulcra_task_sync.documents import parse, task_path


class Provider:
    name = 'todoist'
    namespace = 'synthetic-source-account'

    def __init__(self):
        self.items = {'task-a': Task('task-a', 'list-a', 'Synthetic task', '', False, revision='r1')}
        self.writes = 0
        self.lost_response = False
        self.fail = False

    def collections(self):
        return [Collection('list-a', 'Synthetic list')]

    def tasks(self, selected_ids):
        yield from [t for t in self.items.values() if t.collection_id in selected_ids]
        if self.fail:
            raise RuntimeError('synthetic private detail never reported')

    def get_task(self, task_id):
        return self.items.get(task_id)

    def complete(self, task_id, expected_revision, collection_id):
        task = self.items[task_id]
        assert task.revision == expected_revision and task.collection_id == collection_id
        self.items[task_id] = replace(task, completed=True, revision='r2')
        self.writes += 1
        if self.lost_response:
            self.lost_response = False
            raise TimeoutError('synthetic lost reply')
        return self.items[task_id]


class Vault:
    namespace = 'synthetic-fulcra-account'

    def __init__(self):
        self.files = {}
        self.bodies = {}
        self.fail_write = False
        self.racing = None

    def versions(self, path):
        return list(self.files.get(path, []))

    def read_version(self, version):
        return self.bodies[version]

    def write(self, path, text):
        if self.fail_write:
            raise TimeoutError('synthetic upload failure')
        if self.racing:
            racing, self.racing = self.racing, None
            self.write(path, racing)
        version = f'v{len(self.bodies) + 1}'
        self.bodies[version] = text
        self.files.setdefault(path, []).insert(0, version)
        return version

    def latest(self):
        return self.bodies[self.files[task_path('todoist', 'task-a')][0]]

    def resolve(self):
        self.write(task_path('todoist', 'task-a'), self.latest().replace('status: open', 'status: resolved'))


@pytest.fixture
def rig():
    from fulcra_task_sync.engine import run_sync
    provider, vault, state = Provider(), Vault(), {}
    def run(**kwargs):
        return run_sync(provider, {'list-a'}, vault, lambda k: deepcopy(state.get(k)),
                        lambda k, v: state.__setitem__(k, deepcopy(v)), **kwargs)
    return provider, vault, state, run


def test_import_source_completion_and_idempotent_repeat(rig):
    p, v, s, run = rig
    assert not run().errors
    assert parse(v.latest(), p.name, 'task-a').metadata['status'] == 'open'
    total = len(v.bodies)
    assert not run().errors and len(v.bodies) == total
    p.items['task-a'] = replace(p.items['task-a'], completed=True, revision='r2')
    assert not run().errors
    assert parse(v.latest(), p.name, 'task-a').metadata['status'] == 'resolved'


def test_bot_resolution_completes_exact_source_and_retains_annotations(rig):
    p, v, s, run = rig
    run()
    v.write(task_path(p.name, 'task-a'), v.latest().replace('status: open', 'status: resolved') + '\nKeep annotation\n')
    assert not run().errors
    assert p.items['task-a'].completed and p.writes == 1
    assert v.latest().endswith('\nKeep annotation\n')
    run()
    assert p.writes == 1


def test_racing_resolved_version_survives_later_import(rig):
    p, v, s, run = rig
    run()
    v.racing = v.latest().replace('status: open', 'status: resolved')
    p.items['task-a'] = replace(p.items['task-a'], title='Updated synthetic title')
    run()
    assert not p.items['task-a'].completed
    run()
    assert p.items['task-a'].completed


def test_source_reopen_rotates_generation_and_rejects_stale_resolve(rig):
    p, v, s, run = rig
    run()
    v.resolve()
    run()
    old = v.latest()
    p.items['task-a'] = replace(p.items['task-a'], completed=False, revision='r3')
    run()
    new_generation = parse(v.latest(), p.name, 'task-a').metadata['generation']
    assert new_generation != parse(old, p.name, 'task-a').metadata['generation']
    v.write(task_path(p.name, 'task-a'), old)
    run()
    assert not p.items['task-a'].completed and p.writes == 1


def test_uncertain_completion_rechecks_and_does_not_repeat_write(rig):
    p, v, s, run = rig
    run()
    v.resolve()
    p.lost_response = True
    assert run().errors
    assert any(record.get('journal') for record in s.values() if isinstance(record, dict))
    assert not run().errors
    assert p.writes == 1 and 'status: resolved' in v.latest()


def test_failed_vault_upload_retries_without_repeating_source_completion(rig):
    p, v, s, run = rig
    run()
    v.resolve()
    v.fail_write = True
    assert run().errors
    v.fail_write = False
    assert not run().errors and p.writes == 1


def test_partial_snapshot_authorizes_no_changes(rig):
    p, v, s, run = rig
    p.fail = True
    result = run()
    assert result.errors and not v.bodies and not s and not p.writes
    assert 'private detail' not in str(result.errors)


def test_deselection_and_dry_run_never_write_or_checkpoint(rig):
    p, v, s, run = rig
    run(dry_run=True)
    assert not v.bodies and not s
    run()
    v.resolve()
    before = deepcopy(s), len(v.bodies)
    run(still_selected=lambda: set())
    assert (s, len(v.bodies)) == before and p.writes == 0
    run(dry_run=True)
    assert (s, len(v.bodies)) == before and p.writes == 0


def test_missing_source_never_means_completion(rig):
    p, v, s, run = rig
    run()
    p.items.clear()
    run()
    assert 'status: open' in v.latest() and p.writes == 0


def test_recurring_resolution_is_blocked(rig):
    p, v, s, run = rig
    p.items['task-a'] = replace(p.items['task-a'], recurring=True, due='2026-01-01')
    run()
    v.resolve()
    result = run()
    assert result.counts['blocked_recurring'] == 1 and p.writes == 0
    old = v.latest()
    p.items['task-a'] = replace(p.items['task-a'], due='2026-01-02', revision='r2')
    run()
    assert parse(old, p.name, 'task-a').metadata['generation'] != parse(v.latest(), p.name, 'task-a').metadata['generation']


@pytest.mark.parametrize('mutation', ['source_id: task-a', 'list_id: list-a', 'source_revision: r1'])
def test_forged_identity_or_revision_cannot_complete_source(rig, mutation):
    p, v, s, run = rig
    run()
    v.write(task_path(p.name, 'task-a'), v.latest().replace(mutation, mutation.split(':')[0] + ': foreign').replace('status: open', 'status: resolved'))
    result = run()
    assert result.errors and p.writes == 0


def test_deadline_can_resume_without_mutations(rig):
    p, v, s, run = rig
    result = run(deadline_s=0)
    assert result.partial and not v.bodies and not s
    assert not run().errors and v.bodies


def test_account_switch_cannot_reuse_old_completion_journal(rig):
    p, v, s, run = rig
    run()
    v.resolve()
    p.namespace = 'another-synthetic-account'
    run()
    assert p.writes == 0


def test_blocked_recurring_resolution_stays_visible_on_repeated_runs(rig):
    p, v, s, run = rig
    p.items['task-a'] = replace(p.items['task-a'], recurring=True)
    run()
    v.resolve()
    for _ in range(3):
        assert run().counts['blocked_recurring'] == 1
        assert 'status: resolved' in v.latest()
    assert p.writes == 0


def test_deselection_after_journal_prevents_source_write(rig):
    p, v, s, run = rig
    run()
    v.resolve()
    def selected():
        pending = any(record.get('journal') for record in s.values() if isinstance(record, dict))
        return set() if pending else {'list-a'}
    result = run(still_selected=selected)
    assert result.partial and p.writes == 0


def test_source_changed_after_snapshot_is_rejected(rig):
    p, v, s, run = rig
    run()
    v.resolve()
    p.get_task = lambda _: replace(p.items['task-a'], revision='r-new')
    assert run().errors and p.writes == 0


def test_unreadable_history_does_not_overwrite_or_complete(rig):
    p, v, s, run = rig
    run()
    v.resolve()
    previous = deepcopy(s), len(v.bodies)
    v.read_version = lambda _: (_ for _ in ()).throw(TimeoutError('synthetic failure'))
    assert run().errors
    assert (s, len(v.bodies)) == previous and p.writes == 0


def test_false_checkpoint_prevents_completion(rig):
    from fulcra_task_sync.engine import run_sync
    p, v, s, run = rig
    run()
    v.resolve()
    result = run_sync(p, {'list-a'}, v, lambda k: deepcopy(s[k]), lambda k, d: False)
    assert result.errors and p.writes == 0


def test_racing_resolution_from_previous_published_revision_is_still_actionable(rig):
    p, v, s, run = rig
    run()
    v.racing = v.latest().replace('status: open', 'status: resolved')
    p.items['task-a'] = replace(p.items['task-a'], title='Updated synthetic title', revision='r-next')
    assert not run().errors
    assert not p.items['task-a'].completed
    assert not run().errors
    assert p.items['task-a'].completed


def test_forged_source_completed_baseline_is_rejected(rig):
    p, v, s, run = rig
    run()
    v.write(task_path(p.name, 'task-a'), v.latest().replace('source_completed: false', 'source_completed: true').replace('status: open', 'status: resolved'))
    assert run().errors and p.writes == 0


def test_uncertain_completion_cannot_complete_a_reopened_source(rig):
    p, v, s, run = rig
    run()
    v.resolve()
    p.lost_response = True
    assert run().errors
    p.items['task-a'] = replace(p.items['task-a'], completed=False, revision='reopened')
    assert not run().errors
    assert p.writes == 1 and not p.items['task-a'].completed
    assert 'status: open' in v.latest()


def test_lost_upload_reply_recovers_published_baseline(rig):
    p, v, s, run = rig
    run()
    p.items['task-a'] = replace(p.items['task-a'], revision='r-new', title='Updated synthetic task')
    write = v.write
    def lost_upload(path, body):
        write(path, body)
        raise TimeoutError('synthetic lost upload reply')
    v.write = lost_upload
    assert run().errors
    v.write = write
    assert not run().errors
    assert 'Updated synthetic task' in v.latest() and p.writes == 0


def test_task_moved_out_of_selection_does_not_starve_other_selected_tasks(rig):
    p, v, s, run = rig
    run()
    p.items['task-a'] = replace(p.items['task-a'], collection_id='list-unselected')
    p.items['task-b'] = Task('task-b', 'list-a', 'Another synthetic task', '', False, revision='r1')
    result = run()
    assert not result.partial and not result.errors
    assert task_path(p.name, 'task-b') in v.files
    assert p.writes == 0


def test_deadline_continuation_advances_past_already_processed_tasks(rig, monkeypatch):
    import fulcra_task_sync.engine as engine
    p, v, s, run = rig
    p.items['task-b'] = Task('task-b', 'list-a', 'Another synthetic task', '', False, revision='r1')
    now = [0]
    monkeypatch.setattr(engine.time, 'monotonic', lambda: now[0])
    def selected():
        if any(record.get('next_id') == 'task-b' for record in s.values() if isinstance(record, dict)):
            now[0] = 10
        return {'list-a'}
    result = run(deadline_s=1, still_selected=selected)
    assert result.partial and task_path(p.name, 'task-b') not in v.files
    now[0] = 0
    assert not run().errors
    assert task_path(p.name, 'task-b') in v.files


def collect_kv(state):
    """Validate using the production Collect JSON/key limits, not a permissive fake."""
    from fulcra_collect.db import _encode_plugin_kv_value, _validate_plugin_kv_key
    def save(key, value):
        _validate_plugin_kv_key(key)
        _encode_plugin_kv_value(value)
        state[key] = deepcopy(value)
    return lambda key: deepcopy(state.get(key)), save


def test_two_hundred_tasks_fit_real_collect_kv_and_repeat(rig):
    from fulcra_task_sync.engine import run_sync
    p, v, state, _ = rig
    p.items = {f'task-{n:03}': Task(f'task-{n:03}', 'list-a', 'Synthetic task', '', False,
                                  revision='r1') for n in range(200)}
    load, save = collect_kv(state)
    result = run_sync(p, {'list-a'}, v, load, save)
    assert not result.errors and len(v.files) == 200
    assert not run_sync(p, {'list-a'}, v, load, save).errors


def test_large_notes_never_enter_checkpoint(rig):
    from fulcra_task_sync.engine import run_sync
    p, v, state, _ = rig
    p.items['task-a'] = replace(p.items['task-a'], notes='Synthetic long note. ' * 20000)
    load, save = collect_kv(state)
    assert not run_sync(p, {'list-a'}, v, load, save).errors
    assert 'Synthetic long note.' in v.latest()
    assert 'Synthetic long note.' not in str(state)


@pytest.mark.parametrize('middle', [{'list-a', 'list-b'}, {'list-b'}])
def test_selection_roundtrip_cannot_revive_uncertain_old_journal(rig, middle):
    from fulcra_task_sync.engine import run_sync
    p, v, state, run = rig
    p.collections = lambda: [Collection('list-a', 'Synthetic A'), Collection('list-b', 'Synthetic B')]
    run()
    v.resolve()
    complete = p.complete
    p.complete = lambda *args: (_ for _ in ()).throw(TimeoutError('synthetic before write'))
    assert run().errors
    p.complete = complete
    load, save = collect_kv(state)
    assert not run_sync(p, middle, v, load, save).errors
    assert not run().errors
    assert not p.items['task-a'].completed and p.writes == 0


def test_changed_latest_generation_cannot_revive_local_journal(rig):
    from fulcra_task_sync.documents import render
    p, v, state, run = rig
    run()
    v.resolve()
    complete = p.complete
    p.complete = lambda *args: (_ for _ in ()).throw(TimeoutError('synthetic before write'))
    assert run().errors
    p.complete = complete
    v.write(task_path(p.name, 'task-a'), render(p.name, p.items['task-a'], 'another-generation'))
    assert not run().errors
    assert not p.items['task-a'].completed and p.writes == 0


def test_configuration_epoch_invalidates_journal_without_intermediate_sync(rig):
    p, v, state, run = rig
    run(selection_epoch='settings-before-disable')
    v.resolve()
    complete = p.complete
    p.complete = lambda *args: (_ for _ in ()).throw(TimeoutError('synthetic before write'))
    assert run(selection_epoch='settings-before-disable').errors
    p.complete = complete
    assert not run(selection_epoch='settings-after-reenable').errors
    assert not p.items['task-a'].completed and p.writes == 0


def test_preview_selection_epoch_does_not_change_active_state(rig):
    p, v, state, run = rig
    run(selection_epoch='settings-a')
    before = deepcopy(state), len(v.bodies)
    assert not run(selection_epoch='settings-preview', dry_run=True).errors
    assert (state, len(v.bodies)) == before


def test_empty_selection_performs_no_provider_vault_or_state_io(rig):
    from fulcra_task_sync.engine import run_sync
    p, v, _, _ = rig
    def forbidden(*args):
        raise AssertionError('unexpected I/O')
    p.collections = p.tasks = forbidden
    v.versions = v.read_version = v.write = forbidden
    result = run_sync(p, set(), v, forbidden, forbidden, selection_epoch='settings-empty')
    assert not result.errors and not result.partial


def test_full_version_budget_blocks_completion_before_mutating_source(rig):
    p, v, state, run = rig
    run()
    path = task_path(p.name, 'task-a')
    for _ in range(498):
        v.write(path, v.latest())
    v.resolve()
    assert len(v.versions(path)) == 500
    result = run()
    assert result.errors and p.writes == 0 and not p.items['task-a'].completed


@pytest.mark.parametrize('disposition', ['missing', 'moved'])
def test_slow_missing_or_moved_tasks_advance_cursor_before_budget_expires(rig, monkeypatch, disposition):
    import fulcra_task_sync.engine as engine
    p, v, _, run = rig
    p.items = {f'task-{n}': Task(f'task-{n}', 'list-a', 'Synthetic task', '', False,
                                revision='r1') for n in range(3)}
    assert not run().errors
    for task_id in ('task-0', 'task-1'):
        if disposition == 'missing':
            del p.items[task_id]
        else:
            p.items[task_id] = replace(p.items[task_id], collection_id='list-unselected')
    p.items['task-2'] = replace(p.items['task-2'], title='Updated later task', revision='r2')
    now = [0.0]
    monkeypatch.setattr(engine.time, 'monotonic', lambda: now[0])
    get_task = p.get_task
    def slow_lookup(task_id):
        now[0] += 0.6
        return get_task(task_id)
    p.get_task = slow_lookup
    for _ in range(3):
        now[0] = 0.0
        result = run(deadline_s=1)
        assert not result.errors
    path = task_path(p.name, 'task-2')
    assert 'Updated later task' in v.read_version(v.versions(path)[0])


@pytest.mark.parametrize('stop', ['deadline', 'deselection'])
def test_missing_lookup_does_not_checkpoint_after_deadline_or_deselection(rig, monkeypatch, stop):
    import fulcra_task_sync.engine as engine
    p, v, state, run = rig
    run()
    before = deepcopy(state)
    p.items.clear()
    now, live = [0.0], [{'list-a'}]
    monkeypatch.setattr(engine.time, 'monotonic', lambda: now[0])
    def missing(_):
        if stop == 'deadline':
            now[0] = 2.0
        else:
            live[0] = set()
        return None
    p.get_task = missing
    result = run(deadline_s=1, still_selected=lambda: live[0])
    assert result.partial and not result.errors
    assert state == before


@pytest.mark.parametrize('operation', ['complete', 'upload'])
def test_consent_is_rechecked_inside_mutation_scope(rig, operation):
    from contextlib import contextmanager

    p, v, _, run = rig
    if operation == 'complete':
        run()
        v.resolve()
    live = [{'list-a'}]
    @contextmanager
    def revoked_before_acquisition():
        # The account transition wins after the last ordinary guard but before
        # the writer can acquire its lease. It must not use that stale guard.
        live[0] = set()
        yield
    before = len(v.bodies)
    result = run(still_selected=lambda: live[0], mutation_scope=revoked_before_acquisition)
    assert result.partial and not result.errors
    assert p.writes == 0 and not p.items['task-a'].completed
    assert len(v.bodies) == before


@pytest.mark.parametrize('operation', ['complete', 'upload'])
def test_actual_external_mutation_holds_lease_until_call_returns(rig, tmp_path, monkeypatch, operation):
    import threading
    from fulcra_collect import config
    from fulcra_collect.config_leases import task_mutation_scope

    p, v, _, run = rig
    monkeypatch.setenv('FULCRA_COLLECT_HOME', str(tmp_path / 'synthetic-config'))
    cfg = config.load()
    cfg.enable(p.name)
    config.save(cfg)
    captured_epoch = config.load().plugin_epochs[p.name]
    if operation == 'complete':
        run()
        v.resolve()
    attempted, finished = threading.Event(), threading.Event()
    failures, order = [], []
    def revoke():
        try:
            attempted.set()
            with config.fulcra_account_transition():
                order.append('transition')
            finished.set()
        except Exception as exc:
            failures.append(exc)
    worker = threading.Thread(target=revoke)
    def selection():
        current = config.load()
        return {'list-a'} if (not current.account_transition and
                             current.plugin_epochs[p.name] == captured_epoch) else set()
    real_mutation = p.complete if operation == 'complete' else v.write
    def paused_mutation(*args):
        worker.start()
        assert attempted.wait(5)
        assert not finished.wait(0.2)
        value = real_mutation(*args)
        order.append('mutation')
        return value
    if operation == 'complete':
        p.complete = paused_mutation
    else:
        v.write = paused_mutation
    try:
        result = run(mutation_scope=task_mutation_scope, still_selected=selection)
        assert not result.errors
        assert finished.wait(5)
        assert not failures
        assert order == ['mutation', 'transition']
        assert (p.writes == 1) if operation == 'complete' else bool(v.files)
    finally:
        if worker.ident is not None:
            worker.join(5)
    assert not worker.is_alive()
