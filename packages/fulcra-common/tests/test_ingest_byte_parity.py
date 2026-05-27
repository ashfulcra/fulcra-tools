"""Byte-for-byte regression net for the refactor #69 cutover.

For each of the 4+ callsites being cut over (media-helpers, attention,
csv-importer, daemon quick-record, daemon tombstone), we hand-build the
wire record exactly the way the legacy site does today, then build the
same record via `IngestPipeline.build_record(...)`, and assert the
JSONL-encoded bytes are identical.

This file is INTENTIONALLY deletable in Phase 3: once every callsite is
using the pipeline, the comparison is between two computations that read
from the same source, so the test is redundant. Until then it is the
contract that says "the wire format hasn't drifted."

Per the refactor #69 pre-resolved decision (Option B): attention's
`category` / `url` / `og_description` / `favicon_url` / `parent_source_id`
stay as top-level data fields (not under external_ids). Byte parity is
the whole point of the refactor, so the wire shape is preserved verbatim.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fulcra_common import wire
from fulcra_common.ingest import DurationEvent, IngestPipeline, MomentEvent

UTC = timezone.utc
PIPE = IngestPipeline(client=None)


def _bytes(rec: dict) -> bytes:
    return wire.encode_batch([rec])


def test_byte_parity_media_helpers_duration():
    """Mirrors fulcra_media/fulcra.py ingest_batch construction."""
    start = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    end   = datetime(2026, 5, 22, 12, 30, 0, tzinfo=UTC)
    legacy_data = {
        "note": "Severance",
        "title": "Severance — S2E5",
        "service": "trakt",
        "timestamp_confidence": "high",
        "duration_seconds": int((end - start).total_seconds()),
        "external_ids": {"trakt_id": 12345},
    }
    legacy = wire.build_record(
        data_type=wire.DURATION_ANNOTATION,
        start_time=start, end_time=end,
        data=legacy_data,
        source_id="com.fulcra.media.trakt.deadbeef",
        tags=["tag-trakt"],
        definition_id="def-watched",
        extra_source_ids=("com.fulcra.content.watched.v1.fp",),
    )

    ev = DurationEvent(
        definition_id="def-watched",
        source_id="com.fulcra.media.trakt.deadbeef",
        extra_source_ids=("com.fulcra.content.watched.v1.fp",),
        tags=("tag-trakt",),
        external_ids={"trakt_id": 12345},
        note="Severance",
        title="Severance — S2E5",
        service="trakt",
        timestamp_confidence="high",
        start=start, end=end,
    )
    assert _bytes(PIPE.build_record(ev)) == _bytes(legacy)


def test_byte_parity_attention_duration():
    """Mirrors fulcra_attention/ingest.py build_attention_event — the
    five extension fields (category, url, og_description, favicon_url,
    parent_source_id) stay at top-level data, NOT under external_ids."""
    start = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    end   = datetime(2026, 5, 22, 12, 1, 30, tzinfo=UTC)
    legacy_data = {
        "note": "Title — https://example.com/x",
        "title": "Title",
        "service": "web",
        "category": None,
        "url": "https://example.com/x",
        "og_description": "desc",
        "favicon_url": "https://example.com/fav.ico",
        "duration_seconds": int((end - start).total_seconds()),
        "parent_source_id": None,
        "external_ids": {
            "client": "chrome",
            "host": "example.com",
            "chrome_identity": None,
            "og_type": None,
            "lang": "en",
        },
    }
    legacy = wire.build_record(
        data_type=wire.DURATION_ANNOTATION,
        start_time=start, end_time=end,
        data=legacy_data,
        source_id="com.fulcra.attention.v2.abc",
        tags=["t-attention", "t-web"],
        definition_id="def-attention",
    )

    ev = DurationEvent(
        definition_id="def-attention",
        source_id="com.fulcra.attention.v2.abc",
        tags=("t-attention", "t-web"),
        external_ids={
            "client": "chrome",
            "host": "example.com",
            "chrome_identity": None,
            "og_type": None,
            "lang": "en",
        },
        note="Title — https://example.com/x",
        title="Title",
        service="web",
        category=None,
        url="https://example.com/x",
        og_description="desc",
        favicon_url="https://example.com/fav.ico",
        parent_source_id=None,
        _emit_attention_fields=True,
        start=start, end=end,
    )
    assert _bytes(PIPE.build_record(ev)) == _bytes(legacy)


def test_byte_parity_csv_importer_duration_with_data_fields():
    """Mirrors fulcra_csv/fulcra.py _build_record (DURATION branch)."""
    start = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    end   = datetime(2026, 5, 22, 12, 10, 0, tzinfo=UTC)
    duration = int((end - start).total_seconds())
    legacy_data = {
        "note": "n", "title": "t",
        "duration_seconds": duration,
        "external_ids": {"row": 7},
    }
    legacy = wire.build_record(
        data_type=wire.DURATION_ANNOTATION,
        start_time=start, end_time=end,
        data=legacy_data,
        source_id="csv-row-7",
        tags=["tag-id-1"],
        definition_id="def-csv",
    )

    ev = DurationEvent(
        definition_id="def-csv",
        source_id="csv-row-7",
        tags=("tag-id-1",),
        external_ids={"row": 7},
        note="n", title="t",
        start=start, end=end,
    )
    assert _bytes(PIPE.build_record(ev)) == _bytes(legacy)


def test_byte_parity_daemon_quick_record_moment():
    """Mirrors fulcra_collect/daemon.py:746 build for the moment branch."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    legacy = wire.build_record(
        data_type=wire.MOMENT_ANNOTATION,
        start_time=now,
        data={"comment": "hello"},
        source_id="com.fulcradynamics.fulcra-collect.quick-record.aaaa",
        tags=["tag-1"],
        definition_id="def-quick",
    )

    ev = MomentEvent(
        definition_id="def-quick",
        source_id="com.fulcradynamics.fulcra-collect.quick-record.aaaa",
        tags=("tag-1",),
        comment="hello",
        ts=now,
    )
    assert _bytes(PIPE.build_record(ev)) == _bytes(legacy)


