"""Exercise the actual Collect KV encoding boundary with synthetic IDs."""
from copy import deepcopy

import pytest

from fulcra_collect.db import _encode_plugin_kv_value, _validate_plugin_kv_key
from fulcra_task_sync.state import StateStore, StateError, digest


def storage():
    data = {}
    def save(key, value):
        _validate_plugin_kv_key(key)
        _encode_plugin_kv_value(value)
        data[key] = deepcopy(value)
    return data, lambda key: deepcopy(data.get(key)), save


def test_full_index_and_history_shards_fit_real_collect_kv():
    data, load, save = storage()
    store = StateStore(['synthetic-account'], ['synthetic-selection'], load, save, lambda: None)
    ids = [f'{n:05}-' + 'x' * 1018 for n in range(10000)]
    store.activate(ids)
    seen = [f'{n:03}-' + 'v' * 508 for n in range(500)]
    baselines = [digest(n) for n in range(500)]
    store.save_task(ids[0], {'baseline': {'completed': False}, 'generation': 'synthetic-generation',
                            'seen': seen, 'baselines': baselines, 'journal': None})
    resumed = StateStore(['synthetic-account'], ['synthetic-selection'], load, save, lambda: None)
    assert resumed.ids == ids
    assert resumed.task(ids[0])['seen'] == seen
    assert resumed.task(ids[0])['baselines'] == baselines
    assert max(len(_encode_plugin_kv_value(value).encode()) for value in data.values()) < 65536
    assert max(len(key.encode()) for key in data) < 256


@pytest.mark.parametrize('field,value', [
    ('seen', [f'v{n}' for n in range(501)]),
    ('seen', ['v' * 513]),
    ('baselines', [digest(n) for n in range(501)]),
])
def test_oversized_task_history_is_rejected_before_any_checkpoint_write(field, value):
    data, load, save = storage()
    store = StateStore(['synthetic-account'], ['synthetic-selection'], load, save, lambda: None)
    store.activate(['task-a'])
    before = deepcopy(data)
    record = {'baseline': {}, 'generation': 'synthetic-generation', 'seen': [],
              'baselines': [], 'journal': None, field: value}
    with pytest.raises(StateError):
        store.save_task('task-a', record)
    assert data == before


def test_failed_manifest_write_cannot_expose_partial_task_state():
    data, load, save = storage()
    store = StateStore(['synthetic-account'], ['synthetic-selection'], load, save, lambda: None)
    store.activate(['task-a'])
    old = {'baseline': {}, 'generation': 'old-generation', 'seen': ['v-old'],
           'baselines': [], 'journal': None}
    store.save_task('task-a', old)
    def fail_manifest(key, value):
        if ':task:' in key:
            raise TimeoutError('synthetic failed commit')
        save(key, value)
    store.save = fail_manifest
    with pytest.raises(TimeoutError):
        store.save_task('task-a', {**old, 'generation': 'new-generation', 'seen': ['v-new']})
    resumed = StateStore(['synthetic-account'], ['synthetic-selection'], load, save, lambda: None)
    assert resumed.task('task-a')['generation'] == 'old-generation'
    assert resumed.task('task-a')['seen'] == ['v-old']
