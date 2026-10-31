"""The Fulcra Files writer for selected-email JSON.

An *effective match* is serialized by :mod:`fulcra_gmail.convert` and written to
the operator's own Fulcra Files at the deterministic path::

    /collect/gmail/<account_id>/<yyyy-mm>/<message_id>.json

The path is a pure function of ``(account_id, message_id, internalDate)`` — the
same message always lands at the same path, so a post-crash re-write is a
same-content overwrite (idempotent), never a duplicate. ``<yyyy-mm>`` is derived
from the message's ``internalDate`` (UTC), NOT from the wall clock, so the shard
a message lives in never depends on when the poll happened to run.

The body is :func:`canonical_json` — sorted keys, tight separators — so the same
selected-email dict serializes to identical bytes every time. Its SHA-256 goes
into the privacy ledger (metadata + hash only; the ledger never holds content).

The writer talks to Fulcra Files through an injectable ``api`` object exposing
``upload_file(data, file_type, file_size, filepath)`` (the
``fulcra_api.core.FulcraAPI`` surface — the same one
:class:`fulcra_prefs.store.FulcraStore` uses). Tests inject a dict-backed fake;
Collect injects its guarded Files transport, bound to the captured account.
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime, timezone


def canonical_json(obj: object) -> bytes:
    """Byte-stable JSON: sorted keys, compact separators, UTF-8.

    Deterministic for a given dict regardless of key insertion order, so the
    same selected-email serializes to identical bytes (identical SHA-256) on
    every run — the property the same-content-overwrite guarantee rests on.
    """
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _yyyy_mm(internal_date_ms: int | str | None) -> str:
    """The ``<yyyy-mm>`` shard for a Gmail ``internalDate`` (ms since epoch, UTC).

    A missing/unparseable date falls back to ``"undated"`` rather than raising —
    the file still lands (over-capture beats loss), just in a catch-all shard.
    """
    if internal_date_ms is None:
        return "undated"
    try:
        seconds = int(internal_date_ms) / 1000
        return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m")
    except (ValueError, TypeError, OverflowError, OSError):
        return "undated"


def files_path(account_id: str, message_id: str, internal_date_ms: int | str | None) -> str:
    """The deterministic Fulcra Files path for one selected email."""
    return f"/collect/gmail/{account_id}/{_yyyy_mm(internal_date_ms)}/{message_id}.json"


@dataclass(frozen=True)
class FileWriteResult:
    path: str
    sha256: str


class FilesWriter:
    """Writes selected-email JSON to Fulcra Files via an injected ``api``."""

    def __init__(self, api: object) -> None:
        #: A ``fulcra_api.core.FulcraAPI``-shaped object (or a fake) exposing
        #: ``upload_file(data, file_type, file_size, filepath)``.
        self._api = api

    def write(
        self,
        account_id: str,
        message_id: str,
        internal_date_ms: int | str | None,
        selected_email: dict,
    ) -> FileWriteResult:
        """Serialize + upload the selected email; return its path + content SHA.

        Idempotent by construction: the path is deterministic and the bytes are
        canonical, so re-writing the same message overwrites with identical
        content. Never logs subject/body/from — only the opaque path is a fact
        worth a debug line (and even that is left to the caller).
        """
        body = canonical_json(selected_email)
        path = files_path(account_id, message_id, internal_date_ms)
        self._api.upload_file(io.BytesIO(body), "application/json", len(body), path)
        return FileWriteResult(path=path, sha256=hashlib.sha256(body).hexdigest())
