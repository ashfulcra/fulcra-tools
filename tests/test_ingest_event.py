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
    assert data["note"] == "Why I Quit Twitter — https://example.com/article"


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


def test_build_event_url_is_scrubbed_defense_in_depth(state: State):
    """If a client POSTs a URL with an auth-bearing param, it MUST be stripped
    before landing in Fulcra. Defense in depth — the extension also scrubs
    client-side, but the relay enforces."""
    payload = {
        "url": "https://example.com/page?access_token=DEADBEEF&id=42",
        "title": "T",
        "category": None,
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(payload, state=state)
    data = json.loads(ev["data"])
    assert data["url"] == "https://example.com/page?id=42"
    assert "access_token" not in data["url"]


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


# ---------- three-axis tag tests (machine / category / identity) ----------

def test_tags_include_machine_when_hostname_set():
    state = State(
        attention_definition_id="def-att",
        hostname="deskbookpro",
        tag_ids={
            "attention": "tag-a",
            "web": "tag-w",
            "machine:deskbookpro": "tag-m-dbp",
        },
    )
    p = {
        "url": "https://x.com/",
        "title": "X",
        "category": None,
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    assert ev["metadata"]["tags"] == ["tag-a", "tag-w", "tag-m-dbp"]


def test_tags_omit_machine_when_hostname_unset(state: State):
    p = {
        "url": "https://x.com/",
        "title": "X",
        "category": None,
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    assert ev["metadata"]["tags"] == ["tag-a", "tag-w"]


def test_tags_include_category_when_categorized():
    state = State(
        attention_definition_id="def-att",
        tag_ids={
            "attention": "tag-a",
            "web": "tag-w",
            "category:banking": "tag-c-bank",
        },
    )
    p = {
        "url": None,
        "title": None,
        "category": "banking",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    assert ev["metadata"]["tags"] == ["tag-a", "tag-w", "tag-c-bank"]


def test_tags_omit_category_when_uncategorized_tag_cache_missing(state: State):
    """Defensive: if category tag isn't pre-created, gracefully drop the tag
    rather than crashing. The Tier 2 vocab is supposed to be ensured at
    bootstrap, so this only happens for ad-hoc categories or old state."""
    p = {
        "url": None,
        "title": None,
        "category": "made-up-category-not-in-vocab",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    # Only the always-on tags (no machine, no category mapping in state)
    assert ev["metadata"]["tags"] == ["tag-a", "tag-w"]


def test_tags_include_identity_when_cached():
    state = State(
        attention_definition_id="def-att",
        tag_ids={
            "attention": "tag-a",
            "web": "tag-w",
            "identity:redacted@users.noreply.github.com": "tag-i-ash",
        },
    )
    p = {
        "url": "https://x.com/",
        "title": "X",
        "category": None,
        "chrome_identity": "redacted@users.noreply.github.com",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    assert ev["metadata"]["tags"] == ["tag-a", "tag-w", "tag-i-ash"]


def test_tags_all_three_axes_at_once():
    state = State(
        attention_definition_id="def-att",
        hostname="deskbookpro",
        tag_ids={
            "attention": "tag-a",
            "web": "tag-w",
            "machine:deskbookpro": "tag-m-dbp",
            "category:banking": "tag-c-bank",
            "identity:redacted@users.noreply.github.com": "tag-i-ash",
        },
    )
    p = {
        "url": None,
        "title": None,
        "category": "banking",
        "chrome_identity": "redacted@users.noreply.github.com",
        "start_time": "2026-05-18T14:00:00Z",
        "end_time":   "2026-05-18T14:05:00Z",
        "client": "c",
    }
    ev = build_attention_event(p, state=state)
    # Order: always-on first, then machine, category, identity (insertion order).
    assert ev["metadata"]["tags"] == [
        "tag-a", "tag-w", "tag-m-dbp", "tag-c-bank", "tag-i-ash",
    ]
