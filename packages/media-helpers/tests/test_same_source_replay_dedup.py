"""Same-source quick-replay vs cross-source twin dedup (data-loss fix).

Confirmed loss (2026-06-07): Last.fm recorded two genuine plays of
"Patrick Hernandez – Born to Be Alive" at 15:00:17 and 15:03:36. Both
land in the same 5-minute fingerprint bucket, so both carry the IDENTICAL
com.fulcra.content.listened.v1.* cross-source fingerprint. The old
readback-skip dropped ANY event whose key set intersected the existing
source ids — so the 15:03 replay was skipped as "existing" and lost.

The decision rule under test:
  1. deterministic_id already in Fulcra        → skip (true duplicate).
  2. fingerprint matches an existing record R:
     - R carries a source from the SAME importer namespace → genuine
       same-source replay → POST, with the matched fingerprint STRIPPED
       from the wire source array (so query-time source-merging doesn't
       collapse the replay into the original record).
     - R is from a DIFFERENT importer → cross-source twin → skip.
  3. Batch-internal (claim path): a run is one plugin, so a fingerprint
     claim collision within the same run is same-source by construction
     → strip the already-claimed fingerprint and still POST, claiming
     the remaining keys.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.state import State
from media_test_helpers import json_response

_DEF_SID = "com.fulcradynamics.annotation.def-listened"

# The real fingerprint both June-7 plays produced (same 15:00 bucket).
_FP = "com.fulcra.content.listened.v1.49c528434ee6804d"

_PLAY_1 = datetime(2026, 6, 7, 15, 0, 17, tzinfo=timezone.utc)
_PLAY_2 = datetime(2026, 6, 7, 15, 3, 36, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def _listen(det_id: str, start: datetime,
            extras: tuple[str, ...] = ()) -> NormalizedEvent:
    return NormalizedEvent(
        importer="lastfm",
        service="lastfm",
        category="listened",
        note="Patrick Hernandez – Born to Be Alive",
        title="Born to Be Alive",
        start_time=start,
        end_time=start + timedelta(seconds=1),
        deterministic_id=det_id,
        timestamp_confidence="high",
        extra_source_ids=extras,
    )


def _client(recording_transport, existing_records):
    """FulcraClient whose GET readback returns ``existing_records`` and whose
    POST returns 204. Captures decoded JSONL POST records for assertions."""
    posted_records: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, existing_records)
        if request.method == "POST":
            for line in request.content.splitlines():
                posted_records.append(json.loads(line))
            return httpx.Response(204)
        pytest.fail(request.url)

    client = FulcraClient(transport=recording_transport(handler))
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        read_definition_id="def-read",
        tag_ids={"lastfm": "tag-lf"},
    )
    return client, state, posted_records


def test_same_source_replay_cross_run_is_posted_with_fp_stripped(
        recording_transport):
    """The June-7 loss, reproduced: the 15:00 play is already in Fulcra
    (its record carries the lastfm det_id AND the shared fingerprint). The
    15:03 replay has a DIFFERENT det_id but the SAME fingerprint. Because
    the existing record is from the SAME importer (lastfm), this is a
    genuine quick replay — it must POST, with the fingerprint stripped
    from its wire source array."""
    det_a = "com.fulcra.media.lastfm.v1.aaaa111122223333"
    det_b = "com.fulcra.media.lastfm.v1.bbbb444455556666"
    existing = [{
        "source_id": _DEF_SID,
        "sources": [det_a, _FP, _DEF_SID],
    }]
    client, state, posted_records = _client(recording_transport, existing)

    replay = _listen(det_b, _PLAY_2, extras=(_FP,))
    result = client.run_import([replay], state, chunk_size=10)

    assert result.posted == 1
    assert result.skipped_existing == 0
    assert len(posted_records) == 1
    wire_sources = posted_records[0]["metadata"]["source"]
    assert det_b in wire_sources
    assert _FP not in wire_sources, (
        "fingerprint must be stripped or query-time source-merging "
        "collapses the replay into the original record"
    )
    # The caller's event must not be mutated — record_twins_after_post and
    # friends consume the same list after run_import returns.
    assert replay.extra_source_ids == (_FP,)


def test_cross_source_twin_is_still_skipped(recording_transport):
    """Unchanged behavior: the existing record is from a DIFFERENT importer
    (apple-music-takeout), so a matching fingerprint means the same listen
    reported by two services — skip."""
    apple_det = "com.fulcra.media.apple-music-takeout.v1.cccc777788889999"
    det_b = "com.fulcra.media.lastfm.v1.bbbb444455556666"
    existing = [{
        "source_id": _DEF_SID,
        "sources": [apple_det, _FP, _DEF_SID],
    }]
    client, state, posted_records = _client(recording_transport, existing)

    twin = _listen(det_b, _PLAY_2, extras=(_FP,))
    result = client.run_import([twin], state, chunk_size=10)

    assert result.posted == 0
    assert result.skipped_existing == 1
    assert posted_records == []


def test_batch_internal_replay_with_claim_posts_both(recording_transport):
    """Both June-7 plays arriving in ONE run (the claim path). A run is a
    single plugin, so the second event's fingerprint-claim collision is a
    same-source replay by construction: BOTH post, the second with the
    fingerprint stripped, claiming only its remaining keys."""
    det_a = "com.fulcra.media.lastfm.v1.aaaa111122223333"
    det_b = "com.fulcra.media.lastfm.v1.bbbb444455556666"
    client, state, posted_records = _client(recording_transport, [])

    play_1 = _listen(det_a, _PLAY_1, extras=(_FP,))
    play_2 = _listen(det_b, _PLAY_2, extras=(_FP,))

    store: set[str] = set()
    claim_calls: list[set[str]] = []

    def claim(keys: set[str]) -> bool:
        claim_calls.append(set(keys))
        if store & keys:
            return False
        store.update(keys)
        return True

    result = client.run_import([play_1, play_2], state, chunk_size=10,
                               claim=claim)

    assert result.posted == 2
    assert result.skipped_existing == 0
    assert len(posted_records) == 2

    by_det = {
        next(s for s in rec["metadata"]["source"]
             if s.startswith("com.fulcra.media.")): rec
        for rec in posted_records
    }
    assert _FP in by_det[det_a]["metadata"]["source"]
    assert _FP not in by_det[det_b]["metadata"]["source"]

    # First event claimed its full key set; the second claimed only its
    # remaining keys (det_id) after the same-run fingerprint strip.
    assert claim_calls == [{det_a, _FP}, {det_b}]
    assert store == {det_a, _FP, det_b}
    # Caller events untouched.
    assert play_2.extra_source_ids == (_FP,)


def test_true_duplicate_det_id_is_still_skipped(recording_transport):
    """Unchanged behavior: an event whose deterministic_id is already in
    Fulcra is a true duplicate — skip, even when it also carries a
    fingerprint pointing at the same (same-source) record."""
    det_a = "com.fulcra.media.lastfm.v1.aaaa111122223333"
    existing = [{
        "source_id": _DEF_SID,
        "sources": [det_a, _FP, _DEF_SID],
    }]
    client, state, posted_records = _client(recording_transport, existing)

    dup = _listen(det_a, _PLAY_1, extras=(_FP,))
    result = client.run_import([dup], state, chunk_size=10)

    assert result.posted == 0
    assert result.skipped_existing == 1
    assert posted_records == []


def test_event_without_fingerprint_behaves_as_before(recording_transport):
    """Unchanged behavior: no extra_source_ids → skip iff det_id exists."""
    det_a = "com.fulcra.media.lastfm.v1.aaaa111122223333"
    det_b = "com.fulcra.media.lastfm.v1.bbbb444455556666"
    existing = [{
        "source_id": _DEF_SID,
        "sources": [det_a, _DEF_SID],
    }]
    client, state, posted_records = _client(recording_transport, existing)

    dup = _listen(det_a, _PLAY_1)
    fresh = _listen(det_b, _PLAY_2)
    result = client.run_import([dup, fresh], state, chunk_size=10)

    assert result.posted == 1
    assert result.skipped_existing == 1
    assert len(posted_records) == 1
    assert det_b in posted_records[0]["metadata"]["source"]
