"""Guarded v1 media import runtime, paired with the per-run Collect session.

The caller opens one CollectRecordSession, resumes pending batches BEFORE its
source fetch, and uses this writer for the normalized results. Existing legacy
CLI/plugin call sites are switched separately with their setup/readback paths.
"""
from __future__ import annotations

import json
import hashlib
from collections import defaultdict
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace
from threading import RLock
from uuid import UUID

from fulcra_collect.v1_outbox import V1RecordOutbox, has_pending_records
from fulcra_collect.v1_session import CollectRecordSession
from fulcra_collect.config import settings_fingerprint
from fulcra_common.client import ImportResult
from fulcra_common.v1 import _destination, record_matches, _utc
from fulcra_common.data_types import tag_create_name
from fulcra_csv import find_low_conf_twins

from .collect_twins import MediaTwinIndex
from .fulcra import _importer_namespace_prefix
from .v1 import build_media_record, media_spec

_PREFLIGHT_TYPE = "Event/00000000-0000-0000-0000-000000000000"
_DESTINATIONS = "v1_media_destinations"


def media_destinations(*categories):
    """Plugin setup contract; a generic source uses its saved category."""
    def specs(settings):
        selected = categories or (settings.get("category"),)
        return {category: media_spec(category) for category in selected}
    return specs


def media_pending(ctx):
    return has_pending_records(ctx, key="v1_media_pending")


def _cursor_scope(ctx, session):
    persisted = getattr(ctx, "_persisted_config", None)
    return {"account_id": session.account_id, "epoch": ctx.config_epoch,
            "settings_fingerprint": settings_fingerprint(ctx.config if persisted is None else persisted)}


def _watermark(value):
    if not isinstance(value, str):
        raise ValueError("media v1 cursor is malformed; reconcile before collecting")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
    except ValueError:
        raise ValueError("media v1 cursor is malformed; reconcile before collecting") from None
    return value


def media_run(function):
    """Bind intake, pending recovery, writes and progress to one Collect run.

    Legacy watermarks prove only legacy delivery. V1 cursors are private KV
    envelopes scoped to account, consent epoch and raw saved selection. Restore
    legacy state even on failure so the parent worker cannot persist a stale v1
    watermark through its older unscoped result protocol.
    """
    @wraps(function)
    def run(ctx):
        with CollectRecordSession(ctx) as session:
            writer = MediaRecordWriter(ctx, session)
            ctx._media_record_writer = writer
            legacy_watermark = ctx.state.watermark
            try:
                preview = False
                for setting in ("dry_run", "preview_only"):
                    value = ctx.config.get(setting, False)
                    if type(value) is not bool:
                        raise ValueError("Preview only must be on or off")
                    preview |= value
                scope = _cursor_scope(ctx, session)
                key = "v1_media_cursor:" + hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()
                saved = ctx.kv_get(key, None)
                if saved is not None and (not isinstance(saved, dict)
                        or any(saved.get(field) != value for field, value in scope.items())):
                    raise ValueError("media v1 cursor scope is malformed; reconcile before collecting")
                ctx.state.watermark = _watermark(saved.get("watermark")) if saved is not None else None
                if not preview:
                    writer.resume()
                from fulcra_collect.trakt_source import current_work_scope
                current = (current_work_scope(ctx, session, session._source_local_guard)
                           if getattr(ctx, "plugin_id", None) == "trakt" else nullcontext())
                with current:
                    result = function(ctx)
                    if not preview and ctx.state.watermark is not None:
                        cursor = {**scope, "watermark": _watermark(ctx.state.watermark)}
                        session.commit(lambda: ctx.kv_set(key, cursor))
                    return result
            finally:
                ctx.state.watermark = legacy_watermark
                del ctx._media_record_writer
    return run


