"""Version-history Files adapter with explicit token or captured transport."""
from pathlib import PurePosixPath
from datetime import datetime, timezone
from uuid import UUID
import time
from typing import Protocol
from urllib.parse import quote, urlparse

import httpx

from .documents import MAX_BYTES


class VaultError(RuntimeError):
    pass


class Vault(Protocol):
    namespace: str
    def versions(self, path: str) -> list[str]: ...  # newest first, complete history
    def read_version(self, version_id: str) -> str: ...
    def write(self, path: str, text: str) -> str: ...


class FulcraVault:
    def __init__(self, token: str | None = None, *, base_url='https://api.fulcradynamics.com',
                 namespace=None, timeout_s=20, max_versions=500, transport=None, bound_files=None):
        self._validate_url(base_url)
        self.base_url = base_url.rstrip('/')
        self._bound_files = bound_files
        if bound_files is not None:
            if token is not None or (namespace is not None and namespace != bound_files.namespace):
                raise VaultError('ambiguous bound account')
            namespace = bound_files.namespace
        elif not isinstance(token, str) or not token:
            raise VaultError('explicit task vault sign-in required')
        self._namespace = namespace
        self.timeout_s = min(float(timeout_s), 30)
        self.max_versions = max_versions
        self._version_sizes = {}
        self.deadline = None
        self._api = self._storage = None
        if bound_files is None:
            self._api = httpx.Client(headers={'Authorization': f'Bearer {token}'},
                                     transport=transport, follow_redirects=False)
            # A separate client also prevents default credentials/cookies leaking.
            self._storage = httpx.Client(transport=transport, follow_redirects=False)

    @staticmethod
    def _validate_url(url):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise VaultError('invalid secure URL')

    def set_deadline(self, deadline):
        self.deadline = deadline

    def _request(self, method, url, *, storage=False, **kwargs):
        self._validate_url(url)
        timeout = self.timeout_s
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
        if timeout <= 0:
            raise VaultError('run deadline')
        if self._bound_files is not None:
            return self._bound_files._request(method, url, storage=storage,
                timeout=timeout, limit=MAX_BYTES, **kwargs)
        client = self._storage if storage else self._api
        try:
            with client.stream(method, url, timeout=timeout, **kwargs) as response:
                if response.is_redirect:
                    return response.status_code, response.headers, b''
                response.raise_for_status()
                chunks, total = [], 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES or (self.deadline and time.monotonic() >= self.deadline):
                        raise VaultError('response limit')
                    chunks.append(chunk)
                return response.status_code, response.headers, b''.join(chunks)
        except httpx.HTTPError as exc:
            raise VaultError('Fulcra transport failed') from exc

    def _json(self, method, route, **kwargs):
        import json
        status, _, data = self._request(method, self.base_url + route, **kwargs)
        if status >= 300:
            raise VaultError('unexpected API redirect')
        try:
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError) as exc:
            raise VaultError('invalid API response') from exc

    @property
    def namespace(self):
        if self._bound_files is not None:
            self._bound_files.guard()
        if self._namespace is None:
            value = self._json('GET', '/user/v1alpha1/info').get('userid')
            if not isinstance(value, str) or not value:
                raise VaultError('account identity unavailable')
            self._namespace = value
        return self._namespace

    def versions(self, path):
        # Metadata belongs to one complete history snapshot. A failed or later
        # listing cannot leave an old exact-version read capability behind.
        self._version_sizes = {}
        p = PurePosixPath('/' + path.lstrip('/'))
        namespace = self.namespace if self._bound_files is not None else None
        params = {'path': str(p.parent), 'name': p.name, 'state': 'uploaded,archived'}
        if self._bound_files is not None:
            params['fulcra_userid'] = namespace
        result = self._json('GET', '/input/v1/file_upload', params=params)
        rows = result.get('files')
        if (not isinstance(rows, list) or len(rows) > self.max_versions
                or any(result.get(k) for k in ('next_cursor', 'next_page', 'has_more', 'continuation_token'))):
            raise VaultError('incomplete version history')
        if any(not isinstance(r, dict) or not isinstance(r.get('id'), str) or not r['id']
               or r.get('state') not in ('uploaded', 'archived')
               or not isinstance(r.get('uploaded_at'), str)
               or ('size' in r and (type(r['size']) is not int or r['size'] < 0)) for r in rows):
            raise VaultError('invalid version history')
        if rows and not any(r['state'] == 'uploaded' for r in rows):
            raise VaultError('missing current version')
        if self._bound_files is not None:
            try:
                if rows and sum(row['state'] == 'uploaded' for row in rows) != 1:
                    raise ValueError
                for row in rows:
                    if (str(UUID(row['id'])) != row['id'] or row.get('name') != p.name
                            or PurePosixPath(row.get('path')) != p.parent
                            or row.get('fulcra_userid', namespace) != namespace
                            or datetime.fromisoformat(row['uploaded_at'].replace('Z', '+00:00')).tzinfo is None):
                        raise ValueError
                rows = sorted(rows, key=lambda row: (row['state'] == 'uploaded',
                    datetime.fromisoformat(row['uploaded_at'].replace('Z', '+00:00')).astimezone(timezone.utc)), reverse=True)
            except (ValueError, TypeError, KeyError, AttributeError):
                raise VaultError('version history identity is ambiguous or mismatched') from None
        else:
            rows = sorted(rows, key=lambda r: r['uploaded_at'], reverse=True)
        ids = [r['id'] for r in rows]
        if len(set(ids)) != len(ids):
            raise VaultError('duplicate versions')
        if self._bound_files is not None:
            self._bound_files.guard()
        self._version_sizes = {row['id']: row.get('size') for row in rows}
        return ids

    def read_version(self, version_id):
        if not isinstance(version_id, str) or version_id not in self._version_sizes:
            raise VaultError('exact version metadata unavailable; relist history')
        expected_size = self._version_sizes[version_id]
        status, headers, data = self._request('GET', self.base_url +
            f'/input/v1/file_upload/{quote(version_id, safe="")}/download')
        if 300 <= status < 400:
            # Follow a signed storage redirect with no Fulcra credentials.
            status, _, data = self._request('GET', headers.get('location', ''), storage=True)
        if status != 200:
            raise VaultError('task version download is not a complete HTTP success')
        if expected_size is not None and len(data) != expected_size:
            raise VaultError('task version download disagrees with listed size')
        try:
            return data.decode('utf-8')
        except UnicodeError as exc:
            raise VaultError('invalid task encoding') from exc

    def write(self, path, text):
        data = text.encode('utf-8')
        if len(data) > MAX_BYTES:
            raise VaultError('oversized task')
        p = PurePosixPath('/' + path.lstrip('/'))
        content_type = 'text/markdown; charset=utf-8'
        result = self._json('POST', '/input/v1/file_upload', json={
            'content_length': len(data), 'content_type': content_type,
            'name': p.name, 'path': str(p.parent)})
        version = result.get('file', {}).get('id')
        if not isinstance(version, str) or not version:
            raise VaultError('upload identity unavailable')
        if self._bound_files is not None:
            try:
                if str(UUID(version)) != version:
                    raise ValueError
            except (ValueError, TypeError):
                raise VaultError('upload identity unavailable') from None
        status, _, _ = self._request('POST', result.get('url', ''), storage=True,
                                    content=data, headers={'Content-Type': content_type,
                                                          'Content-Length': str(len(data))})
        if status >= 300:
            raise VaultError('unexpected upload redirect')
        return version

    def close(self):
        if self._api is not None:
            self._api.close()
        if self._storage is not None:
            self._storage.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
