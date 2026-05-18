# tests/test_ingest_event.py
"""build_attention_event — payload shape + source-id determinism."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from fulcra_attention.ingest import build_attention_event, source_id
from fulcra_attention.state import State


@pytest.fixture
def state() -> State:
    return State(
        attention_definition_id="def-att",
        tag_ids={"attention": "tag-a", "web": "tag-w"},
    )


def test_source_id_deterministic_for_same_url_and_second():
    t1 = datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 5, 18, 14, 0, 0, 500_000, tzinfo=timezone.utc)
    a = source_id(key="https://x.com/p", start_time=t1)
    b = source_id(key="https://x.com/p", start_time=t2)
    assert a == b
    assert a.startswith("com.fulcra.attention.v1.")


def test_source_id_changes_when_url_changes():
    t = datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc)
    assert source_id(key="a", start_time=t) != source_id(key="b", start_time=t)


def test_source_id_changes_when_second_changes():
    a = source_id(key="x", start_time=datetime(2026, 5, 18, 14, 0, 0, tzinfo=timezone.utc))
    b = source_id(key="x", start_time=datetime(2026, 5, 18, 14, 0, 1, tzinfo=timezone.utc))
    assert a != b


def test_build_event_url_variant(state: State):
    payload = {
        "url": "https://example.com/article",
        "title": "Why I Quit Twitter",
        "og_description": "A reflection.",
        "favicon_url": "https://example.com/fav.ico",
        "category": None,
        "chrome_identity": "redacted@users.noreply.github.com",
        "og_type": "article",
        "lang": "en",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "fulcra-attention-chrome/0.1.0",
    }
    ev = build_attention_event(payload, state=state)
    assert ev["specversion"] == 1
    md = ev["metadata"]
    assert md["data_type"] == "DurationAnnotation"
    assert md["recorded_at"]["start_time"] == "2026-05-18T14:00:00Z"
    assert md["recorded_at"]["end_time"] == "2026-05-18T14:05:00Z"
    assert md["tags"] == ["tag-a", "tag-w"]
    assert md["source"][0].startswith("com.fulcra.attention.v1.")
    assert md["source"][1] == "com.fulcradynamics.annotation.def-att"
    data = json.loads(ev["data"])
    assert data["title"] == "Why I Quit Twitter"
    assert data["url"] == "https://example.com/article"
    assert data["category"] is None
    assert data["og_description"] == "A reflection."
    assert data["favicon_url"] == "https://example.com/fav.ico"
    assert data["service"] == "web"
    assert data["parent_source_id"] is None
    assert data["external_ids"]["host"] == "example.com"
    assert data["external_ids"]["client"] == "fulcra-attention-chrome/0.1.0"
    assert data["external_ids"]["chrome_identity"] == "redacted@users.noreply.github.com"
    assert data["external_ids"]["og_type"] == "article"
    assert data["external_ids"]["lang"] == "en"
    assert data["note"] == "Attention: Why I Quit Twitter"


def test_build_event_omits_unknown_enrichment_fields(state: State):
    """When chrome_identity / og_type / lang are missing from payload,
    they go in external_ids as None — never KeyError."""
    payload = {
        "url": "https://example.com/article",
        "title": "T",
        "category": None,
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(payload, state=state)
    data = json.loads(ev["data"])
    assert data["external_ids"]["chrome_identity"] is None
    assert data["external_ids"]["og_type"] is None
    assert data["external_ids"]["lang"] is None


def test_build_event_category_variant(state: State):
    payload = {
        "url": None,
        "title": None,
        "og_description": None,
        "favicon_url": None,
        "category": "banking",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "fulcra-attention-chrome/0.1.0",
    }
    ev = build_attention_event(payload, state=state)
    data = json.loads(ev["data"])
    assert data["category"] == "banking"
    assert data["url"] is None
    assert data["title"] is None
    assert data["external_ids"].get("host") is None
    assert data["note"] == "Attention: banking"


def test_build_event_source_id_url_keyed_when_url_present(state: State):
    p = {
        "url": "https://example.com/x",
        "title": "x",
        "category": None,
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev1 = build_attention_event(p, state=state)
    ev2 = build_attention_event(p, state=state)
    assert ev1["metadata"]["source"][0] == ev2["metadata"]["source"][0]


def test_build_event_source_id_category_keyed_when_categorized(state: State):
    p = {
        "url": None,
        "title": None,
        "category": "banking",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    assert ev["metadata"]["source"][0].startswith("com.fulcra.attention.v1.")


def test_build_event_strips_fractional_seconds_from_recorded_at(state: State):
    p = {
        "url": "https://x.com/",
        "title": "X",
        "category": None,
        "start_time": "2026-05-18T14:00:00.412Z",
        "end_time":   "2026-05-18T14:05:00.108Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    assert ev["metadata"]["recorded_at"]["start_time"] == "2026-05-18T14:00:00Z"
    assert ev["metadata"]["recorded_at"]["end_time"] == "2026-05-18T14:05:00Z"
