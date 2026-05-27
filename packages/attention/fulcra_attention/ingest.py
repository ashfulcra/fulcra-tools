# fulcra_attention/ingest.py
"""Build DurationAnnotation events for fulcra-attention.

Converts a single extension-shaped payload to a typed `DurationEvent`
that the unified IngestPipeline knows how to POST. Source-id is
sha256-derived for idempotency. The HTTP transport that used to live in
relay.py is gone — the fulcra-collect daemon's `/api/extension/attention`
route now calls `validate_payload()`, then `build_attention_event()` to
get the typed event, and posts via `IngestPipeline.ingest_one(event)`.

Refactor #69 wire-shape decision (Option B): the five attention
extension fields (category / url / og_description / favicon_url /
parent_source_id) stay at top-level `data` — preserved byte-identically
to the legacy shape rather than being routed under `external_ids`. The
DurationEvent's `_emit_attention_fields=True` flag opts the pipeline
into emitting those five keys (with None values when not applicable).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fulcra_common.ingest import DurationEvent

from .fulcra import build_tag_name
from .scrub import scrub_url
from .state import State


_STRING_FIELDS = (
    "url", "category", "title", "og_description", "favicon_url",
    "chrome_identity", "og_type", "lang", "client",
    "start_time", "end_time",
)


def validate_payload(payload: dict) -> None:
    """Raise ValueError with a human-readable message on schema violation.

    The same validator the old standalone relay used. Lifted here so the
    daemon's extension route can share it without depending on the
    deleted relay module.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    required = ("start_time", "end_time", "client")
    missing = [k for k in required if k not in payload]
    if missing:
        raise ValueError(f"missing fields: {missing}")
    # Type-check every string field — refuse ints, lists, dicts in places
    # we expect strings. Closes off a class of accidental
    # crash-via-malformed-payload paths and prevents weird types from
    # flowing into sanitize_tag_value / build_tag_name downstream.
    for k in _STRING_FIELDS:
        v = payload.get(k)
        if v is not None and not isinstance(v, str):
            raise ValueError(
                f"field {k!r} must be string or null, got {type(v).__name__}",
            )
    url = payload.get("url")
    cat = payload.get("category")
    if (url is None) == (cat is None):
        raise ValueError("exactly one of {url, category} must be non-null")
    try:
        st = _parse_iso(payload["start_time"])
        en = _parse_iso(payload["end_time"])
    except ValueError as exc:
        raise ValueError(f"unparseable timestamp: {exc}") from exc
    if st > en:
        raise ValueError("start_time > end_time")
    now = datetime.now(timezone.utc)
    if en > now + timedelta(minutes=5):
        raise ValueError("end_time more than 5 minutes in the future")

# Bumped 2026-05-19 from v1 → v2 so that source_ids generated post-reset
# can never collide with v1 source_ids that are still in Fulcra (under
# the soft-deleted v1 attention definition). v1 events stay visible if
# a query doesn't filter by definition, but v2 events get fresh,
# non-overlapping source_ids and clean dedup behaviour.
SOURCE_PREFIX = "com.fulcra.attention.v2."


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


def build_attention_event(payload: dict, *, state: State) -> DurationEvent:
    """Translate a relay-validated payload to a typed `DurationEvent`.

    Caller has already enforced: exactly one of {url, category} non-null,
    bearer token, time bounds. We trust the payload here.

    The pipeline's `_emit_attention_fields=True` flag is set so the five
    attention-specific top-level data keys (category / url /
    og_description / favicon_url / parent_source_id) land at the top of
    the wire payload — matching the byte-shape the legacy site emitted.
    The #30 defensive `duration_seconds` field is injected by the
    pipeline, not here.
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
    start_dt_sec = _parse_iso(payload["start_time"]).replace(microsecond=0)
    end_dt_sec = _parse_iso(payload["end_time"]).replace(microsecond=0)

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

    return DurationEvent(
        definition_id=state.attention_definition_id,
        source_id=sid,
        tags=tuple(tags),
        external_ids={
            "client": client,
            "host": host,
            "chrome_identity": chrome_identity,
            "og_type": og_type,
            "lang": lang,
        },
        note=note,
        title=title,
        service="web",
        category=category,
        url=url,
        og_description=og_description,
        favicon_url=favicon_url,
        parent_source_id=None,  # reserved for v2 highlights
        _emit_attention_fields=True,
        start=start_dt_sec,
        end=end_dt_sec,
    )
