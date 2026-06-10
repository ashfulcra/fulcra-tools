from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient, ImportResult
from fulcra_media.importers.base import NormalizedEvent
from fulcra_media.state import State
from media_test_helpers import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def _ev(i: int, det_id: str | None = None) -> NormalizedEvent:
    return NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note=f"N{i}",
        title=f"T{i}",
        start_time=datetime(2026, 5, 12, 21, 0, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 12, 22, 0, tzinfo=timezone.utc),
        deterministic_id=det_id or f"com.fulcra.media.netflix.id{i:04d}",
        timestamp_confidence="low",
    )


def test_run_import_dedupes_against_existing(recording_transport):
    """One existing, two new -> ingest 2, skip 1, verify 2."""
    # Real Fulcra records carry source_id pointing at the current def, plus
    # the per-event sources array. fetch_existing_source_ids filters records
    # by source_id to ignore orphans from soft-deleted defs.
    _DEF_SID = "com.fulcradynamics.annotation.def-watched"
    existing_response = [
        {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0001", _DEF_SID]},
    ]
    after_response = [
        {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0001", _DEF_SID]},
        {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0002", _DEF_SID]},
        {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0003", _DEF_SID]},
    ]
    call_counter = {"get": 0, "post": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            call_counter["get"] += 1
            return json_response(200, existing_response if call_counter["get"] == 1 else after_response)
        if request.method == "POST":
            call_counter["post"] += 1
            return httpx.Response(204)
        pytest.fail(request.url)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"netflix": "tag-netflix"},
    )
    events = [_ev(1), _ev(2), _ev(3)]
    result = client.run_import(events, state, chunk_size=10)
    assert isinstance(result, ImportResult)
    assert result.skipped_existing == 1
    assert result.posted == 2
    assert result.verified == 2
    assert call_counter["post"] == 1
    assert call_counter["get"] == 2


def test_run_import_no_new_events_does_not_post(recording_transport):
    _DEF_SID = "com.fulcradynamics.annotation.d"
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [
                {"source_id": _DEF_SID, "sources": ["com.fulcra.media.netflix.id0001", _DEF_SID]},
            ])
        pytest.fail(f"unexpected POST {request.url}")
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2", tag_ids={"netflix": "t"})
    result = client.run_import([_ev(1)], state, chunk_size=10)
    assert result.posted == 0
    assert result.skipped_existing == 1
    assert result.verified == 0


def _ev_with_extra(i: int, extra: tuple[str, ...]) -> NormalizedEvent:
    ev = _ev(i)
    ev.extra_source_ids = extra
    return ev


def test_run_import_claim_skips_already_claimed_event(recording_transport):
    """An event whose dedup key set was already claimed (claim returns False)
    is NOT posted; it counts as skipped_existing instead."""
    post_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])  # nothing in Fulcra → readback passes
        post_count["n"] += 1
        return httpx.Response(204)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2",
                  tag_ids={"netflix": "t"})

    # Pre-claimed: the daemon already forwarded this event's key in a prior
    # run; claim returns False for its deterministic_id.
    already = {"com.fulcra.media.netflix.id0001"}

    def claim(keys: set[str]) -> bool:
        return not (keys & already)

    result = client.run_import([_ev(1)], state, chunk_size=10, claim=claim)
    assert result.posted == 0
    assert result.skipped_existing == 1
    assert post_count["n"] == 0


def test_run_import_claim_none_behaves_as_before(recording_transport):
    """claim=None → identical to the pre-component-3 readback-only path:
    a new (not-in-Fulcra) event is posted."""
    post_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])
        post_count["n"] += 1
        return httpx.Response(204)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2",
                  tag_ids={"netflix": "t"})
    result = client.run_import([_ev(1)], state, chunk_size=10, claim=None)
    assert result.posted == 1
    assert post_count["n"] == 1


def test_run_import_claim_same_run_fingerprint_collision_posts_both(recording_transport):
    """Two events in ONE run share a com.fulcra.content.* fingerprint but
    have different deterministic_ids — neither is in Fulcra yet (readback
    passes for both). A run is a single plugin, so a same-run fingerprint
    collision is a same-source quick replay (two real plays inside one
    5-minute bucket), NOT a cross-source twin: BOTH must post. The second
    event's already-claimed fingerprint is stripped before its claim, so it
    claims (and posts with) only its remaining keys."""
    post_bodies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])
        post_bodies.append(request.content)
        return httpx.Response(204)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2",
                  tag_ids={"netflix": "t"})

    shared_fp = "com.fulcra.content.movie.v1.sharedhash"
    twin_a = _ev_with_extra(1, (shared_fp,))  # det id0001
    twin_b = _ev_with_extra(2, (shared_fp,))  # det id0002, same fingerprint

    # A real claim store: a set of already-claimed keys, mirroring the
    # forwarded_events INSERT OR IGNORE semantics.
    claimed: set[str] = set()

    def claim(keys: set[str]) -> bool:
        if keys & claimed:
            return False
        claimed.update(keys)
        return True

    result = client.run_import([twin_a, twin_b], state, chunk_size=10,
                               claim=claim)
    # Both replays posted (one batch POST); the fingerprint was claimed once.
    assert result.posted == 2
    assert result.skipped_existing == 0
    assert len(post_bodies) == 1
    assert claimed == {
        "com.fulcra.media.netflix.id0001",
        "com.fulcra.media.netflix.id0002",
        shared_fp,
    }
    # The second event's fingerprint was stripped from the wire body so
    # query-time source-merging can't collapse the replay into the original.
    lines = post_bodies[0].splitlines()
    assert len(lines) == 2
    import json as _json
    sources_by_line = [
        _json.loads(line)["metadata"]["source"] for line in lines
    ]
    fp_carriers = [s for s in sources_by_line if shared_fp in s]
    assert len(fp_carriers) == 1


