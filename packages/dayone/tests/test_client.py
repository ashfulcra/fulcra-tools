"""DayOneFulcraClient — find-or-create the Journal definition."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
from fulcra_csv.events import INSTANT, GenericEvent

from fulcra_dayone.client import DayOneFulcraClient


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-token")


def test_ensure_journal_definition_adopts_existing(recording_transport):
    """An existing live "Journal" moment definition is adopted, not recreated."""
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[
                {"name": "Journal", "annotation_type": "moment",
                 "id": "def-journal", "created_at": "2026-01-01T00:00:00Z",
                 "deleted_at": None},
            ])
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json={"id": "def-created-not-adopted"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-journal"


def test_ensure_journal_definition_creates_a_moment_definition(recording_transport):
    """With no existing definition, a `moment` definition is created.

    Fulcra's annotation-definition `annotation_type` enum has no "instant";
    the point-in-time variant is "moment", and that union variant carries no
    `measurement_spec` field. The create POST answers 303 See Other — the
    client must follow the redirect to the new resource rather than raise.
    """
    posted: list[dict] = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[])
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            posted.append(json.loads(r.content))
            return httpx.Response(
                303, headers={"Location": "/user/v1alpha1/annotation/def-new"},
            )
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation/def-new":
            return httpx.Response(200, json={
                "id": "def-new", "name": "Journal", "annotation_type": "moment",
            })
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-new"
    assert posted[0]["name"] == "Journal"
    assert posted[0]["annotation_type"] == "moment"
    assert "measurement_spec" not in posted[0]


def test_ensure_journal_definition_picks_oldest_duplicate(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[
                {"name": "Journal", "annotation_type": "moment", "id": "def-new",
                 "created_at": "2026-05-01T00:00:00Z", "deleted_at": None},
                {"name": "Journal", "annotation_type": "moment", "id": "def-old",
                 "created_at": "2026-01-01T00:00:00Z", "deleted_at": None},
            ])
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json={"id": "def-created-not-adopted"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-old"


def test_ensure_journal_definition_ignores_soft_deleted(recording_transport):
    posted: list[dict] = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation":
            return httpx.Response(200, json=[
                {"name": "Journal", "annotation_type": "moment",
                 "id": "def-dead", "created_at": "2026-01-01T00:00:00Z",
                 "deleted_at": "2026-02-01T00:00:00Z"},
            ])
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            posted.append(json.loads(r.content))
            return httpx.Response(
                303, headers={"Location": "/user/v1alpha1/annotation/def-fresh"},
            )
        if r.method == "GET" and r.url.path == "/user/v1alpha1/annotation/def-fresh":
            return httpx.Response(200, json={"id": "def-fresh"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    client = DayOneFulcraClient(transport=recording_transport(responder))
    assert client.ensure_journal_definition() == "def-fresh"
    assert len(posted) == 1


def test_run_import_targets_the_moment_annotation_data_type(recording_transport):
    """run_import's dedup readback and ingest must both target the
    `MomentAnnotation` data type. Fulcra has no `InstantAnnotation` data
    type — querying it 404s and ingesting under it is unreadable."""
    ev = GenericEvent(
        start_time=datetime(2024, 1, 1, tzinfo=timezone.utc),
        note="hello", title=None, source_id="src-1", annotation_type=INSTANT,
    )

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path.startswith("/data/v1alpha1/event/"):
            return httpx.Response(200, json=[])
        if r.method == "POST" and r.url.path == "/ingest/v1/record/batch":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = DayOneFulcraClient(transport=transport)
    client.run_import([ev], definition_id="def-1")

    readbacks = [q for q in transport.requests
                 if q.url.path.startswith("/data/v1alpha1/event/")]
    assert readbacks, "expected a dedup readback request"
    assert all(q.url.path == "/data/v1alpha1/event/MomentAnnotation"
               for q in readbacks)

    ingests = [q for q in transport.requests
               if q.url.path == "/ingest/v1/record/batch"]
    assert ingests, "expected an ingest request"
    record = json.loads(ingests[0].content.splitlines()[0])
    assert record["metadata"]["data_type"] == "MomentAnnotation"
