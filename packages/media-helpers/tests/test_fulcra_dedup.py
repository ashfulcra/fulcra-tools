from datetime import datetime, timezone

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from media_test_helpers import json_response


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


def test_fetch_existing_source_ids_collects_from_records(recording_transport):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/data/v1alpha1/event/DurationAnnotation"
        # Both params present
        params = dict(request.url.params)
        assert "start_time" in params and "end_time" in params
        return json_response(200, [
            {"metadata": {"source": ["com.fulcra.media.netflix.aaa", "com.fulcradynamics.annotation.x"]}},
            {"metadata": {"source": ["com.fulcra.media.netflix.bbb", "com.fulcradynamics.annotation.x"]}},
            {"metadata": {"source": ["unrelated.source"]}},
        ])

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    got = client.fetch_existing_source_ids(
        start=datetime(2026, 5, 12, 20, 50, tzinfo=timezone.utc),
        end=datetime(2026, 5, 12, 23, 10, tzinfo=timezone.utc),
    )
    assert got == {
        "com.fulcra.media.netflix.aaa",
        "com.fulcra.media.netflix.bbb",
        "com.fulcradynamics.annotation.x",
        "unrelated.source",
    }


def test_fetch_existing_source_groups_keeps_per_record_grouping(recording_transport):
    """The record-grouped sibling of fetch_existing_source_ids: one set per
    record (union of sources + metadata.source), records with no sources
    omitted. The flat function is a union over these groups."""
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, [
            {
                "sources": ["com.fulcra.media.lastfm.v1.aaa"],
                "metadata": {"source": ["com.fulcra.content.listened.v1.fff"]},
            },
            {"sources": ["com.fulcra.media.netflix.bbb"]},
            {"metadata": {}},  # no sources at all → omitted
        ])

    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    start = datetime(2026, 5, 12, tzinfo=timezone.utc)
    end = datetime(2026, 5, 13, tzinfo=timezone.utc)
    groups = client.fetch_existing_source_groups(start=start, end=end)
    assert groups == [
        {"com.fulcra.media.lastfm.v1.aaa", "com.fulcra.content.listened.v1.fff"},
        {"com.fulcra.media.netflix.bbb"},
    ]
    flat = client.fetch_existing_source_ids(start=start, end=end)
    assert flat == groups[0] | groups[1]