def test_run_import_check_only_does_not_call_claim(recording_transport):
    """check_only is a dry run; it must not mutate the shared dedup store,
    so the claim is bypassed entirely."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])
        pytest.fail(f"check_only must not POST: {request.url}")

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2",
                  tag_ids={"netflix": "t"})

    calls: list = []

    def claim(keys: set[str]) -> bool:
        calls.append(keys)
        return True

    result = client.run_import([_ev(1)], state, chunk_size=10,
                               check_only=True, claim=claim)
    assert result.posted == 1   # would-post count
    assert calls == []          # claim never invoked in a dry run


def _claim_pair(store: set):
    """Return (claim, unclaim) callables backed by ``store`` — a real
    in-memory analogue of forwarded_events. claim records all keys iff none
    pre-exist; unclaim deletes exactly the named keys."""
    def claim(keys: set[str]) -> bool:
        if store & keys:
            return False
        store.update(keys)
        return True

    def unclaim(keys: set[str]) -> None:
        store.difference_update(keys)

    return claim, unclaim


def test_run_import_unclaims_on_post_failure_so_event_is_retried(
        recording_transport):
    """Durable-loss guard: if the batch POST RAISES, the keys this batch
    claimed are released — they're NOT left in the store, so a re-run
    re-posts the event instead of skipping it forever."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])  # readback passes
        return httpx.Response(500)  # POST fails → raise_for_status raises

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2",
                  tag_ids={"netflix": "t"})

    store: set[str] = set()
    claim, unclaim = _claim_pair(store)

    ev = _ev(1)
    with pytest.raises(Exception):
        client.run_import([ev], state, chunk_size=10,
                          claim=claim, unclaim=unclaim)

    # The failed event's key was released — store is clean.
    assert ev.deterministic_id not in store
    assert store == set()


def test_run_import_keeps_claim_on_post_success(recording_transport):
    """The complement: a SUCCESSFUL POST leaves the claim in place, so a
    re-run skips the already-written event (no duplicate)."""
    post_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])
        post_count["n"] += 1
        return httpx.Response(204)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2",
                  tag_ids={"netflix": "t"})

    store: set[str] = set()
    claim, unclaim = _claim_pair(store)

    ev = _ev(1)
    result = client.run_import([ev], state, chunk_size=10,
                               claim=claim, unclaim=unclaim)
    assert result.posted == 1
    assert post_count["n"] == 1
    # Claim retained on success.
    assert ev.deterministic_id in store

    # Re-run with the SAME store → the claim blocks a second POST.
    result2 = client.run_import([_ev(1)], state, chunk_size=10,
                                claim=claim, unclaim=unclaim)
    assert result2.posted == 0
    assert result2.skipped_existing == 1
    assert post_count["n"] == 1  # no second POST


def test_run_import_failure_unclaim_scoped_to_failed_batch_only(
        recording_transport):
    """With two chunks where the FIRST succeeds and the SECOND fails, only
    the second batch's keys are released — the first batch's claim stays so
    the already-written events aren't re-posted on retry."""
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])
        posts["n"] += 1
        # First chunk's POST succeeds; second chunk's POST fails.
        return httpx.Response(204 if posts["n"] == 1 else 500)

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2",
                  tag_ids={"netflix": "t"})

    store: set[str] = set()
    claim, unclaim = _claim_pair(store)

    # chunk_size=1 → ev(1) is batch 1 (succeeds), ev(2) is batch 2 (fails).
    with pytest.raises(Exception):
        client.run_import([_ev(1), _ev(2)], state, chunk_size=1,
                          claim=claim, unclaim=unclaim)

    # Batch 1's claim retained; batch 2's claim released.
    assert "com.fulcra.media.netflix.id0001" in store
    assert "com.fulcra.media.netflix.id0002" not in store


def test_run_import_chunks_large_input(recording_transport):
    post_count = {"n": 0}
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])
        post_count["n"] += 1
        return httpx.Response(204)
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    state = State(watched_definition_id="d", listened_definition_id="d2", tag_ids={"netflix": "t"})
    events = [_ev(i) for i in range(25)]
    # GET response is empty so verification finds 0; the pipeline used to raise
    # but now reports the gap non-fatally (Fulcra has read-after-write lag).
    result = client.run_import(events, state, chunk_size=10)
    assert result.total == 25
    assert result.posted == 25
    assert result.verified == 0
    assert post_count["n"] == 3
