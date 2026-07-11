"""Files writer — deterministic path + same-content idempotent overwrite.

Synthetic data only (``@example.com`` etc.); no real PII.
"""
from __future__ import annotations

import hashlib

from fulcra_gmail.files_writer import FilesWriter, canonical_json, files_path


class FakeFilesApi:
    """Dict-backed stand-in for ``fulcra_api.core.FulcraAPI.upload_file``."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.store: dict[str, bytes] = {}

    def upload_file(self, data, file_type: str, file_size: int, filepath: str):
        body = data.read()
        assert file_type == "application/json"
        assert file_size == len(body)
        self.uploads.append((filepath, body))
        self.store[filepath] = body
        return {"id": f"file-{len(self.uploads)}"}


def test_files_path_uses_account_and_month_and_message_id():
    # 2021-06-15T00:00:00Z in ms.
    ms = 1623715200000
    p = files_path("acct-1", "m123", ms)
    assert p == "/collect/gmail/acct-1/2021-06/m123.json"


def test_files_path_is_stable_for_same_inputs():
    ms = 1623715200000
    assert files_path("a", "m", ms) == files_path("a", "m", ms)


def test_canonical_json_is_byte_stable_regardless_of_key_order():
    a = canonical_json({"b": 1, "a": 2})
    b = canonical_json({"a": 2, "b": 1})
    assert a == b
    assert isinstance(a, bytes)


def test_write_uploads_selected_email_and_returns_sha_and_path():
    api = FakeFilesApi()
    writer = FilesWriter(api)
    selected = {"message_id": "m1", "headers": {"Subject": "hi"}}
    ms = 1623715200000
    result = writer.write("acct-1", "m1", ms, selected)
    body = canonical_json(selected)
    assert result.path == "/collect/gmail/acct-1/2021-06/m1.json"
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert api.store[result.path] == body


def test_rewrite_same_message_is_same_content_overwrite():
    api = FakeFilesApi()
    writer = FilesWriter(api)
    selected = {"message_id": "m1", "headers": {"Subject": "hi"}}
    ms = 1623715200000
    r1 = writer.write("acct-1", "m1", ms, selected)
    r2 = writer.write("acct-1", "m1", ms, selected)
    # Same path, identical bytes both times (post-crash rewrite is safe).
    assert r1.path == r2.path
    assert r1.sha256 == r2.sha256
    assert api.uploads[0][1] == api.uploads[1][1]
