"""Deterministic Markdown with explicit unsupported-content markers."""
import re

from .client import NotionError, plain_text


def _escape(value):
    # Preserve text rather than injecting its link/HTML syntax into Markdown.
    return re.sub(r'([\\`*_{}\[\]<>#|])', r'\\\1', value)


def _block(item, depth):
    kind = item['type']
    if item.get('archived') or item.get('in_trash'):
        return '[Archived block omitted.]'
    data = item.get(kind)
    if not isinstance(data, dict):
        raise NotionError('Invalid Notion block content.')
    if kind in {'column_list', 'column', 'table'}:
        body = ''
    elif kind == 'table_row':
        cells = data.get('cells')
        if not isinstance(cells, list):
            raise NotionError('Invalid Notion table row.')
        body = '| ' + ' | '.join(_escape(plain_text(cell)) for cell in cells) + ' |'
    elif kind == 'divider':
        body = '---'
    elif kind == 'code':
        raw = plain_text(data.get('rich_text'))
        fence = '`' * max(3, max((len(v) for v in re.findall(r'`+', raw)), default=0) + 1)
        body = fence + '\n' + raw + '\n' + fence
    elif kind in {'paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item',
                  'numbered_list_item', 'to_do', 'toggle', 'quote', 'callout'}:
        content = _escape(plain_text(data.get('rich_text')))
        prefix = {'heading_1': '# ', 'heading_2': '## ', 'heading_3': '### ',
                  'bulleted_list_item': '- ', 'numbered_list_item': '1. ', 'quote': '> ',
                  'callout': '> ', 'toggle': '- '}.get(kind, '')
        if kind == 'to_do':
            if type(data.get('checked')) is not bool:
                raise NotionError('Invalid Notion checkbox state.')
            prefix = '- [x] ' if data.get('checked') is True else '- [ ] '
        body = prefix + content
    else:
        body = '[Unsupported or linked content omitted: ' + _escape(kind[:80]) + '.]'
    nested = '\n\n'.join(_block(child, depth + 1) for child in item.get('_children', []))
    if nested:
        body += '\n\n' + '\n'.join('  ' + line for line in nested.splitlines())
    return body


def render_snapshot(snapshot, *, max_bytes=2 * 1024 * 1024):
    content = ('# ' + _escape(snapshot.title.replace('\n', ' ')) + '\n\n'
               + 'Source: https://app.notion.com/p/' + snapshot.page_id + '\n\n'
               + '\n\n'.join(_block(item, 0) for item in snapshot.blocks) + '\n')
    if len(content.encode('utf-8')) > max_bytes:
        raise NotionError('Selected Notion page exceeds the rendered content limit.')
    return content
