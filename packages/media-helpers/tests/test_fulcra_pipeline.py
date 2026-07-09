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
    import json as _json
    post_bodies: list = []
    landed_rows: list[dict] = []  # echo POSTed records back on verify GETs

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, landed_rows)
        post_bodies.append(request.content)
        for line in request.content.splitlines():
            landed_rows.append({
                "source_id": "com.fulcradynamics.annotation.d",
                "sources": _json.loads(line)["sources"],
            })
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
    sources_by_line = [
        _json.loads(line)["sources"] for line in lines
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
    """The complement: a SUCCESSFUL POST that LANDS leaves the claim in
    place, so a re-run skips the already-written event (no duplicate).
    The handler echoes POSTed records back on verify GETs — a landed
    record; a record that never lands now has its claim released by the
    delayed-verify self-heal instead."""
    import json as _json
    post_count = {"n": 0}
    landed_rows: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, landed_rows)
        post_count["n"] += 1
        for line in request.content.splitlines():
            landed_rows.append({
                "source_id": "com.fulcradynamics.annotation.d",
                "sources": _json.loads(line)["sources"],
            })
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


# ---------------------------------------------------------------------------
# Typed-ingest adoption (Task 4): run_import posts UNWRAPPED records to the
# typed endpoint, and its landed-count check surfaces silent JSONL drops.
# ---------------------------------------------------------------------------

_DEF_WATCHED = "com.fulcradynamics.annotation.def-watched"


def test_run_import_posts_typed_jsonl_to_base_type_endpoint(recording_transport):
    """A multi-event run posts UNWRAPPED records to
    /ingest/v1/record/DurationAnnotation as application/x-jsonl — the typed
    endpoint requires exactly that content-type (application/x-jsonlines
    415s, live-verified 2026-07-08) and silently strips the wrapped
    specversion/metadata/data envelope, so run_import must emit the
    unwrapped shape.
    """
    import json

    captured: dict = {}
    call = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            call["get"] += 1
            if call["get"] == 1:  # dedup readback: nothing exists yet
                return json_response(200, [])
            # verify readback: both records landed
            return json_response(200, [
                {"source_id": _DEF_WATCHED,
                 "sources": ["com.fulcra.media.netflix.id0001", _DEF_WATCHED]},
                {"source_id": _DEF_WATCHED,
                 "sources": ["com.fulcra.media.netflix.id0002", _DEF_WATCHED]},
            ])
        if request.method == "POST":
            captured["path"] = request.url.path
            captured["content_type"] = request.headers.get("content-type")
            captured["body"] = request.content
            return httpx.Response(204)
        pytest.fail(str(request.url))

    client = FulcraClient(transport=recording_transport(handler))
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"netflix": "tag-netflix"},
    )
    result = client.run_import([_ev(1), _ev(2)], state, chunk_size=10)
    assert result.posted == 2
    assert result.verified == 2

    assert captured["path"] == "/ingest/v1/record/DurationAnnotation"
    assert captured["content_type"].startswith("application/x-jsonl")
    lines = [ln for ln in captured["body"].splitlines() if ln.strip()]
    assert len(lines) == 2
    recs = [json.loads(ln) for ln in lines]
    for rec in recs:
        # UNWRAPPED — no CloudEvents envelope.
        assert "specversion" not in rec
        assert "metadata" not in rec
        assert "data" not in rec
        # Duration recorded_at is the {start,end} object.
        assert set(rec["recorded_at"]) == {"start_time", "end_time"}
        assert _DEF_WATCHED in rec["sources"]
    notes = {rec["note"] for rec in recs}
    assert notes == {"N1", "N2"}


def _delayed_verify_setup(recording_transport, poll_rows_fn):
    """Client + claim/unclaim store + sleep recorder for the delayed-verify
    tests. GET #1 is the dedup readback (empty), GET #2 the immediate
    post-POST verify (only id0001 landed), GET #3+ are the delayed poll
    attempts, answered by ``poll_rows_fn(attempt_number)``.
    """
    call = {"get": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            call["get"] += 1
            if call["get"] == 1:  # dedup readback: nothing exists yet
                return json_response(200, [])
            if call["get"] == 2:  # immediate verify: only id0001 landed
                return json_response(200, [
                    {"source_id": _DEF_WATCHED,
                     "sources": ["com.fulcra.media.netflix.id0001",
                                 _DEF_WATCHED]},
                ])
            return poll_rows_fn(call["get"] - 2)  # poll attempt 1, 2, ...
        if request.method == "POST":
            return httpx.Response(204)
        pytest.fail(str(request.url))

    client = FulcraClient(transport=recording_transport(handler))
    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"netflix": "tag-netflix"},
    )
    store: set[str] = set()
    claim, unclaim = _claim_pair(store)
    sleeps: list[float] = []
    return client, state, store, claim, unclaim, sleeps


