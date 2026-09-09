from dataclasses import FrozenInstanceError
import pytest


def test_models_are_immutable_and_have_safe_defaults():
    from fulcra_task_sync.models import Collection, Task
    task = Task('task-a', 'list-a', 'Synthetic task', '', False)
    assert task.due is None and task.revision == '' and not task.recurring
    assert Collection('list-a', 'Synthetic list').writable
    with pytest.raises(FrozenInstanceError):
        task.completed = True
