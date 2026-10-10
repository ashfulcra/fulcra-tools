import pytest

from fulcra_notion.client import PageSnapshot, NotionError
from fulcra_notion.render import render_snapshot

PAGE = '00000000-0000-4000-8000-000000000001'


def block(kind, **data):
    return {'type': kind, kind: data}


def rich(value):
    return [{'plain_text': value}]


def test_plain_markdown_structure_checkboxes_code_and_table():
    snapshot = PageSnapshot(PAGE, 'Synthetic <title>', (
        block('heading_2', rich_text=rich('Agenda')),
        block('to_do', rich_text=rich('Discuss'), checked=True),
        block('quote', rich_text=rich('Comment')),
        block('code', rich_text=rich('```\nexample')),
        block('table_row', cells=[rich('Column | A'), rich('Column B')]),
    ))
    content = render_snapshot(snapshot)
    assert render_snapshot(snapshot) == content
    assert '## Agenda' in content and '- [x] Discuss' in content
    assert '> Comment' in content and '````\n```\nexample\n````' in content
    assert '| Column \\| A | Column B |' in content
    assert '<title>' not in content


def test_attachment_fields_and_provider_metadata_are_not_exported():
    content = render_snapshot(PageSnapshot(PAGE, 'Synthetic', (
        block('image', file={'url': 'https://example.invalid/?secret=synthetic'}),
        block('child_page', title='Unselected private page'),
    )))
    assert 'omitted' in content.lower()
    assert 'secret=synthetic' not in content and 'Unselected private page' not in content


def test_render_limit_never_returns_a_truncated_document():
    with pytest.raises(NotionError, match='rendered'):
        render_snapshot(PageSnapshot(PAGE, 'Synthetic', ()), max_bytes=5)