def test_run_import_unclaims_and_warns_when_still_missing_after_poll(
        recording_transport, caplog):
    """The self-healing path for a silent JSONL drop: typed ingest is async
    (201 != stored) and a batch silently drops a bad line (live-verified
    2026-07-08). A record still missing after the bounded delayed poll has
    its dedup keys UNCLAIMED (so the next run retries it instead of
    skipping it forever — a held claim + dropped line = permanent loss)
    and ONE WARNING names the missing source-ids.
    """
    import logging
    from fulcra_media import fulcra as fulcra_mod

    def poll_rows(_attempt):  # id0002 never lands
        return json_response(200, [
            {"source_id": _DEF_WATCHED,
             "sources": ["com.fulcra.media.netflix.id0001", _DEF_WATCHED]},
        ])

    client, state, store, claim, unclaim, sleeps = _delayed_verify_setup(
        recording_transport, poll_rows)
    with caplog.at_level(logging.WARNING, logger="fulcra_media.fulcra"):
        result = client.run_import([_ev(1), _ev(2)], state, chunk_size=10,
                                   claim=claim, unclaim=unclaim,
                                   sleep_fn=sleeps.append)

    assert result.posted == 2
    assert result.verified == 1
    # The poll ran to its bound: attempts x delay, first attempt delayed too.
    assert sleeps == [fulcra_mod._LANDED_VERIFY_DELAY_S] * \
        fulcra_mod._LANDED_VERIFY_ATTEMPTS
    # The missing event's keys were released for retry; the landed one kept.
    assert "com.fulcra.media.netflix.id0002" not in store
    assert "com.fulcra.media.netflix.id0001" in store

    warnings = [r.getMessage() for r in caplog.records
                if r.levelname == "WARNING"]
    assert len(warnings) == 1
    msg = warnings[0]
    assert "still not visible" in msg
    assert "unclaimed for retry next run" in msg
    # Names the MISSING id, not the one that landed.
    assert "com.fulcra.media.netflix.id0002" in msg
    assert "com.fulcra.media.netflix.id0001" not in msg


def test_run_import_poll_recovery_is_silent_and_keeps_claims(
        recording_transport, caplog):
    """A record that becomes visible during the delayed poll is just the
    normal async lag (~1-2 min): no output above DEBUG, claims stay, and
    the record counts as verified."""
    import logging
    from fulcra_media import fulcra as fulcra_mod

    def poll_rows(_attempt):  # both visible on the first poll attempt
        return json_response(200, [
            {"source_id": _DEF_WATCHED,
             "sources": ["com.fulcra.media.netflix.id0001", _DEF_WATCHED]},
            {"source_id": _DEF_WATCHED,
             "sources": ["com.fulcra.media.netflix.id0002", _DEF_WATCHED]},
        ])

    client, state, store, claim, unclaim, sleeps = _delayed_verify_setup(
        recording_transport, poll_rows)
    with caplog.at_level(logging.DEBUG, logger="fulcra_media.fulcra"):
        result = client.run_import([_ev(1), _ev(2)], state, chunk_size=10,
                                   claim=claim, unclaim=unclaim,
                                   sleep_fn=sleeps.append)

    assert result.posted == 2
    assert result.verified == 2  # 1 immediate + 1 via the delayed poll
    # Poll stopped after the first (successful) attempt.
    assert sleeps == [fulcra_mod._LANDED_VERIFY_DELAY_S]
    # Both claims kept — the records landed.
    assert "com.fulcra.media.netflix.id0001" in store
    assert "com.fulcra.media.netflix.id0002" in store
    # Nothing above DEBUG from the pipeline itself. Scope to our logger so the
    # assertion tracks the capture setup above (at_level pins fulcra_media.fulcra)
    # and stays immune to unrelated ambient chatter — e.g. httpx's INFO "HTTP
    # Request" lines, which surface whenever some other test has lowered the root
    # logger level and left it that way.
    assert [r for r in caplog.records
            if r.levelno > logging.DEBUG and r.name.startswith("fulcra_media")] == []


def test_run_import_verify_error_warns_but_keeps_claims(
        recording_transport, caplog):
    """If the verification readback itself FAILS, do NOT unclaim (fail-safe:
    a kept claim loses the event only if it was ALSO dropped; unclaiming on
    unknown risks duplicates — the typed endpoint has no server-side dedup).
    A WARNING says landings could not be verified; the import still returns
    normally."""
    import logging

    def poll_rows(_attempt):  # verification readback errors out
        return httpx.Response(500)

    client, state, store, claim, unclaim, sleeps = _delayed_verify_setup(
        recording_transport, poll_rows)
    with caplog.at_level(logging.WARNING, logger="fulcra_media.fulcra"):
        result = client.run_import([_ev(1), _ev(2)], state, chunk_size=10,
                                   claim=claim, unclaim=unclaim,
                                   sleep_fn=sleeps.append)

    assert result.posted == 2
    assert result.verified == 1
    # Claims for BOTH events kept — outcome unknown, fail-safe.
    assert "com.fulcra.media.netflix.id0001" in store
    assert "com.fulcra.media.netflix.id0002" in store

    warnings = [r.getMessage() for r in caplog.records
                if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "could not verify landings" in warnings[0]
    assert "unclaimed" not in warnings[0]
