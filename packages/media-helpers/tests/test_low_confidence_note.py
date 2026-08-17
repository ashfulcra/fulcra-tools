"""A low-confidence timestamp must be visible to the HUMAN reading the timeline.

WHY THIS EXISTS. On 2026-08-14 Trakt reported 128 watch items to us, 91 of them
carrying the byte-identical `watched_at` of 06:51:00Z — a bulk mark-as-watched
on Trakt's side, not viewing. Our importer detected exactly that (any timestamp
shared by >= 5 items is a cluster) and set `timestamp_confidence: "low"`. Then
the typed ingest path dropped the field on the wire, because the typed schema
has no slot for it and — per the comment in fulcra.py — "nothing reads those
back from the server".

That premise was true of CODE and false of the USER. Ash opened his Watched
annotations, found 110 hours of television stamped inside a 2h50m window, and
had no way to tell those rows from things he actually watched. The signal was
computed correctly and then thrown away one step before the only consumer who
needed it.

`note` is the sole free-form slot the typed schema offers, so that is where the
marker goes. These tests pin the properties that make it safe to put it there.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fulcra_media.importers.base import (
    BULK_IMPORT_NOTE_MARKER,
    LOW_CONFIDENCE_NOTE_MARKER,
    NormalizedEvent,
)


def _event(
    confidence: str = "low",
    note: str = "Letterkenny S07E03 – Nut",
    external_ids: dict | None = None,
) -> NormalizedEvent:
    start = datetime(2026, 8, 14, 6, 51, tzinfo=timezone.utc)
    return NormalizedEvent(
        external_ids=dict(external_ids or {}),
        importer="trakt",
        service="trakt",
        category="watched",
        note=note,
        title="Letterkenny",
        start_time=start,
        end_time=start + timedelta(minutes=21),
        deterministic_id="com.fulcra.media.trakt.v1.history.14266974563",
        timestamp_confidence=confidence,
    )


def _wire(ev: NormalizedEvent):
    return ev.to_duration_event(definition_id="def-watched")


class TestProvenanceDecidesTheWording:
    """`timestamp_confidence: "low"` is set by several importers for genuinely
    different reasons, so the wording follows evidence rather than confidence:

      * Trakt cluster  — many rows share one `watched_at`; carries
        `timestamp_cluster_size`. "bulk import" is literally true.
      * Netflix slim   — date-only row given a synthetic noon. We know the DAY.
      * Apple TV snap  — Recently Watched row stamped with fetch time, an upper
        bound. It happened some time BEFORE this.

    Calling the last two "bulk import" states something false about them, which
    is worse than no label: a reader who catches one wrong label stops
    trusting every label."""

    def test_a_trakt_cluster_gets_the_BULK_wording(self):
        ev = _event("low", external_ids={"timestamp_cluster_size": 91})
        assert BULK_IMPORT_NOTE_MARKER in _wire(ev).note

    def test_a_netflix_date_only_row_does_NOT_claim_bulk_import(self):
        # No cluster evidence — synthetic noon on a date-only row.
        note = _wire(_event("low", external_ids={})).note
        assert BULK_IMPORT_NOTE_MARKER not in note
        assert LOW_CONFIDENCE_NOTE_MARKER in note

    def test_an_apple_tv_snapshot_row_does_NOT_claim_bulk_import(self):
        # Fetch-time upper bound, no cluster evidence.
        note = _wire(_event("low", external_ids={"apple_tv_snapshot": True})).note
        assert BULK_IMPORT_NOTE_MARKER not in note
        assert LOW_CONFIDENCE_NOTE_MARKER in note

    def test_a_zero_cluster_size_is_not_treated_as_cluster_evidence(self):
        # Absence and 0 must behave alike; a falsy count is not proof of a bulk
        # event, and truthiness is the whole gate.
        note = _wire(_event("low", external_ids={"timestamp_cluster_size": 0})).note
        assert BULK_IMPORT_NOTE_MARKER not in note
        assert LOW_CONFIDENCE_NOTE_MARKER in note

    def test_the_generic_marker_is_a_substring_of_nothing_misleading(self):
        # The generic marker must not accidentally appear inside the bulk one in
        # a way that makes membership checks ambiguous.
        assert LOW_CONFIDENCE_NOTE_MARKER.strip("[] ") == "time unreliable"


class TestTheMarkerIsVisible:
    def test_low_confidence_note_carries_the_marker(self):
        assert LOW_CONFIDENCE_NOTE_MARKER in _wire(_event("low")).note

    def test_the_original_note_text_survives(self):
        # The marker annotates the title, it does not replace it. A user
        # skimming the timeline still needs to see WHAT the row is.
        assert "Letterkenny S07E03 – Nut" in _wire(_event("low")).note

    @pytest.mark.parametrize(
        "marker", [LOW_CONFIDENCE_NOTE_MARKER, BULK_IMPORT_NOTE_MARKER]
    )
    def test_every_marker_is_human_readable_not_a_code(self, marker):
        # These strings are read by a person in a UI, not parsed by us. If one
        # ever becomes an opaque token, this test should fail and be argued.
        assert marker.startswith(" ")           # separated from the title
        assert marker.strip().startswith("[")   # visibly an annotation
        assert "time" in marker.lower()         # says what is doubtful
        assert marker.strip("[] ").islower()    # not a SCREAMING_TOKEN


class TestItDoesNotTouchGoodData:
    @pytest.mark.parametrize("confidence", ["high", "medium"])
    def test_trustworthy_timestamps_are_left_alone(self, confidence):
        # The overwhelming majority of rows are real viewing. Marking those
        # would train the user to ignore the marker, which is worse than not
        # having one.
        assert _wire(_event(confidence)).note == "Letterkenny S07E03 – Nut"

    @pytest.mark.parametrize("confidence", ["high", "medium"])
    def test_and_the_marker_appears_nowhere_in_them(self, confidence):
        assert LOW_CONFIDENCE_NOTE_MARKER not in _wire(_event(confidence)).note


class TestIdempotence:
    """Re-imports are routine: the watermark can replay a window, and a user
    can re-run a backfill. Appending on every pass would grow the note without
    bound and make the same row look different on each sync."""

    def test_marking_twice_does_not_duplicate_the_marker(self):
        once = _wire(_event("low")).note
        twice = _wire(_event("low", note=once)).note
        assert twice.count(LOW_CONFIDENCE_NOTE_MARKER) == 1

    def test_and_the_note_is_byte_identical_on_the_second_pass(self):
        once = _wire(_event("low")).note
        assert _wire(_event("low", note=once)).note == once

    def test_a_bulk_marked_note_does_not_also_collect_the_generic_marker(self):
        # The regression the two-marker split introduces: idempotence must be
        # membership across ALL markers, not equality with one. Otherwise a
        # cluster row re-imported without its external_ids grows a second tag.
        bulk = _wire(_event("low", external_ids={"timestamp_cluster_size": 91})).note
        again = _wire(_event("low", note=bulk, external_ids={})).note
        assert again == bulk
        assert LOW_CONFIDENCE_NOTE_MARKER not in again.replace(BULK_IMPORT_NOTE_MARKER, "")


class TestDedupIdentityIsUntouched:
    """The whole change would be unshippable if it moved the dedup id: every
    already-imported row would re-post as a new one, doubling the history it
    was meant to clarify."""

    def test_source_id_does_not_depend_on_the_marker(self):
        assert _wire(_event("low")).source_id == _wire(_event("high")).source_id

    def test_source_id_is_the_deterministic_id_verbatim(self):
        ev = _event("low")
        assert _wire(ev).source_id == ev.deterministic_id

    def test_start_and_end_are_unchanged(self):
        # We are marking the timestamp as UNRELIABLE, not correcting it. We do
        # not know the true time, and inventing one would be worse than a
        # labelled wrong one.
        lo, hi = _wire(_event("low")), _wire(_event("high"))
        assert (lo.start, lo.end) == (hi.start, hi.end)


class TestEdges:
    def test_an_empty_note_still_gets_the_marker(self):
        # Movies with no title metadata can arrive with an empty note; the
        # row is exactly the kind that most needs explaining.
        assert LOW_CONFIDENCE_NOTE_MARKER in _wire(_event("low", note="")).note

    def test_the_marker_is_appended_not_prepended(self):
        # Timeline UIs truncate long notes from the right, but they sort and
        # scan by the leading text — the title must stay in front.
        note = _wire(_event("low")).note
        assert note.startswith("Letterkenny")

    def test_timestamp_confidence_is_still_carried_on_the_wire_object(self):
        # The marker is a fallback for the typed path dropping the field, not
        # a replacement for it. Any consumer that CAN read the structured
        # value must keep getting it.
        assert _wire(_event("low")).timestamp_confidence == "low"