def test_fetch_existing_source_ids_empty_when_no_records(recording_transport):
    transport = recording_transport(lambda r: json_response(200, []))
    client = FulcraClient(transport=transport)
    got = client.fetch_existing_source_ids(
        start=datetime(2026, 5, 12, tzinfo=timezone.utc),
        end=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    assert got == set()


def test_fetch_existing_source_ids_reads_top_level_sources_array(recording_transport):
    """Fulcra returns source IDs under top-level 'sources' (plural), not metadata.source.

    Verified against real api.fulcradynamics.com — the production response shape is:
        {"id": ..., "sources": [...], "metadata": {<definition metadata>}}
    where metadata is the annotation-definition info, NOT a CloudEvents-style envelope.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, [
            {
                "id": "evt-1",
                "sources": ["com.fulcra.media.netflix.aaa", "com.fulcradynamics.annotation.x"],
                "metadata": {"name": "Watched", "annotation_type": "duration"},
            },
            {
                "id": "evt-2",
                "sources": ["com.fulcra.media.netflix.bbb"],
                "metadata": {},
            },
        ])
    transport = recording_transport(handler)
    client = FulcraClient(transport=transport)
    got = client.fetch_existing_source_ids(
        start=datetime(2026, 5, 12, tzinfo=timezone.utc),
        end=datetime(2026, 5, 13, tzinfo=timezone.utc),
    )
    assert got == {
        "com.fulcra.media.netflix.aaa",
        "com.fulcra.media.netflix.bbb",
        "com.fulcradynamics.annotation.x",
    }


# ---------------------------------------------------------------------------
# run_import dedup-readback pre-gate (GET /data/v1/updates before the
# per-chunk readback). data_updates is PROCESSING-time based (verified live
# 2026-07-06), so the gate window is [win_start, now] and only engages for
# windows starting within 48h of now — live polled imports, not historical
# bulk imports.
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from fulcra_media.importers.base import NormalizedEvent  # noqa: E402
from fulcra_media.state import State  # noqa: E402


def _recent_ev(i: int) -> NormalizedEvent:
    now = datetime.now(timezone.utc)
    return NormalizedEvent(
        importer="lastfm",
        service="lastfm",
        category="listened",
        note=f"N{i}",
        title=f"T{i}",
        start_time=now - timedelta(hours=1),
        end_time=now - timedelta(minutes=55),
        deterministic_id=f"com.fulcra.media.lastfm.v1.recent{i:04d}",
        timestamp_confidence="high",
    )


def _old_ev(i: int) -> NormalizedEvent:
    return NormalizedEvent(
        importer="netflix-slim",
        service="netflix",
        category="watched",
        note=f"N{i}",
        title=f"T{i}",
        start_time=datetime(2020, 6, 12, 21, 0, tzinfo=timezone.utc),
        end_time=datetime(2020, 6, 12, 22, 0, tzinfo=timezone.utc),
        deterministic_id=f"com.fulcra.media.netflix.v2.old{i:04d}",
        timestamp_confidence="low",
    )


def _media_state() -> State:
    return State(watched_definition_id="def-w", listened_definition_id="def-l",
                 read_definition_id="def-r", tag_ids={"lastfm": "t", "netflix": "t2"})


def test_pre_gate_zero_count_skips_readback_but_claim_still_runs(recording_transport):
    """Zero DurationAnnotation records processed since win_start → the
    pre-POST readback GET is skipped entirely, events are treated as new,
    and the per-event claim still runs (write-dedup guarantee untouched)."""
    gets = {"n": 0}
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            gets["n"] += 1
            return json_response(200, [])  # only the post-POST verify readback
        posts["n"] += 1
        return httpx.Response(204)

    client = FulcraClient(transport=recording_transport(handler))
    gate_calls: list[tuple] = []

    def fake_updates(start, end):
        gate_calls.append((start, end))
        return {"MomentAnnotation": 7}  # activity, but none of OUR type

    claimed: list[set] = []

    def claim(keys: set[str]) -> bool:
        claimed.append(set(keys))
        return True

    result = client.run_import([_recent_ev(1)], _media_state(), chunk_size=10,
                               claim=claim, updates_summary=fake_updates)
    assert len(gate_calls) == 1
    # The gate window reaches from win_start to (at least) now — the sound
    # window under processing-time semantics.
    start, end = gate_calls[0]
    assert end >= datetime.now(timezone.utc) - timedelta(minutes=5)
    assert result.posted == 1
    assert result.skipped_existing == 0
    assert claimed and "com.fulcra.media.lastfm.v1.recent0001" in claimed[0]
    assert posts["n"] == 1
    assert gets["n"] == 1  # ONLY the verify readback — the pre-readback was gated off


def test_pre_gate_nonzero_count_runs_normal_readback(recording_transport):
    """Records of the chunk's data type were processed since win_start →
    the normal readback runs and dedup works exactly as before."""
    det = "com.fulcra.media.lastfm.v1.recent0001"
    _DEF_SID = "com.fulcradynamics.annotation.def-l"
    gets = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            gets["n"] += 1
            return json_response(200, [{"source_id": _DEF_SID, "sources": [det, _DEF_SID]}])
        pytest.fail("must not POST — the event already exists")

    client = FulcraClient(transport=recording_transport(handler))
    result = client.run_import(
        [_recent_ev(1)], _media_state(), chunk_size=10,
        updates_summary=lambda s, e: {"DurationAnnotation": 3},
    )
    assert gets["n"] == 1          # pre-readback happened
    assert result.posted == 0
    assert result.skipped_existing == 1


def test_pre_gate_failure_fails_open_to_normal_readback(recording_transport):
    """The updates call raising must never block the import OR skip the
    readback — behaviour is exactly the ungated path."""
    det = "com.fulcra.media.lastfm.v1.recent0001"
    _DEF_SID = "com.fulcradynamics.annotation.def-l"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [{"source_id": _DEF_SID, "sources": [det, _DEF_SID]}])
        pytest.fail("must not POST")

    client = FulcraClient(transport=recording_transport(handler))

    def broken_updates(start, end):
        raise RuntimeError("500 from /data/v1/updates")

    result = client.run_import([_recent_ev(1)], _media_state(), chunk_size=10,
                               updates_summary=broken_updates)
    assert result.skipped_existing == 1
    assert result.posted == 0


def test_pre_gate_not_consulted_for_historical_windows(recording_transport):
    """A chunk whose window starts >48h ago (historical bulk import) skips
    the gate entirely: data_updates is processing-time based, so the sound
    gate window would span years and the endpoint 500s on large windows."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return json_response(200, [])
        return httpx.Response(204)

    client = FulcraClient(transport=recording_transport(handler))
    gate_calls: list[tuple] = []

    def fake_updates(start, end):
        gate_calls.append((start, end))
        return {}

    result = client.run_import([_old_ev(1)], _media_state(), chunk_size=10,
                               updates_summary=fake_updates)
    assert gate_calls == []        # gate never consulted
    assert result.posted == 1      # normal readback path posted it


def test_pre_gate_defaults_to_data_updates_summary_endpoint(recording_transport):
    """With no injected callable, the gate hits GET /data/v1/updates on the
    same client, and a zero DurationAnnotation count skips the readback."""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/data/v1/updates":
            return json_response(200, {"data_types": {"StepCount": 12},
                                       "file_changes": []})
        if request.method == "POST":
            return httpx.Response(204)
        return json_response(200, [])  # verify readback

    client = FulcraClient(transport=recording_transport(handler))
    result = client.run_import([_recent_ev(1)], _media_state(), chunk_size=10)
    assert "/data/v1/updates" in paths
    # Pre-readback was skipped: the only event GET is the post-POST verify.
    event_gets = [p for p in paths if p.startswith("/data/v1alpha1/event/")]
    assert len(event_gets) == 1
    assert result.posted == 1
