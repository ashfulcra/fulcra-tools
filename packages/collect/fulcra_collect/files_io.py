"""Account-bound Files HTTP used by Collect plugins; no CLI auth fallback."""
import json
import re
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import urlparse
from uuid import UUID

import httpx

from fulcra_common.record_observations import _PARTIAL_HEADERS

from .config_leases import task_mutation_scope


class FilesIOError(RuntimeError):
    """The remote file state is unknown."""


class MissingFile(FilesIOError):
    """A complete, validated listing positively proved absence."""


class CollectFiles:
    """Keep existing Files paths without inheriting a CLI account.

    The session's API hook checks fresh principal/settings for every request.
    Signed storage uses a separate credential-free client and no redirects.
    Supported account/settings transitions wait for each leased effect to end.
    """
    def __init__(self, session, *, storage_transport=None, guard=None):
        self.session = session
        self.guard = guard or session.guard
        self.namespace = session.account_id
        self.storage = httpx.Client(transport=storage_transport, follow_redirects=False)

    @staticmethod
    def _complete_read_headers(headers, *, row_count=None):
        if any(name in headers for name in _PARTIAL_HEADERS):
            raise FilesIOError("Files response is partial; remote state is unknown")
        for name in ("x-total-count", "x-total"):
            total = headers.get(name)
            if total is not None and (not re.fullmatch(r"[0-9]{1,10}", total)
                    or (row_count is not None and int(total) != row_count)):
                raise FilesIOError("Files response total is incomplete or unreadable")

    def _request(self, method, url, *, storage=False, timeout=30, limit=32 * 1024 * 1024, **kwargs):
        mutating = method not in {"GET", "HEAD"}
        with task_mutation_scope():
            self.guard(mutating=mutating)
            if storage:
                parsed = urlparse(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
                    raise FilesIOError("invalid signed storage URL")
                client = self.storage
            else:
                client = self.session.client._client()
                kwargs["headers"] = {**kwargs.get("headers", {}), **self.session.client._authed_headers()}
            try:
                with client.stream(method, url, follow_redirects=False, timeout=min(max(float(timeout), 0.1), 30), **kwargs) as response:
                    if response.is_redirect:
                        return response.status_code, response.headers, b""
                    response.raise_for_status()
                    if method in {"GET", "HEAD"}:
                        if response.status_code != 200:
                            raise FilesIOError("Files read is not a complete HTTP success")
                        self._complete_read_headers(response.headers)
                    chunks, count = [], 0
                    for chunk in response.iter_bytes():
                        self.guard(mutating=mutating)
                        count += len(chunk)
                        if count > limit:
                            raise FilesIOError("Files response exceeds the bounded read limit")
                        chunks.append(chunk)
                    self.guard(mutating=mutating)
                    body = b"".join(chunks)
                    # Bound vault consumers use _request directly, so listing
                    # completeness must hold before the transport releases bytes.
                    if not storage and method == "GET" and urlparse(url).path == "/input/v1/file_upload":
                        try:
                            rows = json.loads(body)["files"]
                            if not isinstance(rows, list):
                                raise ValueError
                        except (ValueError, TypeError, KeyError, UnicodeError):
                            raise FilesIOError("Files listing is unreadable") from None
                        self._complete_read_headers(response.headers, row_count=len(rows))
                    return response.status_code, response.headers, body
            except httpx.HTTPError:
                raise FilesIOError("Collect Files transport failed; remote state is unknown") from None

    def _json(self, method, path, *, timeout=30, **kwargs):
        status, headers, raw = self._request(method, path, timeout=timeout, **kwargs)
        try:
            if status >= 300 or (method == "GET" and status != 200):
                raise ValueError
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            if method == "GET" and isinstance(result.get("files"), list):
                self._complete_read_headers(headers, row_count=len(result["files"]))
            return result
        except (ValueError, UnicodeError):
            raise FilesIOError("Files response is unreadable") from None

    @staticmethod
    def _path(remote):
        path = PurePosixPath(remote)
        if not path.is_absolute() or ".." in path.parts or not path.name:
            raise FilesIOError("Files destination path is invalid")
        return path

    def _resolve(self, remote, *, timeout):
        path = self._path(remote)
        body = self._json("GET", "/input/v1/file_upload", timeout=timeout, params={
            "path": str(path.parent), "name": path.name, "state": "uploaded", "fulcra_userid": self.namespace})
        rows = body.get("files")
        if not isinstance(rows, list) or len(rows) > 1 or any(body.get(k) for k in ("has_more", "next_cursor", "next_page", "continuation_token")):
            raise FilesIOError("Files listing is incomplete or ambiguous")
        if not rows:
            raise MissingFile(remote)
        row = rows[0]
        try:
            if (not isinstance(row, dict) or row.get("name") != path.name
                    or PurePosixPath(row.get("path")) != path.parent or row.get("state") != "uploaded"
                    or row.get("fulcra_userid", self.namespace) != self.namespace
                    or str(UUID(row["id"])) != row["id"]):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise FilesIOError("Files identity is unreadable or mismatched") from None
        return row

    def read_text(self, remote, *, timeout=60):
        row = self._resolve(remote, timeout=timeout)
        expected_size = row.get("size")
        if "size" in row and (type(expected_size) is not int or expected_size < 0):
            raise FilesIOError("Files version size is unreadable; remote state is unknown")
        status, headers, body = self._request("GET", f"/input/v1/file_upload/{row['id']}/download", timeout=timeout)
        if 300 <= status < 400:
            status, _, body = self._request("GET", headers.get("location", ""), storage=True, timeout=timeout)
        if status >= 300:
            raise FilesIOError("unexpected storage redirect")
        if expected_size is not None and len(body) != expected_size:
            raise FilesIOError("Files download disagrees with version size; remote state is unknown")
        try:
            return body.decode("utf-8")
        except UnicodeError:
            raise FilesIOError("Files text is not valid UTF-8") from None

    def stat(self, remote, *, timeout=60):
        try:
            return json.dumps(self._resolve(remote, timeout=timeout))
        except MissingFile:
            return None

    def _upload(self, content, remote, size, content_type, *, timeout):
        path = self._path(remote)
        with task_mutation_scope():
            self.guard(mutating=True)
            result = self._json("POST", "/input/v1/file_upload", timeout=timeout, json={
                "content_length": size, "content_type": content_type, "name": path.name, "path": str(path.parent)})
            try:
                if str(UUID(result["file"]["id"])) != result["file"]["id"] or not isinstance(result["url"], str):
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                raise FilesIOError("upload identity is unreadable") from None
            status, _, _ = self._request("POST", result["url"], storage=True, timeout=timeout, limit=1024 * 1024,
                content=content, headers={"Content-Type": content_type, "Content-Length": str(size)})
            if status >= 300:
                raise FilesIOError("unexpected upload redirect")
            self.guard(mutating=True)

    def write_text(self, remote, content, *, timeout=120):
        body = content.encode("utf-8")
        self._upload(body, remote, len(body), "text/markdown; charset=utf-8", timeout=timeout)

    def upload_file(self, content, content_type, size, remote, *, timeout=30):
        """SDK-shaped byte-stream upload without unguarded storage I/O."""
        return self._upload(content, remote, size, content_type, timeout=timeout)

    def list_dir(self, remote, *, timeout=30):
        path = self._path(remote)
        body = self._json("GET", "/input/v1/file_upload", timeout=timeout,
            params={"path": str(path), "state": "uploaded", "fulcra_userid": self.namespace})
        files, folders = body.get("files"), body.get("folders", [])
        if folders is None:  # API explicitly encodes no folders as null.
            folders = []
        if (not isinstance(files, list) or not isinstance(folders, list)
                or any(body.get(k) for k in ("has_more", "next_cursor", "next_page", "continuation_token"))):
            raise FilesIOError("Collect Files listing is incomplete or unreadable")
        rows, names, ids = [], set(), set()
        def add_name(name):
            if (not isinstance(name, str) or not name or name in {".", ".."}
                    or "/" in name or "\\" in name or any(ord(c) < 32 for c in name) or name in names):
                raise FilesIOError("Collect Files listing has ambiguous or unsafe names")
            names.add(name)
        for name in folders:
            add_name(name)
            rows.append({"name": name, "is_dir": True})
        for row in files:
            try:
                if (not isinstance(row, dict) or PurePosixPath(row.get("path")) != path
                        or row.get("state") != "uploaded"
                        or row.get("fulcra_userid", self.namespace) != self.namespace
                        or str(UUID(row["id"])) != row["id"] or row["id"] in ids
                        or type(row.get("size")) is not int or row["size"] < 0
                        or not isinstance(row.get("uploaded_at"), str)
                        or datetime.fromisoformat(row["uploaded_at"].replace("Z", "+00:00")).tzinfo is None):
                    raise ValueError
            except (ValueError, TypeError, KeyError, AttributeError):
                raise FilesIOError("Collect Files listing identity is unreadable or mismatched") from None
            add_name(row.get("name"))
            ids.add(row["id"])
            rows.append({"name": row["name"], "is_dir": False,
                         "size": row["size"], "mtime": row["uploaded_at"]})
        self.guard()
        return sorted(rows, key=lambda row: row["name"])

    def delete(self, remote, *, timeout=30):
        with task_mutation_scope():
            self.guard(mutating=True)
            row = self._resolve(remote, timeout=timeout)
            status, _, _ = self._request("DELETE", f"/input/v1/file_upload/{row['id']}", timeout=timeout)
            if status >= 300:
                raise FilesIOError("unexpected Collect Files delete redirect")
            self.guard(mutating=True)

    def close(self):
        self.storage.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
