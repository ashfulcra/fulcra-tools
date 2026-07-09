"""End-to-end test driving the real Netflix CSV through the full pipeline.

Uses httpx.MockTransport so no real network calls are made.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import httpx
import pytest

from fulcra_media.fulcra import FulcraClient
from fulcra_media.importers.netflix import parse_slim
from fulcra_media.state import State
from media_test_helpers import json_response


REAL_CSV = Path(__file__).parent.parent / "takeouts" / "NetflixViewingHistory.csv"


@pytest.fixture(autouse=True)
def fake_token(mocker):
    mocker.patch.dict("os.environ", {"FULCRA_ACCESS_TOKEN": "test-token"})


@pytest.mark.skipif(not REAL_CSV.exists(), reason="real Netflix takeout not present")
def test_real_netflix_csv_full_pipeline(recording_transport):
    # All 6,456 rows produce distinct deterministic IDs
    events = list(parse_slim(REAL_CSV))
    assert len(events) >= 6000, f"expected at least 6000 events, got {len(events)}"
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids)), "deterministic IDs collided — rewatch dedup is broken"

    # And the dedup rule produced extra annotations for same-day rewatches
    by_date_title = Counter()
    for e in events:
        by_date_title[(e.external_ids["raw_date"], e.note)] += 1
    rewatches = {k: v for k, v in by_date_title.items() if v > 1}
    assert len(rewatches) >= 1, "expected at least one same-day rewatch in real data"

    # Drive the network pipeline with a mock that captures every JSONL line
    posted_lines: list[bytes] = []

    # Verification readback after ingest returns the same source IDs we posted,
    # carrying the current def's source_id at top level (so the run_import
    # def-scoped filter accepts them).
    _DEF_SID = "com.fulcradynamics.annotation.def-watched"
    posted_so_far: set[str] = set()
    def handler2(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/data/v1alpha1/event/DurationAnnotation":
            return json_response(
                200,
                [{"source_id": _DEF_SID, "sources": [sid, _DEF_SID]} for sid in posted_so_far],
            )
        if (request.method == "POST"
                and request.url.path == "/ingest/v1/record/DurationAnnotation"):
            for line in request.content.splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                # Typed records are unwrapped: the deterministic ID sits at
                # position 0 of the top-level `sources` array.
                posted_so_far.add(rec["sources"][0])
                posted_lines.append(line)
            return httpx.Response(204)
        pytest.fail(f"unexpected {request.method} {request.url}")

    client = FulcraClient(transport=recording_transport(handler2))

    state = State(
        watched_definition_id="def-watched",
        listened_definition_id="def-listened",
        tag_ids={"netflix": "tag-netflix"},
    )

    result = client.run_import(events, state)

    assert result.total == len(events)
    assert result.skipped_existing == 0
    assert result.posted == len(events)
    assert result.verified == len(events)
    assert len(posted_lines) == len(events)

    # Spot-check the first emitted line — the UNWRAPPED typed shape.
    first = json.loads(posted_lines[0])
    assert "specversion" not in first
    assert "metadata" not in first
    assert "data" not in first
    assert "start_time" in first["recorded_at"] and "end_time" in first["recorded_at"]
    assert first["tags"] == ["tag-netflix"]
    assert any("com.fulcradynamics.annotation." in s for s in first["sources"])
    # note is the only free-form slot; service / timestamp_confidence had no
    # typed slot and are dropped from the wire (nothing reads them back from
    # the server — see FulcraClient.ingest_batch_typed).
    assert first["note"]
    assert "service" not in first
    assert "timestamp_confidence" not in first
