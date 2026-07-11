"""Shared fakes for the fulcra-gmail suite.

Synthetic data ONLY — no real emails, ids, tokens, or addresses. Everything
uses ``example.com`` / ``@example.test`` / obviously-fake token strings so the
PII grep gate stays clean.

The two ports the registry depends on (keychain + non-secret store) are faked
in-memory here; the network is faked with ``httpx.MockTransport``.
"""
from __future__ import annotations

import base64
import contextlib
import json

import httpx
import pytest

from fulcra_gmail.accounts import AccountRegistry


# ---------------------------------------------------------------------------
# Synthetic Gmail messages.get(full) payload builders (rules/convert/ledger)
# ---------------------------------------------------------------------------


def b64url(text: str) -> str:
    """Gmail-style URL-safe base64 with the padding STRIPPED (as the API
    returns it) so the converter's padding logic is exercised."""
    raw = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    return raw.rstrip("=")


def header(name: str, value: str) -> dict:
    return {"name": name, "value": value}


def text_part(mime: str, body_text: str) -> dict:
    return {"mimeType": mime, "body": {"data": b64url(body_text)}}


def attachment_part(
    filename: str, mime: str, attachment_id: str, size: int
) -> dict:
    """An attachment part: metadata + attachmentId, NO inline ``data`` bytes
    (Gmail omits ``data`` for attachment parts; you fetch bytes separately)."""
    return {
        "mimeType": mime,
        "filename": filename,
        "body": {"attachmentId": attachment_id, "size": size},
    }


def make_message(
    *,
    msg_id: str = "m1",
    thread_id: str = "t1",
    headers: list[dict] | None = None,
    payload: dict | None = None,
) -> dict:
    """A synthetic ``messages.get(format=full)`` envelope."""
    if payload is None:
        payload = {"headers": headers or [], "mimeType": "text/plain",
                   "body": {"data": b64url("")}}
    elif headers is not None:
        payload = {**payload, "headers": headers}
    return {"id": msg_id, "threadId": thread_id, "payload": payload}


class FakeKeychain:
    """Dict-backed :class:`~fulcra_gmail.accounts.Keychain`."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class InMemoryStore:
    """Dict-backed :class:`~fulcra_gmail.accounts.RegistryStore`."""

    def __init__(self) -> None:
        self.doc: dict = {}

    def read(self) -> dict:
        # Return a copy so callers must go through write() to persist —
        # mirrors a real file store's read-modify-write.
        return json.loads(json.dumps(self.doc)) if self.doc else {}

    def write(self, doc: dict) -> None:
        self.doc = json.loads(json.dumps(doc))

    def transaction(self):
        # Single in-process instance in the unit tests — the registry's own
        # threading.Lock already serializes; no cross-process lock needed.
        # Cross-process atomicity is exercised against a real JsonFileStore in
        # test_concurrency.py.
        return contextlib.nullcontext()


def json_response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


@pytest.fixture
def keychain() -> FakeKeychain:
    return FakeKeychain()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def make_registry(keychain, store):
    """Factory: build an AccountRegistry over the fakes with an optional
    transport."""

    def _make(transport: httpx.BaseTransport | None = None) -> AccountRegistry:
        return AccountRegistry(store=store, keychain=keychain, transport=transport)

    return _make
