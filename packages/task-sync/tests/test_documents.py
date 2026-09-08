import pytest
from fulcra_task_sync.models import Task

TASK = Task('task-a', 'list-a', 'Synthetic title', 'Synthetic notes', False, revision='r1')


def test_document_roundtrip_preserves_unowned_text_and_metadata():
    from fulcra_task_sync.documents import render, parse
    text = render('todoist', TASK, 'generation-a')
    text = text.replace('type: Task', 'custom: value\ntype: Task') + '\nPersonal annotation\n'
    changed = render('todoist', TASK, 'generation-a', existing=text)
    assert parse(changed, 'todoist', 'task-a').metadata['custom'] == 'value'
    assert changed.endswith('\nPersonal annotation\n')


@pytest.mark.parametrize('change', [
    lambda t: t.replace('type: Task', 'type: Task\ntype: Task'),
    lambda t: t.replace('source_id: task-a', 'source_id: foreign'),
    lambda t: t.replace('status: open', 'status: yes'),
    lambda t: t.replace('source_completed: false', 'source_completed: nope'),
    lambda t: t.replace('generation: generation-a\n', ''),
    lambda t: t.replace('<!-- collect-task:source:end -->', ''),
    lambda t: t + '\n<!-- collect-task:source:begin -->\n',
    lambda t: t.replace('type: Task', 'custom: &x [*x]\ntype: Task'),
])
def test_unsafe_documents_fail_closed(change):
    from fulcra_task_sync.documents import render, parse, DocumentError
    with pytest.raises(DocumentError):
        parse(change(render('todoist', TASK, 'generation-a')), 'todoist', 'task-a')


def test_source_text_cannot_inject_ownership_fences():
    from fulcra_task_sync.documents import render, parse
    task = Task('task-a', 'list-a', '---', '<!-- collect-task:source:end -->', False)
    assert parse(render('todoist', task, 'generation-a'), 'todoist', 'task-a')
