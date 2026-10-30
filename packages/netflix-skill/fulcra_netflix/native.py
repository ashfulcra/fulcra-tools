"""Installed Netflix adapter over the shared native Event session/outbox.

Only the in-flight <=500-record chunk is durable. A later invocation recovers
that original payload before reading its source; unstarted input needs the
original CSV again. Counts are immutable complete-group lower bounds.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
from importlib import import_module

CONTRACT_VERSION = 1
PENDING = "v1_netflix_pending"


def require_runtime():
    """Prove installed public contracts without constructing clients/context."""
    try:
        runtime = import_module("fulcra_collect.standalone_v1")
        outbox = import_module("fulcra_collect.v1_outbox").V1RecordOutbox
        v1 = import_module("fulcra_common.v1")
        media = import_module("fulcra_media.v1")
        if (type(runtime.STANDALONE_RUNTIME_VERSION) is not int
                or runtime.STANDALONE_RUNTIME_VERSION != 1
                or not callable(runtime.standalone_namespace_supported)
                or runtime.standalone_namespace_supported("cli-netflix") is not True
                or not all(callable(value) for value in (
                    runtime.standalone_runtime, outbox.stage, outbox.drain,
                    outbox.pending_destinations, v1.build_event_record,
                    v1.record_matches, v1.V1DataClient.validate_records, media.media_spec))
                or not isinstance(outbox.progress, property)):
            raise ValueError
    except Exception:
        raise RuntimeError(
            "Matching installed Netflix/Collect runtime required; use the pinned checkout environment."
        ) from None


def authority(destination):
    from fulcra_media.v1 import media_spec

    return {"destination": destination, "spec": media_spec("watched").fingerprint,
            "codec": "netflix.v1"}


def build_netflix_record(event, destination, variant):
    """Retain original identity/note/provenance without media note/tag policy."""
    from fulcra_common.v1 import build_event_record

    if variant not in ("slim", "rich"):
        raise ValueError("invalid Netflix variant")
    duration = (event.end - event.start).total_seconds()
    if duration < 0 or not duration.is_integer():
        raise ValueError("invalid Netflix duration")
    return build_event_record(data_type=destination, source_id=event.det_id,
        start_time=event.start, end_time=event.end, tags=[], fields={
            "note": event.note, "title": event.title, "duration_seconds": int(duration),
            "external_ids": {**event.external, "content_fingerprint": event.fingerprint,
                             "timestamp_confidence": event.confidence},
            "service": "netflix", "importer": "netflix-" + variant,
            "category": "watched", "timestamp_confidence": event.confidence})


@dataclass(frozen=True)
class Progress:
    verified: int = 0
    excluded: int = 0
    duplicate_input: int = 0


class NetflixWriter:
    def __init__(self, ctx, session, destination):
        from fulcra_collect.v1_outbox import V1RecordOutbox
        from fulcra_media.v1 import media_spec

        self.session = session
        self.destination = destination
        self.spec = media_spec("watched")
        self.outbox = V1RecordOutbox(ctx, session, key=PENDING)
        self.progress = Progress()
        self.recovery = Progress()

    def resume(self):
        with self.session.record_write_scope():
            # Fully prove all original targets before recovering even one group.
            for destination in self.outbox.pending_destinations():
                self.session.resolve_destination(self.spec, pinned_id=destination, allow_create=False)
            try:
                self.outbox.drain()
            finally:
                progress = self.outbox.progress
                if progress is not None:
                    self.recovery = Progress(progress.verified, progress.excluded)

    def run_import(self, events, variant, *, chunk_size=500):
        from fulcra_common.v1 import V1DataClient, record_matches

        if type(chunk_size) is not int or chunk_size <= 0 or chunk_size > 500:
            raise ValueError("Netflix chunk size must be between 1 and 500")
        # Build the complete local codec before metadata adoption/creation.
        # The real destination is applied once resolved; this placeholder is
        # solely a local builder-validation target, never staged or submitted.
        events = list(events)
        local = {}
        for event in events:
            row = build_netflix_record(event, "Event/00000000-0000-0000-0000-000000000000", variant)
            if row["id"] in local and not (record_matches(local[row["id"]], row)
                                          and record_matches(row, local[row["id"]])):
                raise ValueError("conflicting Netflix input identity")
            local[row["id"]] = row
        with self.session.record_write_scope():
            bound = self.session.resolve_destination(
                self.spec, pinned_id=self.destination, allow_create=True)
            destination = bound.data_type
            rows = {}
            for event in events:
                row = build_netflix_record(event, destination, variant)
                if row["id"] in rows:
                    if not (record_matches(rows[row["id"]], row)
                            and record_matches(row, rows[row["id"]])):
                        raise ValueError("conflicting Netflix input identity")
                    self.progress = replace(self.progress,
                        duplicate_input=self.progress.duplicate_input + 1)
                rows[row["id"]] = row
            finalized = V1DataClient(self.session.client).validate_records(destination, list(rows.values()))
            existing = self.session.get_records(destination, list(rows)) if rows else {}
            # Mandatory whole-batch mismatch hold, including late primary IDs,
            # precedes all current record effects (even exclusion repair).
            for row in finalized:
                if row["id"] in existing and not record_matches(existing[row["id"]], row):
                    raise ValueError("existing Netflix primary payload differs; retain original source")
            for offset in range(0, len(finalized), chunk_size):
                self.outbox.stage({destination: finalized[offset:offset + chunk_size]},
                                  message="Imported Netflix records")
                # Stage is outside this finally: a failed later stage must not
                # add the preceding drain's stale progress a second time.
                baseline = self.progress
                try:
                    self.outbox.drain()
                finally:
                    progress = self.outbox.progress
                    if progress is not None:
                        self.progress = replace(baseline,
                            verified=baseline.verified + progress.verified,
                            excluded=baseline.excluded + progress.excluded)


@contextmanager
def open_netflix(settings, *, on_writer=None):
    require_runtime()
    from fulcra_collect.standalone_v1 import standalone_runtime

    with standalone_runtime("cli-netflix", settings) as (ctx, session):
        writer = NetflixWriter(ctx, session, settings["destination"])
        if on_writer is not None:
            on_writer(writer)
        writer.resume()
        yield writer
