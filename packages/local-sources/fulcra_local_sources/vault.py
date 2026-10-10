"""Files uploads bound to Collect's authenticated account, never CLI auth."""
from __future__ import annotations

import json
import mimetypes
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx


class VaultError(RuntimeError):
    pass


class VaultWriter:
    def __init__(self, token: str, *, timeout: int = 20, transport=None):
        if not isinstance(token, str) or not token.strip():
            raise VaultError("Sign in to Fulcra in Collect before uploading.")
        self.timeout = min(timeout, 30)
        self._namespace = None
        self._api = httpx.Client(headers={"Authorization": f"Bearer {token}"},
                                 transport=transport, follow_redirects=False)
        self._storage = httpx.Client(transport=transport, follow_redirects=False)

    def _request(self, client, method: str, url: str, **kwargs) -> bytes:
        parsed = urlparse(url)
        if (parsed.scheme != "https" or not parsed.hostname
                or parsed.username or parsed.password):
            raise VaultError("invalid secure URL for Fulcra file upload")
        try:
            with client.stream(method, url, timeout=self.timeout, **kwargs) as response:
                if not 200 <= response.status_code < 300:
                    raise VaultError("Fulcra file upload request failed")
                chunks = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise VaultError("Fulcra file upload response limit")
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError:
            raise VaultError("Fulcra file upload transport failed") from None

    def _json(self, method: str, route: str, **kwargs) -> dict:
        body = self._request(self._api, method,
                             "https://api.fulcradynamics.com" + route, **kwargs)
        try:
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, UnicodeError):
            raise VaultError("invalid Fulcra file upload response") from None

    @property
    def namespace(self) -> str:
        if self._namespace is None:
            value = self._json("GET", "/user/v1alpha1/info").get("userid")
            if not isinstance(value, str) or not value.strip():
                raise VaultError("Fulcra account identity unavailable")
            self._namespace = value
        return self._namespace

    def _upload(self, content, remote: str, size: int, content_type: str):
        path = PurePosixPath(remote)
        if not path.is_absolute() or ".." in path.parts or not path.name:
            raise VaultError("invalid Fulcra destination path")
        result = self._json("POST", "/input/v1/file_upload", json={
            "content_length": size, "content_type": content_type,
            "name": path.name, "path": str(path.parent)})
        file = result.get("file")
        if not isinstance(file, dict) or not isinstance(file.get("id"), str) or not file["id"]:
            raise VaultError("Fulcra upload identity unavailable")
        url = result.get("url")
        if not isinstance(url, str):
            raise VaultError("Fulcra upload URL unavailable")
        self._request(self._storage, "POST", url, content=content,
                      headers={"Content-Type": content_type, "Content-Length": str(size)})

    def upload_file(self, source: Path | str, remote: str) -> None:
        try:
            path = Path(source)
            with path.open("rb") as handle:
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self._upload(handle, remote, path.stat().st_size, content_type)
        except OSError:
            raise VaultError("Fulcra upload source unavailable") from None

    def write_text(self, remote: str, content: str) -> None:
        data = content.encode("utf-8")
        self._upload(data, remote, len(data), "text/markdown; charset=utf-8")

    def close(self):
        self._api.close()
        self._storage.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
