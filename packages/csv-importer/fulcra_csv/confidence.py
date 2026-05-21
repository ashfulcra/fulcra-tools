"""Confidence-aware preprocessing for imports.

Two rules implemented as pure functions; callers (CLI) own user interaction.

Rule 1 — apply_cluster_policy: bulk-handle events flagged with
`timestamp_cluster_size` ≥ threshold. Drop / sentinel-date / keep.

Rule 2 — find_low_conf_twins + apply_twin_decisions: pair low-confidence
events with high-confidence ones sharing a `content_fingerprint` (or
caller-specified twin key), so the CLI can ask the user before discarding
the low-conf side.

Both operate on any event-like dataclass exposing .start_time, .end_time,
.external_ids, and .source_id — works on fulcra_csv.GenericEvent and
fulcra_media.NormalizedEvent alike. Confidence is read from external_ids
first, then falls back to a top-level .timestamp_confidence attribute.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

VALID_CLUSTER_ACTIONS = {"drop", "sentinel", "keep"}


def confidence_of(ev: Any) -> str | None:
    """Read timestamp_confidence from external_ids first, then top-level attr."""
    ext = getattr(ev, "external_ids", None) or {}
    if "timestamp_confidence" in ext:
        return ext["timestamp_confidence"]
    return getattr(ev, "timestamp_confidence", None)


def cluster_size_of(ev: Any) -> int:
    """Read timestamp_cluster_size from external_ids; 0 if absent."""
    ext = getattr(ev, "external_ids", None) or {}
    v = ext.get("timestamp_cluster_size")
    return int(v) if v is not None else 0


@dataclass(frozen=True)
class ClusterPolicy:
    """How to handle events marked as cluster members.

    action: 'drop' removes them; 'keep' passes through; 'sentinel' shifts
        each member's start_time to Jan 1 of sentinel_year, 1ms apart in
        original-timestamp order, preserving duration.
    cluster_size_threshold: only events with
        external_ids['timestamp_cluster_size'] >= this are affected.
    """
    action: str
    sentinel_year: int = 2010
    cluster_size_threshold: int = 5

    def __post_init__(self) -> None:
        if self.action not in VALID_CLUSTER_ACTIONS:
            raise ValueError(
                f"action must be one of {VALID_CLUSTER_ACTIONS}, got {self.action!r}"
            )
        if self.action == "sentinel" and not (1970 <= self.sentinel_year <= 2100):
            raise ValueError(f"sentinel_year out of range: {self.sentinel_year}")


def apply_cluster_policy(events: list[Any], policy: ClusterPolicy) -> list[Any]:
    """Apply the cluster policy, returning a new list.

    Sentinel notes:
      - start_time shifts to Jan 1, policy.sentinel_year UTC, +1ms per event
        in original-timestamp order. end_time preserves duration.
      - external_ids gains `original_timestamp` (ISO) and `sentinel_applied`=True.
      - source_id is NOT recomputed — it was hashed against the original
        timestamp, so leaving it alone keeps re-runs of the same input
        idempotent. The original timestamp lives on in external_ids.
    """
    if policy.action == "keep":
        return list(events)
    if policy.action == "drop":
        return [e for e in events
                if cluster_size_of(e) < policy.cluster_size_threshold]

    # sentinel
    sentinel_base = datetime(policy.sentinel_year, 1, 1, tzinfo=timezone.utc)
    cluster_indices = [
        i for i, e in enumerate(events)
        if cluster_size_of(e) >= policy.cluster_size_threshold
    ]
    cluster_indices.sort(key=lambda i: events[i].start_time)
    new_start_for: dict[int, datetime] = {
        i: sentinel_base + timedelta(milliseconds=offset)
        for offset, i in enumerate(cluster_indices)
    }

    out: list[Any] = []
    for i, e in enumerate(events):
        if i not in new_start_for:
            out.append(e)
            continue
        new_start = new_start_for[i]
        new_end = None
        if e.end_time is not None:
            duration = e.end_time - e.start_time
            new_end = new_start + duration
        new_external = dict(e.external_ids)
        new_external["original_timestamp"] = e.start_time.isoformat()
        new_external["sentinel_applied"] = True
        out.append(replace(e, start_time=new_start, end_time=new_end,
                           external_ids=new_external))
    return out


def find_low_conf_twins(
    events: Iterable[Any],
    *,
    twin_key: str = "content_fingerprint",
    extra_pool: Iterable[Any] | None = None,
) -> list[tuple[Any, Any]]:
    """Pair low-confidence events with a high-confidence twin sharing
    external_ids[twin_key].

    `events`: the incoming batch.
    `extra_pool`: optional already-ingested events whose external_ids the
        caller has access to (e.g. from a local cache). The Fulcra read
        API does NOT currently surface external_ids, so cross-batch twin
        dedup requires the caller to maintain its own cache.

    Returns: list of (low_conf, high_conf) pairs. A single high-conf event
    can be the twin of multiple low-conf events. Pairs are stable on input
    iteration order so prompts are deterministic.
    """
    events = list(events)
    extra = list(extra_pool or [])

    high_conf_by_key: dict[str, Any] = {}
    for pool in (extra, events):
        for ev in pool:
            if confidence_of(ev) == "high":
                k = (ev.external_ids or {}).get(twin_key)
                if k and k not in high_conf_by_key:
                    high_conf_by_key[k] = ev

    pairs: list[tuple[Any, Any]] = []
    for ev in events:
        if confidence_of(ev) != "low":
            continue
        k = (ev.external_ids or {}).get(twin_key)
        if k and k in high_conf_by_key:
            pairs.append((ev, high_conf_by_key[k]))
    return pairs


def apply_twin_decisions(
    events: list[Any],
    discard_source_ids: set[str],
) -> list[Any]:
    """Filter out events whose source_id is in discard_source_ids."""
    return [e for e in events if e.source_id not in discard_source_ids]
