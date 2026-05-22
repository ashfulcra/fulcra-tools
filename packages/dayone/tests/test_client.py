"""DayOneFulcraClient — find-or-create the Journal definition."""
from __future__ import annotations

import json

import httpx
import pytest

from fulcra_dayone.client import DayOneFulcraClient


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-token")


def test_ensure_journal_definition_adopts_existing(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[
                {"name": "Journal", "annotation_type": "instant",
                 "id": "def-journal", "created_at": "2026-01-01T00:00:00Z",
                 "deleted_at": None},
            ])
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-journal"


def test_ensure_journal_definition_creates_when_absent(recording_transport):
    posted: list[dict] = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[])
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            posted.append(json.loads(r.content))
            return httpx.Response(200, json={"id": "def-new"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-new"
    assert posted[0]["name"] == "Journal"
    assert posted[0]["annotation_type"] == "instant"
    assert posted[0]["measurement_spec"]["measurement_type"] == "instant"


def test_ensure_journal_definition_picks_oldest_duplicate(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"name": "Journal", "annotation_type": "instant", "id": "def-new",
             "created_at": "2026-05-01T00:00:00Z", "deleted_at": None},
            {"name": "Journal", "annotation_type": "instant", "id": "def-old",
             "created_at": "2026-01-01T00:00:00Z", "deleted_at": None},
        ])

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-old"


def test_ensure_journal_definition_ignores_soft_deleted(recording_transport):
    posted: list[dict] = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET":
            return httpx.Response(200, json=[
                {"name": "Journal", "annotation_type": "instant",
                 "id": "def-dead", "created_at": "2026-01-01T00:00:00Z",
                 "deleted_at": "2026-02-01T00:00:00Z"},
            ])
        posted.append(json.loads(r.content))
        return httpx.Response(200, json={"id": "def-fresh"})

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-fresh"
    assert len(posted) == 1