def _external_ids(prior, incoming):
    result = deepcopy(prior)
    for key, value in incoming.items():
        result[key] = _external_ids(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else deepcopy(value)
    return result


def _preserve_enrichment(current, record, declared):
    sources = current.get("sources")
    if (not isinstance(sources, list) or any(not isinstance(s, str) for s in sources)
            or record["sources"][0] not in sources):
        raise ValueError("existing media UUID has an incompatible primary identity; reconcile before writing")
    tags = current.get("tags", [])
    external = current.get("external_ids") or {}
    try:
        if not isinstance(tags, list) or not isinstance(external, dict):
            raise ValueError
        if any(str(UUID(tag)) != tag for tag in tags):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("existing media enrichment is malformed; reconcile before writing") from None
    merged = {**{key: deepcopy(value) for key, value in current.items() if key in declared}, **record}
    merged["tags"] = list(dict.fromkeys([*tags, *record["tags"]]))
    merged["external_ids"] = _external_ids(external, record["external_ids"])
    # Recomputed time-bucket fingerprints are source-owned. Other old source
    # identities/bindings remain provenance when the source record is updated.
    merged["sources"] = list(dict.fromkeys([*record["sources"],
        *(source for source in sources if not source.startswith("com.fulcra.content."))]))
    return merged


def _different_importer(event, row):
    """Skip only a proven other-source twin; opaque IDs are not evidence."""
    prefix = _importer_namespace_prefix(event.deterministic_id)
    if prefix and any(source.startswith(prefix) for source in row["sources"]):
        return False
    importer = row.get("importer")
    if isinstance(importer, str) and importer:
        return importer != event.importer
    prefixes = {_importer_namespace_prefix(source) for source in row["sources"]}
    prefixes.discard(None)
    return prefix is not None and bool(prefixes) and prefix not in prefixes


@dataclass(frozen=True)
class _ImportProgress:
    """Observed lower bounds; preview posted means would-post, verified is zero."""

    total: int = 0
    skipped_existing: int = 0
    posted: int = 0
    verified: int = 0


@dataclass(frozen=True, slots=True)
class _TwinPair:
    account: str
    destination: str
    category: str
    incoming: str
    high: str
    fingerprint: str
    origin: str


@dataclass(frozen=True, slots=True)
class _TwinOffer:
    binding: object
    pairs: tuple[_TwinPair, ...]


class MediaRecordWriter:
    def __init__(self, ctx, session):
        self.ctx = ctx
        self.session = session
        self.outbox = V1RecordOutbox(ctx, session, key="v1_media_pending")
        self.schemas = {}
        self.default_twin_policy = "keep"
        # Webhook requests share this writer across request threads. The
        # plugin's one-worker rule alone cannot serialize its single outbox.
        self._lock = RLock()
        self._destination_ids = {}
        self._progress = None
        self._twin_binding = object()

    @property
    def progress(self):
        """Last serialized invocation, not a caller-bound receipt.

        Shared webhook callers must not use this property for attribution: a
        later invocation can replace it before an earlier caller reads it.
        Reads require no session authority, even after context cleanup.
        """
        with self._lock:
            return self._progress

    @property
    def destination_ids(self):
        with self._lock:
            return dict(self._destination_ids)

    def prepare(self, categories):
        """Prove a service's owned destinations before it starts listening."""
        with self._lock:
            preview = any(self.ctx.config.get(key) is True for key in ("dry_run", "preview_only"))
            result = self._destinations(set(categories), check_only=preview)
            self.session.prepare_record_policy(check_only=preview)
            return result

    def resume(self) -> int:
        """Reconcile captured data before reading a new source sample."""
        with self._lock:
            return self.outbox.drain()

    def inspect_twins(self, events):
        """Capture immutable, read-only proof for this writer's offered pairs."""
        with self._lock:
            unique = {(e.category, e.deterministic_id): e for e in events}
            events = sorted(unique.values(), key=lambda e: _utc(e.start_time))
            destinations = self._destinations({e.category for e in events}, check_only=True)
            return _TwinOffer(self._twin_binding, self._twin_pairs(events, destinations))

    def _validate_offer(self, offer, events, policy):
        if offer is None:
            return
        if (type(offer) is not _TwinOffer or offer.binding is not self._twin_binding
                or type(offer.pairs) is not tuple or policy != "auto-discard"):
            raise ValueError("invalid media twin approval")
        scope = {(e.category, e.deterministic_id) for e in events}
        seen = set()
        for pair in offer.pairs:
            if (type(pair) is not _TwinPair
                    or any(type(v) is not str or not v for v in (pair.account, pair.destination, pair.category, pair.incoming,
                        pair.high, pair.fingerprint, pair.origin))
                    or pair.account != self.session.account_id
                    or pair.origin not in {"existing-owned-record", "current-input"}
                    or (pair.category, pair.incoming) not in scope
                    or pair in seen):
                raise ValueError("invalid media twin approval")
            _destination(pair.destination, "Event")
            if pair.origin == "existing-owned-record" and str(UUID(pair.high)) != pair.high:
                raise ValueError("invalid media twin approval")
            if (pair.origin == "current-input" and
                    (not pair.high.startswith(pair.category + ":")
                     or not pair.high[len(pair.category) + 1:])):
                raise ValueError("invalid media twin approval")
            seen.add(pair)
        # Resolve and check scope without creating metadata or mutating indexes.
        destinations = self._destinations({e.category for e in events}, check_only=True)
        if any(destinations.get(p.category) != p.destination for p in offer.pairs):
            raise ValueError("invalid media twin approval scope")

    def _twin_pairs(self, events, destinations, *, check_only=True):
        index = MediaTwinIndex()
        pairs = []
        for category, destination in sorted(destinations.items()):
            candidates = [event for event in events if event.category == category]
            fingerprints = [event.external_ids.get("content_fingerprint") for event in candidates
                            if event.timestamp_confidence == "low" and event.external_ids.get("content_fingerprint")]
            hints = index.lookup(self.session.account_id, destination, fingerprints)
            rows = self.session.get_records(destination, list(dict.fromkeys(hints.values()))) if hints else {}
            current_ids = {e.deterministic_id: build_media_record(e, destination)["id"]
                           for e in candidates}
            identifiers = list(dict.fromkeys([*rows, *current_ids.values()]))
            excluded = self.session.excluded_ids(destination, identifiers, check_only=check_only) if identifiers else set()
            pool = []
            identities = {}
            for fingerprint, identifier in hints.items():
                row = rows.get(identifier)
                if (identifier not in excluded and row is not None and row.get("id") == identifier
                        and row.get("timestamp_confidence") == "high"
                        and isinstance(row.get("external_ids"), dict)
                        and row["external_ids"].get("content_fingerprint") == fingerprint):
                    high = SimpleNamespace(timestamp_confidence="high", external_ids={
                        "content_fingerprint": fingerprint, "timestamp_confidence": "high"})
                    pool.append(high)
                    identities[id(high)] = (identifier, "existing-owned-record")
            eligible = [e for e in candidates if current_ids[e.deterministic_id] not in excluded]
            for low, high in find_low_conf_twins(eligible, extra_pool=pool):
                identity, origin = identities.get(id(high),
                    (category + ":" + getattr(high, "deterministic_id", ""), "current-input"))
                pairs.append(_TwinPair(self.session.account_id, destination, category,
                    low.deterministic_id, identity, low.external_ids["content_fingerprint"], origin))
        return tuple(pairs)

    def _filter_twins(self, events, destinations, policy, *, check_only=False, approved_twins=None):
        if policy == "keep":
            return events
        pairs = self._twin_pairs(events, destinations, check_only=check_only)
        approved = None if approved_twins is None else set(approved_twins.pairs)
        discard = {(p.category, p.incoming) for p in pairs if approved is None or p in approved}
        return [e for e in events if (e.category, e.deterministic_id) not in discard]

    def _destinations(self, categories, *, check_only):
        cached = self.ctx.kv_get(_DESTINATIONS, {}) or {}
        if not isinstance(cached, dict):
            raise ValueError("media v1 destination cache is malformed")
        pins = cached.get("ids", {}) if cached.get("account_id") == self.session.account_id else {}
        configured = self.ctx.config.get("v1_destination_ids", {})
        if not isinstance(configured, dict) or not isinstance(pins, dict):
            raise ValueError("media v1 destination mapping is malformed")
        for category, pin in configured.items():
            media_spec(category)
            _destination(pin, "Event")
        legacy = getattr(getattr(self.ctx, "state", None), "definition_id", None)
        known_legacy = cached.get("legacy_definition_id") if cached.get("account_id") == self.session.account_id else None
        if legacy and any(category not in configured and
                          (known_legacy != legacy or category not in pins) for category in categories):
            raise RuntimeError("map the existing annotation selection to v1 before collecting")
        destinations = {}
        for category in sorted(categories):
            resolved = self.session.resolve_destination(
                media_spec(category), pinned_id=configured.get(category, pins.get(category)),
                allow_create=not check_only and category not in configured,
            )
            destinations[category] = resolved.data_type
            self.schemas[resolved.data_type] = resolved.schema
        if not check_only:
            self.session.commit(lambda: self.ctx.kv_set(_DESTINATIONS,
                {"account_id": self.session.account_id, "ids": {**pins, **destinations},
                 "legacy_definition_id": legacy}))
        self._destination_ids.update(destinations)
        return destinations

    def run_import(self, events, *, chunk_size: int = 500, check_only: bool = False,
                   twin_policy=None, approved_twins=None) -> ImportResult:
        with self._lock:
            self._progress = _ImportProgress()
            return self._run_import(events, chunk_size=chunk_size, check_only=check_only,
                                    twin_policy=twin_policy, approved_twins=approved_twins)

    def _run_import(self, events, *, chunk_size: int, check_only: bool,
                    twin_policy=None, approved_twins=None) -> ImportResult:
        """Preserve true source replays and suppress proven cross-source twins.

        Each chunk is immutable/durable before its POST. A rejected line,
        indexing delay or unknown response raises with the pending batch intact;
        no successful result, receipt or caller cursor can precede visibility.
        """
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("media chunk size must be a positive integer")
        if type(check_only) is not bool:
            raise ValueError("check_only must be on or off")
        policy = self.ctx.config.get("twin_policy", self.default_twin_policy) if twin_policy is None else twin_policy
        if policy not in {"keep", "auto-discard"}:
            raise ValueError("headless twin_policy must be keep or auto-discard")
        events = list(events)
        self._validate_offer(approved_twins, events, policy)
        self._progress = _ImportProgress(total=len(events))
        # Detect collisions/non-JSON enrichment across the WHOLE input before
        # destination or tag creation, including conflicts in later chunks.
        unique = {}
        services = set()
        for event in events:
            payload = build_media_record(event, _PREFLIGHT_TYPE)
            if event.service not in services:
                tag_create_name(event.service)
                services.add(event.service)
            key = event.category, event.deterministic_id
            snapshot = json.dumps(payload, sort_keys=True, allow_nan=False)
            if key in unique and unique[key][1] != snapshot:
                raise ValueError("conflicting media payloads for the same primary identity")
            unique[key] = event, snapshot
        skipped = len(events) - len(unique)
        posted = verified = 0
        self._progress = _ImportProgress(len(events), skipped, posted, verified)
        if not check_only:
            self.resume()
        if not events:
            return ImportResult(0, 0, 0, 0)
        ordered = sorted((value[0] for value in unique.values()), key=lambda event: _utc(event.start_time))
        destinations = self._destinations({event.category for event in ordered}, check_only=check_only)
        ordered = self._filter_twins(ordered, destinations, policy, check_only=check_only, approved_twins=approved_twins)
        skipped = len(events) - len(ordered)
        self._progress = _ImportProgress(len(events), skipped, posted, verified)
        tags = {service: self.session.resolve_tag(service, allow_create=not check_only)
                for service in sorted({event.service for event in ordered})}
        planned = defaultdict(list)
        for offset in range(0, len(ordered), chunk_size):
            with self.session.record_write_scope() if not check_only else nullcontext():
                chunk = ordered[offset:offset + chunk_size]
                groups = defaultdict(list)
                for event in chunk:
                    groups[destinations[event.category]].append((event, build_media_record(
                        event, destinations[event.category], tags=(tags[event.service],))))
                pending = {}
                confirmed = defaultdict(list)
                for destination, candidates in groups.items():
                    excluded = self.session.excluded_ids(destination, [r["id"] for _, r in candidates], check_only=check_only)
                    skipped += sum(r["id"] in excluded for _, r in candidates)
                    self._progress = _ImportProgress(len(events), skipped, posted, verified)
                    candidates = [(e, r) for e, r in candidates if r["id"] not in excluded]
                    if not candidates:
                        continue
                    actual = self.session.get_records(destination, [record["id"] for _, record in candidates])
                    source_bindings = list(dict.fromkeys(source for event, record in candidates
                        if record["id"] not in actual for source in (event.deterministic_id, *event.extra_source_ids)))
                    existing = self.session.get_source_records(destination, source_bindings,
                        start=min(_utc(event.start_time) for event, _ in candidates) - timedelta(minutes=10),
                        end=max(_utc(event.end_time) for event, _ in candidates) + timedelta(minutes=10)) if source_bindings else []
                    existing.extend(planned[destination])
                    excluded_twins = self.session.excluded_ids(destination,
                        list(dict.fromkeys(row["id"] for row in existing)), check_only=check_only) if existing else set()
                    existing = [row for row in existing if row["id"] not in excluded_twins]
                    # Include accepted rows as this chunk progresses: same-importer
                    # collisions are real replays; different-importer twins skip.
                    for event, record in candidates:
                        current = actual.get(record["id"])
                        if current is not None:
                            record = _preserve_enrichment(current, record, self.schemas[destination]["properties"])
                        if record_matches(current, record):
                            skipped += 1
                            self._progress = _ImportProgress(len(events), skipped, posted, verified)
                            existing.append(current)
                            confirmed[destination].append(current)
                            continue
                        if current is None:
                            if any(event.deterministic_id in row["sources"] for row in existing):
                                # A copied legacy record can bind several primary
                                # source aliases. Its existing owned row already
                                # covers this exact source identity.
                                skipped += 1
                                self._progress = _ImportProgress(len(events), skipped, posted, verified)
                                continue
                            matched = [row for row in existing if set(row["sources"]) & set(event.extra_source_ids)]
                            if any(_different_importer(event, row) for row in matched):
                                skipped += 1
                                self._progress = _ImportProgress(len(events), skipped, posted, verified)
                                continue
                        pending.setdefault(destination, []).append(record)
                        existing.append(record)
                        planned[destination].append(record)
                count = sum(len(records) for records in pending.values())
                if check_only:
                    posted += count
                    self._progress = _ImportProgress(len(events), skipped, posted, verified)
                if count and not check_only:
                    observed = max(_utc(event.start_time) for event in chunk).isoformat()
                    self.outbox.stage(pending, message=f"Media: {count} records verified", observed_at=observed)
                    baseline = self._progress
                    try:
                        self.outbox.drain()
                    finally:
                        evidence = self.outbox.progress
                        posted = baseline.posted + evidence.verified
                        verified = baseline.verified + evidence.verified
                        skipped = baseline.skipped_existing + evidence.excluded
                        self._progress = _ImportProgress(len(events), skipped, posted, verified)
                    for destination, records in pending.items():
                        confirmed[destination].extend(r for r in records
                            if r["id"] not in self.outbox.excluded_ids.get(destination, set()))
                if not check_only:
                    for destination, records in confirmed.items():
                        self.session.commit(lambda: MediaTwinIndex().record(self.session.account_id, destination, records))
        return ImportResult(len(events), skipped, posted, verified)
