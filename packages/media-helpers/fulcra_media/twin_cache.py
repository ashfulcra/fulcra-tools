"""Local cache of high-confidence content fingerprints from past imports.

Necessary because Fulcra's read API doesn't surface `external_ids` on event
queries — only `note`, `recorded_at`, `source_id`, `sources`, `tags`. So
cross-batch twin dedup (low-conf vs high-conf same-content match) can't
look up existing high-conf events server-side; we maintain our own index.

Storage: JSON file at ~/.config/fulcra-media/twin_cache.json.
Shape: { "<content_fingerprint>": { "source_id", "importer", "start_time",
         "confidence" } }
Only stores entries whose confidence is "high" (those are the ones a
low-conf incoming twin should defer to).

The cache is populated by run_import-style flows *after* a successful POST
(via record_imported_events). It's queried via load_for_twin_lookup which
returns event-like objects compatible with fulcra_csv.find_low_conf_twins.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_CACHE_PATH = Path(
    os.environ.get("FULCRA_MEDIA_TWIN_CACHE")
    or os.path.expanduser("~/.config/fulcra-media/twin_cache.json")
)


@dataclass
class CachedEvent:
    """Minimal event-like shape that fulcra_csv.find_low_conf_twins accepts.

    Mirrors the attribute set the confidence module duck-types against:
    start_time, end_time, external_ids, source_id.
    """
    start_time: datetime
    source_id: str
    external_ids: dict[str, Any] = field(default_factory=dict)
    end_time: datetime | None = None
    timestamp_confidence: str = "high"


def _resolve_path(cache_path: Path | None) -> Path:
    """Look up the cache path at call time so monkeypatch on
    DEFAULT_CACHE_PATH actually works (a default arg binds at def time)."""
    return cache_path if cache_path is not None else DEFAULT_CACHE_PATH


def load(cache_path: Path | None = None) -> dict[str, dict]:
    """Read the raw cache dict. Returns {} if missing or corrupt."""
    cache_path = _resolve_path(cache_path)
    if not cache_path.exists():
        return {}
    try:
        return json.loads(cache_path.read_text())
    except (ValueError, OSError):
        return {}


def save(cache: dict[str, dict], cache_path: Path | None = None) -> None:
    """Persist the cache JSON, creating the dir if needed."""
    cache_path = _resolve_path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2, sort_keys=True))


def _source_id_of(ev: Any) -> str:
    """Read the source-id field, whichever name the event uses.

    fulcra_csv.GenericEvent calls it `source_id`; fulcra_media.NormalizedEvent
    calls it `deterministic_id`. Both mean the same thing — the per-row
    dedup key Fulcra sees in the ingest payload's `source` array.
    """
    return getattr(ev, "source_id", None) or getattr(ev, "deterministic_id")


def record_imported_events(events: Iterable[Any],
                           cache_path: Path | None = None) -> int:
    """Add high-confidence events with a content_fingerprint to the cache.

    Returns the number of new (or updated) cache entries written.

    Events must expose: external_ids (dict), source_id or deterministic_id
    (str), start_time (datetime). NormalizedEvent and GenericEvent both qualify.
    """
    cache_path = _resolve_path(cache_path)
    cache = load(cache_path)
    added = 0
    for ev in events:
        ext = getattr(ev, "external_ids", None) or {}
        confidence = (
            ext.get("timestamp_confidence")
            or getattr(ev, "timestamp_confidence", None)
        )
        if confidence != "high":
            continue
        fp = ext.get("content_fingerprint")
        if not fp:
            continue
        importer = getattr(ev, "importer", None) or ext.get("importer")
        cache[fp] = {
            "source_id": _source_id_of(ev),
            "importer": importer,
            "start_time": ev.start_time.isoformat(),
            "confidence": "high",
        }
        added += 1
    save(cache, cache_path)
    return added


def load_for_twin_lookup(cache_path: Path | None = None) -> list[CachedEvent]:
    """Return cache entries as CachedEvent objects ready for find_low_conf_twins.

    The fingerprint is mirrored into external_ids[content_fingerprint] so the
    twin matcher can find them by key without special-casing the cache shape.
    """
    cache_path = _resolve_path(cache_path)
    cache = load(cache_path)
    out: list[CachedEvent] = []
    for fp, entry in cache.items():
        ts_raw = entry.get("start_time")
        if not ts_raw:
            continue
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ext = {
            "content_fingerprint": fp,
            "timestamp_confidence": entry.get("confidence", "high"),
        }
        if "importer" in entry:
            ext["importer"] = entry["importer"]
        out.append(CachedEvent(
            start_time=ts,
            source_id=entry.get("source_id", f"cache:{fp}"),
            external_ids=ext,
        ))
    return out


def clear(cache_path: Path | None = None) -> None:
    """Drop the cache file entirely (used by `fulcra-media reset`)."""
    cache_path = _resolve_path(cache_path)
    if cache_path.exists():
        cache_path.unlink()
