"""Strict task frontmatter and one source-owned body region."""
from dataclasses import dataclass
import hashlib
import html
import re

import yaml
from yaml.tokens import AliasToken, AnchorToken

from .models import Task

BEGIN = '<!-- collect-task:source:begin -->'
END = '<!-- collect-task:source:end -->'
MAX_BYTES = 1_000_000


class DocumentError(ValueError):
    """A file cannot safely authorize a source operation."""


class StrictLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise DocumentError('duplicate or non-string YAML key')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class Document:
    metadata: dict
    before: str
    after: str


def task_path(provider: str, task_id: str) -> str:
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', provider):
        raise DocumentError('invalid provider')
    return f'vault/tasks/{provider}/{hashlib.sha256(task_id.encode()).hexdigest()}.md'


def parse(text: str, provider: str, task_id: str) -> Document:
    try:
        if not isinstance(text, str) or len(text.encode()) > MAX_BYTES:
            raise DocumentError('oversized document')
        if not text.startswith('---\n') or '\n---\n' not in text[4:]:
            raise DocumentError('frontmatter missing')
        header, body = text[4:].split('\n---\n', 1)
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(header)):
            raise DocumentError('YAML aliases not supported')
        data = yaml.load(header, Loader=StrictLoader)
        if not isinstance(data, dict):
            raise DocumentError('invalid frontmatter')
        expected = {'type': 'Task', 'sync_schema': 'collect-task/v1',
                    'source': provider, 'source_id': task_id}
        if any(data.get(key) != value for key, value in expected.items()):
            raise DocumentError('identity mismatch')
        for key in ('source_id', 'list_id', 'generation', 'source_revision'):
            if not isinstance(data.get(key), str) or (key != 'source_revision' and not data[key]):
                raise DocumentError('missing identity or generation')
        if data.get('status') not in ('open', 'resolved'):
            raise DocumentError('invalid status')
        if type(data.get('source_completed')) is not bool or 'source_due' not in data:
            raise DocumentError('invalid source baseline')
        if data['source_due'] is not None and not isinstance(data['source_due'], str):
            raise DocumentError('invalid due')
        if body.count(BEGIN) != 1 or body.count(END) != 1:
            raise DocumentError('invalid ownership fence')
        before, rest = body.split(BEGIN)
        _, after = rest.split(END)
        return Document(data, before, after)
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise DocumentError('invalid task document') from exc


def render(provider: str, task: Task, generation: str, *, existing: str | None = None) -> str:
    old = parse(existing, provider, task.id) if existing is not None else None
    data = dict(old.metadata) if old else {}
    data.update(type='Task', sync_schema='collect-task/v1', source=provider,
                source_id=task.id, list_id=task.collection_id, generation=generation,
                status='resolved' if task.completed else 'open', source_completed=task.completed,
                source_due=task.due, source_revision=task.revision)
    content = f'\n# {html.escape(task.title)}\n\n{html.escape(task.notes)}\n'
    before, after = (old.before, old.after) if old else ('\n', '\n')
    return '---\n' + yaml.safe_dump(data, sort_keys=False, allow_unicode=True) + '---\n' + before + BEGIN + content + END + after
