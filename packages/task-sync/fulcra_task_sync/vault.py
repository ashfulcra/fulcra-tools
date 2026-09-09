"""Bounded Files adapter matching fulcra-api 0.1.41's file implementation."""
from pathlib import PurePosixPath
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
    def __init__(self, token: str, *, base_url='https://api.fulcradynamics.com',
                 namespace=None, timeout_s=20, max_versions=500, transport=None):
        self._validate_url(base_url)
        self.base_url = base_url.rstrip('/')
        self._namespace = namespace
        self.timeout_s = min(float(timeout_s), 30)
        self.max_versions = max_versions
        self.deadline = None
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
        if self._namespace is None:
            value = self._json('GET', '/user/v1alpha1/info').get('userid')
            if not isinstance(value, str) or not value:
                raise VaultError('account identity unavailable')
            self._namespace = value
        return self._namespace

    def versions(self, path):
        p = PurePosixPath('/' + path.lstrip('/'))
        result = self._json('GET', '/input/v1/file_upload', params={
            'path': str(p.parent), 'name': p.name, 'state': 'uploaded,archived'})
        rows = result.get('files')
        if (not isinstance(rows, list) or len(rows) > self.max_versions
                or any(result.get(k) for k in ('next_cursor', 'next_page', 'has_more'))):
            raise VaultError('incomplete version history')
        if any(not isinstance(r, dict) or not isinstance(r.get('id'), str) or not r['id']
               or r.get('state') not in ('uploaded', 'archived')
               or not isinstance(r.get('uploaded_at'), str) for r in rows):
            raise VaultError('invalid version history')
        if rows and not any(r['state'] == 'uploaded' for r in rows):
            raise VaultError('missing current version')
        ids = [r['id'] for r in sorted(rows, key=lambda r: r['uploaded_at'], reverse=True)]
        if len(set(ids)) != len(ids):
            raise VaultError('duplicate versions')
        return ids

    def read_version(self, version_id):
        status, headers, data = self._request('GET', self.base_url +
            f'/input/v1/file_upload/{quote(version_id, safe="")}/download')
        if 300 <= status < 400:
            # Follow a signed storage redirect with no Fulcra credentials.
            status, _, data = self._request('GET', headers.get('location', ''), storage=True)
        if status >= 300:
            raise VaultError('unexpected storage redirect')
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
        status, _, _ = self._request('POST', result.get('url', ''), storage=True,
                                    content=data, headers={'Content-Type': content_type,
                                                          'Content-Length': str(len(data))})
        if status >= 300:
            raise VaultError('unexpected upload redirect')
        return version

    def close(self):
        self._api.close()
        self._storage.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
