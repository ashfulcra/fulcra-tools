from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

from fulcra_media.importers.apple_podcasts import parse_db
from fulcra_media.importers.base import NormalizedEvent

FIXTURE = Path(__file__).parent / "fixtures" / "apple_podcasts_mtlibrary.sqlite"


def test_parse_db_returns_only_completed_unmanual_high_playhead():
    events = list(parse_db(FIXTURE))
    # ep 10 (Reply All) + ep 11 (Hard Fork) — 2 of 4 rows
    assert len(events) == 2
    uuids = sorted(e.external_ids["zuuid"] for e in events)
    assert uuids == ["ep-uuid-10", "ep-uuid-11"]


def test_parse_db_episode_shape():
    events = list(parse_db(FIXTURE))
    e = next(e for e in events if e.external_ids["zuuid"] == "ep-uuid-10")
    assert e.importer == "apple-podcasts"
    assert e.service == "apple-podcasts"
    assert e.category == "listened"
    assert e.note == "Reply All – The Crime Machine, Part I"
    assert e.title == "Reply All"
    # 2700s duration -> start = end - 2700s
    assert (e.end_time - e.start_time).total_seconds() == 2700
    assert e.timestamp_confidence == "medium"
    assert e.external_ids["content_fingerprint"].startswith("podcast:reply-all:")


def test_parse_db_deterministic_id_per_play_snapshot():
    """sha256(ZUUID|ZLASTDATEPLAYED) so a new last-played stamp = new event."""
    events = list(parse_db(FIXTURE))
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("com.fulcra.media.apple-podcasts.v1.") for i in ids)
