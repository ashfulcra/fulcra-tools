"""Tests for the typed-ingest schema-drift detector.

The typed endpoint (POST /ingest/v1/record/{base_type}) SILENTLY strips
unknown keys and defaults missing ones (live-verified 2026-07-08), so a
wire/schema drift never errors at the API — it quietly loses data. These
tests pin the stdlib-only checker that surfaces that drift.
"""
from __future__ import annotations

import httpx
import pytest

from fulcra_common.client import BaseFulcraClient
from fulcra_common.schema_check import (
    check_payload_against_schema,
    fetch_record_schema,
)

MOMENT_SCHEMA = {  # shape captured live 2026-07-08
    "properties": {"id": {}, "tags": {}, "sources": {},
                   "recorded_at": {}, "note": {}},
    "required": [],
}
NUMERIC_SCHEMA = {"properties": {"id": {}, "tags": {}, "sources": {},
                                 "value": {}, "unit": {},
                                 "recorded_at": {}, "note": {}},
                  "required": ["value"]}


def test_unknown_key_is_flagged_as_will_be_stripped():
    problems = check_payload_against_schema(
        {"recorded_at": "2026-07-08T21:00:00Z", "data": "{}"}, MOMENT_SCHEMA)
    assert any("data" in p and "stripped" in p for p in problems)


def test_missing_required_flagged():
    problems = check_payload_against_schema({"note": "x"}, NUMERIC_SCHEMA)
    assert any("value" in p and "required" in p for p in problems)


def test_clean_payload_no_problems():
    assert check_payload_against_schema(
        {"value": 1.0, "sources": ["s"]}, NUMERIC_SCHEMA) == []


def test_wrong_primitive_types_flagged():
    """Shallow type check: value must be a number, note a string, and
    tags/sources arrays. A bool is not a number (JSON distinguishes them)."""
    problems = check_payload_against_schema(
        {"value": "not-a-number", "note": ["x"], "sources": "s", "tags": {}},
        NUMERIC_SCHEMA)
    assert any("value" in p and "number" in p for p in problems)
    assert any("note" in p and "string" in p for p in problems)
    assert any("sources" in p and "array" in p for p in problems)
    assert any("tags" in p and "array" in p for p in problems)
    # a bool must not pass as a number
    assert check_payload_against_schema({"value": True}, NUMERIC_SCHEMA) != []


def test_fetch_record_schema_hits_catalog_endpoint(monkeypatch, recording_transport):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "tok-1")

    def handler(request):
        assert str(request.url).endswith(
            "/data/v1/catalog/MomentAnnotation/v1alpha1/schema")
        assert request.headers["Authorization"] == "Bearer tok-1"
        return httpx.Response(200, json=MOMENT_SCHEMA)

    client = BaseFulcraClient(transport=recording_transport(handler))
    assert fetch_record_schema(client, "MomentAnnotation") == MOMENT_SCHEMA


def test_fetch_record_schema_raises_on_non_200(monkeypatch, recording_transport):
    monkeypatch.setenv("FULCRA_ACCESS_TOKEN", "tok-1")
    client = BaseFulcraClient(
        transport=recording_transport(lambda r: httpx.Response(404)))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_record_schema(client, "MomentAnnotation")
