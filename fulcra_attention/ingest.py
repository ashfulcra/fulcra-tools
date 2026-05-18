# fulcra_attention/ingest.py
"""Build DurationAnnotation events for fulcra-attention.

Converts a single relay-shaped payload to the wire format ingested by
FulcraClient.ingest_batch. Source-id is sha256-derived for idempotency.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .fulcra import build_tag_name
from .scrub import scrub_url
from .state import State

SOURCE_PREFIX = "com.fulcra.attention.v1."


def _parse_iso(s: str) -> datetime:
    """Tolerant ISO-8601 parse. Accepts both 'Z' and '+00:00'."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _to_second_iso(s: str) -> str:
    """Truncate fractional seconds, render with trailing 'Z'."""
    dt = _parse_iso(s).replace(microsecond=0)
    out = dt.isoformat()
    return out.replace("+00:00", "Z")


def source_id(*, key: str, start_time: datetime) -> str:
    """Deterministic source-id derived from `key` (URL or category) and
    `start_time` truncated to the second."""
    sec = start_time.replace(microsecond=0).isoformat()
    h = hashlib.sha256(f"{key}|{sec}".encode()).hexdigest()
    return f"{SOURCE_PREFIX}{h[:16]}"


def build_attention_event(payload: dict, *, state: State) -> dict:
    """Translate a relay-validated payload to a DurationAnnotation wire dict.

    Caller has already enforced: exactly one of {url, category} non-null,
    bearer token, time bounds. We trust the payload here.
    """
    url = payload.get("url")
    if url is not None:
        url = scrub_url(url)
    category = payload.get("category")
    title = payload.get("title")
    og_description = payload.get("og_description")
    favicon_url = payload.get("favicon_url")
    client = payload["client"]
    chrome_identity = payload.get("chrome_identity")
    og_type = payload.get("og_type")
    lang = payload.get("lang")
    start_time = _to_second_iso(payload["start_time"])
    end_time = _to_second_iso(payload["end_time"])

    if url is not None:
        host: str | None = urlsplit(url).hostname
        # Embed URL in note so it's visible in default `fulcra get-records`
        # output (which surfaces `note` but not `data.url`).
        note = f"{title} — {url}" if title else url
        sid_key = url
    else:
        host = None
        note = f"Attention: {category}"
        sid_key = category or ""

    start_dt = _parse_iso(payload["start_time"])
    sid = source_id(key=sid_key, start_time=start_dt)

    data_inner: dict[str, Any] = {
        "note": note,
        "title": title,
        "service": "web",
        "category": category,
        "url": url,
        "og_description": og_description,
        "favicon_url": favicon_url,
        "parent_source_id": None,  # reserved for v2 highlights
        "external_ids": {
            "client": client,
            "host": host,
            "chrome_identity": chrome_identity,
            "og_type": og_type,
            "lang": lang,
        },
    }
    assert state.attention_definition_id, "ensure_definitions() must run first"
    tags = [state.tag_ids["attention"], state.tag_ids["web"]]
    # Three optional axes; each only emitted if the relevant tag UUID is
    # cached in state. machine: set at `setup` time. category: pre-created
    # at bootstrap from CATEGORY_VOCAB. identity: lazy-created by the relay
    # the first time an identity is seen.
    if state.hostname:
        try:
            machine_key = build_tag_name("machine", state.hostname)
            machine_tag = state.tag_ids.get(machine_key)
            if machine_tag:
                tags.append(machine_tag)
        except ValueError:
            pass
    if category:
        try:
            cat_key = build_tag_name("category", category)
            cat_tag = state.tag_ids.get(cat_key)
            if cat_tag:
                tags.append(cat_tag)
        except ValueError:
            pass
    if chrome_identity:
        try:
            id_key = build_tag_name("identity", chrome_identity)
            id_tag = state.tag_ids.get(id_key)
            if id_tag:
                tags.append(id_tag)
        except ValueError:
            pass
    metadata = {
        "data_type": "DurationAnnotation",
        "recorded_at": {
            "start_time": start_time,
            "end_time": end_time,
        },
        "tags": tags,
        "source": [
            sid,
            f"com.fulcradynamics.annotation.{state.attention_definition_id}",
        ],
        "content_type": "application/json",
    }
    return {
        "specversion": 1,
        "data": json.dumps(data_inner, sort_keys=True),
        "metadata": metadata,
    }
