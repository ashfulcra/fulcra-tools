"""Bounded fixed-origin reads of explicitly selected Notion pages."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse
from uuid import UUID

import httpx


class NotionError(RuntimeError):
    pass


def _id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise NotionError('Invalid Notion record identifier.') from None


def _normalize_page_ids(value) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip() or len(value) > 16_384:
        raise NotionError('Paste one or more Notion page links, one per line.')
    selected = []
    for line in value.splitlines():
        line = line.strip()
        if not line:
            continue
        if '://' in line:
            parsed = urlparse(line)
            host = parsed.hostname or ''
            if (parsed.scheme != 'https' or parsed.username or parsed.password
                    or parsed.port not in (None, 443)
                    or not (host in {'notion.so', 'www.notion.so', 'notion.com',
                                     'www.notion.com', 'app.notion.com'}
                            or host.endswith('.notion.site'))):
                raise NotionError('Use a Notion page link or its page ID.')
            match = re.search(r'([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}'
                              r'-[0-9a-fA-F]{12})$', parsed.path.rstrip('/'))
            if not match:
                raise NotionError('This Notion link does not contain a page ID.')
            line = match.group(1)
        identifier = _id(line)
        if identifier not in selected:
            selected.append(identifier)
        if len(selected) > 50:
            raise NotionError('Choose at most 50 Notion pages.')
    if not selected:
        raise NotionError('Choose at least one Notion page.')
    return tuple(selected)


def normalize_page_ids(value) -> tuple[str, ...]:
    try:
        return _normalize_page_ids(value)
    except ValueError:
        raise NotionError('Use a valid Notion page link or its page ID.') from None


@dataclass(frozen=True)
class Limits:
    max_response_bytes: int = 2 * 1024 * 1024
    max_total_bytes: int = 50 * 1024 * 1024
    max_requests: int = 1000
    max_page_blocks: int = 5000
    max_run_blocks: int = 10_000
    max_depth: int = 16
    deadline_s: float = 600
    request_spacing_s: float = 0.35


@dataclass(frozen=True)
class PageSnapshot:
    page_id: str
    title: str
    blocks: tuple[dict, ...] = field(repr=False)


NESTED_TYPES = {'paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item',
                'numbered_list_item', 'to_do', 'toggle', 'quote', 'callout',
                'table', 'table_row', 'column', 'column_list'}


def plain_text(values):
    if not isinstance(values, list):
        raise NotionError('Invalid Notion text response.')
    parts = []
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get('plain_text'), str):
            raise NotionError('Invalid Notion text response.')
        parts.append(item['plain_text'])
    return ''.join(parts)


class NotionClient:
    def __init__(self, token, *, transport=None, limits=None, sleep=time.sleep,
                 still_allowed=lambda: None):
        if (not isinstance(token, str) or not token or not token.isascii()
                or any(ord(c) <= 32 or ord(c) >= 127 for c in token)):
            raise NotionError('Connect Notion in Collect first.')
        self.limits = limits or Limits()
        self._client = httpx.Client(transport=transport, follow_redirects=False, headers={
            'Authorization': 'Bearer ' + token, 'Notion-Version': '2026-03-11',
            'Content-Type': 'application/json'})
        self._sleep = sleep
        self._allowed = still_allowed
        self._start = time.monotonic()
        self._last_request = None
        self._requests = self._bytes = self._blocks = 0

    def _get(self, route, params=None):
        self._allowed()
        if self._last_request is not None:
            self._sleep(max(0, self.limits.request_spacing_s
                            - (time.monotonic() - self._last_request)))
        remaining = self.limits.deadline_s - (time.monotonic() - self._start)
        self._requests += 1
        if remaining <= 0 or self._requests > self.limits.max_requests:
            raise NotionError('Notion run exceeded its time or request limit.')
        self._last_request = time.monotonic()
        try:
            with self._client.stream('GET', 'https://api.notion.com/v1/' + route,
                    params=params, timeout=min(20, remaining)) as response:
                if response.status_code == 429:
                    raise NotionError('Notion rate limit reached; try again later.')
                if response.status_code != 200:
                    raise NotionError('Notion could not read a selected page. Check its connection access.')
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    self._bytes += len(chunk)
                    if (size > self.limits.max_response_bytes
                            or self._bytes > self.limits.max_total_bytes
                            or time.monotonic() - self._start > self.limits.deadline_s):
                        raise NotionError('Notion response exceeded the read limit.')
                    chunks.append(chunk)
            result = json.loads(b''.join(chunks))
        except httpx.HTTPError:
            raise NotionError('Notion connection failed; check the connection and try again.') from None
        except (ValueError, UnicodeError, RecursionError):
            raise NotionError('Invalid Notion response.') from None
        if not isinstance(result, dict):
            raise NotionError('Invalid Notion response.')
        self._allowed()
        return result

    def _children(self, parent, depth, seen):
        if depth > self.limits.max_depth:
            raise NotionError('Selected Notion page exceeds the nesting limit.')
        output = []
        cursor = None
        cursors = set()
        while True:
            params = {'page_size': 100}
            if cursor is not None:
                params['start_cursor'] = cursor
            data = self._get(f'blocks/{parent}/children', params)
            items = data.get('results')
            if (data.get('object') != 'list' or not isinstance(items, list)
                    or type(data.get('has_more')) is not bool):
                raise NotionError('Invalid Notion block list.')
            for original in items:
                if not isinstance(original, dict) or original.get('object') != 'block':
                    raise NotionError('Invalid Notion block.')
                identifier = _id(original.get('id'))
                parent_info = original.get('parent', {})
                parent_kind = parent_info.get('type') if isinstance(parent_info, dict) else None
                if (parent_kind not in {'block_id', 'page_id'}
                        or _id(parent_info.get(parent_kind)) != parent or identifier in seen
                        or type(original.get('has_children')) is not bool
                        or not isinstance(original.get('type'), str)):
                    raise NotionError('Invalid or repeated Notion block relationship.')
                seen.add(identifier)
                self._blocks += 1
                if len(seen) > self.limits.max_page_blocks or self._blocks > self.limits.max_run_blocks:
                    raise NotionError('Selected Notion pages exceed the block limit.')
                item = dict(original)
                item.pop("_children", None)
                if (item['has_children'] and item['type'] in NESTED_TYPES
                        and not item.get('archived') and not item.get('in_trash')):
                    item['_children'] = self._children(identifier, depth + 1, seen)
                output.append(item)
            if not data['has_more']:
                if data.get('next_cursor') is not None:
                    raise NotionError('Invalid terminal Notion pagination cursor.')
                break
            cursor = data.get('next_cursor')
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise NotionError('Invalid or repeated Notion pagination cursor.')
            cursors.add(cursor)
        return output

    def snapshot(self, page_id):
        page_id = _id(page_id)
        data = self._get('pages/' + page_id)
        if (data.get('object') != 'page' or _id(data.get('id')) != page_id
                or data.get('archived') or data.get('in_trash')):
            raise NotionError('Selected Notion page is unavailable.')
        properties = data.get('properties')
        if not isinstance(properties, dict):
            raise NotionError('Invalid Notion page properties.')
        titles = [plain_text(p.get('title')) for p in properties.values()
                  if isinstance(p, dict) and p.get('type') == 'title']
        title = titles[0] if titles else 'Untitled page'
        return PageSnapshot(page_id, title, tuple(self._children(page_id, 0, set())))

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
