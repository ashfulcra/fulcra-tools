"""Tag + definition bootstrap (idempotent)."""
from __future__ import annotations

import json

import httpx
import pytest

from fulcra_attention.fulcra import CATEGORY_VOCAB, FulcraClient, sanitize_tag_value
from fulcra_attention.state import State


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Fulcra accepts `[a-z0-9._-]` in tag names (probed empirically:
        # `_` returns 303, `@` returns 422). `@` is replaced with `-`.
        ("ash@fulcradynamics.com", "ash-fulcradynamics.com"),
        ("ASH@FulcraDynamics.com", "ash-fulcradynamics.com"),
        ("  spaces  ", "spaces"),
        ("Desk Book Pro", "desk-book-pro"),
        # Underscores are allowed verbatim — only dashes collapse.
        ("multi___under_score", "multi___under_score"),
        ("--leading-and-trailing--", "leading-and-trailing"),
        ("já-acentos-ño!", "j-acentos-o"),
        ("", ""),
    ],
)
def test_sanitize_tag_value_collapses_disallowed_chars(raw: str, expected: str):
    assert sanitize_tag_value(raw) == expected


from fulcra_attention.fulcra import TAG_NAME_MAX, build_tag_name


def test_build_tag_name_short_value_no_hash():
    assert build_tag_name("category", "banking") == "category:banking"
    assert build_tag_name("machine", "deskbookpro") == "machine:deskbookpro"


def test_build_tag_name_truncates_with_deterministic_suffix():
    """A too-long value gets truncated + 6-char sha256 suffix so distinct
    long values don't collide on the same truncated head."""
    long = "ash@fulcradynamics.com"  # 22 chars raw; over budget with `identity:` prefix
    name = build_tag_name("identity", long)
    assert len(name) <= TAG_NAME_MAX
    assert name.startswith("identity:")
    # Deterministic: same input → same output.
    assert build_tag_name("identity", long) == name
    # Different input → different suffix (even if heads happen to match).
    other = build_tag_name("identity", "ash@example-organization.com")
    assert other != name


def test_build_tag_name_empty_value_raises():
    with pytest.raises(ValueError):
        build_tag_name("identity", "!!!")  # sanitises to empty


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


def test_ensure_definitions_creates_attention_def_and_vocab(recording_transport):
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
    posted_names = {t["name"] for t in posted_tags}
    assert "attention" in posted_names
    assert "web" in posted_names
    # All Tier-2 vocab tags are pre-created so filters/timelines work
    # before any domain has been categorized.
    for slug in CATEGORY_VOCAB:
        assert f"category:{slug}" in posted_names, f"missing vocab tag for {slug}"
    assert len(posted_defs) == 1
    d = posted_defs[0]
    assert d["name"] == "Attention"
    assert d["annotation_type"] == "duration"
    # The Attention def only references attention + web — category tags
    # apply per-event, not at the def level.
    assert "tag-attention" in d["tags"] and "tag-web" in d["tags"]
    assert len(d["tags"]) == 2


def test_ensure_definitions_skips_def_post_when_already_cached(recording_transport):
    """Def re-creation is skipped, but vocab tags are still re-ensured (cache hit only)."""
    pre_cached = {"attention": "a", "web": "w"}
    for slug in CATEGORY_VOCAB:
        pre_cached[f"category:{slug}"] = f"t-{slug}"

    def responder(r: httpx.Request) -> httpx.Response:
        # Should never be called because everything is cached.
        raise AssertionError(f"unexpected {r.method} {r.url}")

    transport = recording_transport(responder)
    client = FulcraClient(transport=transport)
    state = State(attention_definition_id="def-x", tag_ids=pre_cached)
    client.ensure_definitions(state)
    assert state.attention_definition_id == "def-x"
