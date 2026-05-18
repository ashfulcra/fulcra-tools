"""Tag + definition bootstrap (idempotent)."""
from __future__ import annotations

import json

import httpx
import pytest

from fulcra_attention.fulcra import FulcraClient
from fulcra_attention.state import State


@pytest.fixture(autouse=True)
def _force_test_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "test-tok")


def test_ensure_tag_creates_when_missing(recording_transport):
    posted = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and "/tag/name/" in r.url.path:
            return httpx.Response(404)
        if r.method == "POST" and r.url.path == "/user/v1alpha1/tag":
            posted.append(json.loads(r.content))
            return httpx.Response(200, json={"id": "tag-new"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = FulcraClient(transport=transport)
    state = State()
    tag_id = client.ensure_tag("attention", state)
    assert tag_id == "tag-new"
    assert state.tag_ids["attention"] == "tag-new"
    assert posted == [{"name": "attention"}]


def test_ensure_tag_reuses_cache(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(tag_ids={"attention": "tag-cached"})
    assert client.ensure_tag("attention", state) == "tag-cached"


def test_ensure_tag_uses_existing_server_side(recording_transport):
    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/user/v1alpha1/tag/name/web":
            return httpx.Response(200, json={"id": "tag-existing"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = FulcraClient(transport=transport)
    state = State()
    assert client.ensure_tag("web", state) == "tag-existing"
    assert state.tag_ids["web"] == "tag-existing"


def test_ensure_definitions_creates_attention_def(recording_transport):
    posted_defs: list[dict] = []
    posted_tags: list[dict] = []

    def responder(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and "/tag/name/" in r.url.path:
            return httpx.Response(404)
        if r.method == "POST" and r.url.path == "/user/v1alpha1/tag":
            body = json.loads(r.content)
            posted_tags.append(body)
            return httpx.Response(200, json={"id": f"tag-{body['name']}"})
        if r.method == "POST" and r.url.path == "/user/v1alpha1/annotation":
            posted_defs.append(json.loads(r.content))
            return httpx.Response(200, json={"id": "def-attention"})
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = FulcraClient(transport=transport)
    state = State()
    client.ensure_definitions(state)
    assert state.attention_definition_id == "def-attention"
    assert {t["name"] for t in posted_tags} == {"attention", "web"}
    assert len(posted_defs) == 1
    d = posted_defs[0]
    assert d["name"] == "Attention"
    assert d["annotation_type"] == "duration"
    assert "tag-attention" in d["tags"] and "tag-web" in d["tags"]


def test_ensure_definitions_skips_when_already_cached(recording_transport):
    transport = recording_transport(lambda r: pytest.fail(f"unexpected {r.url}"))
    client = FulcraClient(transport=transport)
    state = State(
        attention_definition_id="def-x",
        tag_ids={"attention": "a", "web": "w"},
    )
    client.ensure_definitions(state)
    assert state.attention_definition_id == "def-x"