def test_byte_parity_daemon_quick_record_duration():
    """Mirrors fulcra_collect/daemon.py:736 build for the duration branch.

    Note: the legacy site emits `duration_seconds` as a FLOAT
    (`.total_seconds()` returns float). The pipeline emits it as INT
    (it casts via `int(...)`). This is a deliberate normalization
    documented in the Task 8 commit message — float seconds → int
    seconds on a count of whole seconds is observably identical to every
    Fulcra consumer, but the bytes differ. This test builds the legacy
    side with the int cast to keep byte parity meaningful.
    """
    start = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    end   = datetime(2026, 5, 22, 13, 0, 0, tzinfo=UTC)
    legacy = wire.build_record(
        data_type=wire.DURATION_ANNOTATION,
        start_time=start, end_time=end,
        data={
            "comment": "session",
            "duration_seconds": int((end - start).total_seconds()),
        },
        source_id="com.fulcradynamics.fulcra-collect.quick-record.bbbb",
        tags=["tag-1"],
        definition_id="def-quick",
    )

    ev = DurationEvent(
        definition_id="def-quick",
        source_id="com.fulcradynamics.fulcra-collect.quick-record.bbbb",
        tags=("tag-1",),
        comment="session",
        start=start, end=end,
    )
    assert _bytes(PIPE.build_record(ev)) == _bytes(legacy)


def test_byte_parity_daemon_tombstone():
    """Mirrors fulcra_collect/daemon.py:_delete_annotation tombstone."""
    now = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    legacy = wire.build_record(
        data_type=wire.MOMENT_ANNOTATION,
        start_time=now,
        data={
            "comment": "[deleted via Fulcra Collect menubar undo]",
            "superseded_by": "deleted",
            "supersedes_source_id": "some-orig-source-id",
        },
        source_id="com.fulcradynamics.fulcra-collect.quick-record.undo.tomb",
        tags=[],
        definition_id=None,
    )

    ev = MomentEvent(
        definition_id=None,
        source_id="com.fulcradynamics.fulcra-collect.quick-record.undo.tomb",
        tags=(),
        comment="[deleted via Fulcra Collect menubar undo]",
        superseded_by="deleted",
        supersedes_source_id="some-orig-source-id",
        ts=now,
    )
    assert _bytes(PIPE.build_record(ev)) == _bytes(legacy)
