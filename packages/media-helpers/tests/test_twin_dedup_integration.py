"""End-to-end integration: import → cache populated → next import sees twins."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from fulcra_media.cli import cli
from fulcra_media.fulcra import ImportResult
from fulcra_media.state import State, save


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Tmp state.json + tmp twin cache so tests don't poison real config."""
    state_path = tmp_path / "state.json"
    save(State(
        watched_definition_id="w-uuid",
        listened_definition_id="l-uuid",
        tag_ids={"netflix": "t-netflix", "trakt": "t-trakt"},
    ), state_path)
    monkeypatch.setattr("fulcra_media.cli.STATE_PATH", state_path)
    cache_path = tmp_path / "twin_cache.json"
    monkeypatch.setattr("fulcra_media.twin_cache.DEFAULT_CACHE_PATH", cache_path)
    return tmp_path


def test_cache_populated_after_successful_high_conf_import(isolated_env, monkeypatch):
    """A successful high-conf import (rich Netflix) populates the twin cache."""
    # Rich variant 10-column header → parse_rich → high-confidence events
    csv = isolated_env / "rich.csv"
    csv.write_text(
        "Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,"
        "Device Type,Bookmark,Latest Bookmark,Country\n"
        "Ash,2026-05-12 20:32:15,01:42:30,,Dune: Part Two,,Apple TV 4K,01:42:30,01:42:30,US\n"
    )

    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: state.tag_ids.get(name, "t"),
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events),
            verified=len(events),
        ),
    )
    res = CliRunner().invoke(cli, ["import", "netflix", str(csv), "--json"])
    assert res.exit_code == 0, res.output

    cache_path = isolated_env / "twin_cache.json"
    assert cache_path.exists()
    cache = json.loads(cache_path.read_text())
    assert "movie:dune-part-two" in cache
    entry = cache["movie:dune-part-two"]
    assert entry["confidence"] == "high"
    assert entry["importer"] == "netflix-rich"


def test_cache_NOT_populated_by_low_conf_import(isolated_env, monkeypatch):
    """Low-conf events shouldn't pollute the cache — they're the side that
    GETS dedup'd against high-conf twins, not the other way around."""
    csv = isolated_env / "slim.csv"
    csv.write_text('Title,Date\n"Movie","5/12/26"\n')

    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.ensure_tag",
        lambda self, name, state: "t",
    )
    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events), verified=0,
        ),
    )
    res = CliRunner().invoke(cli, ["import", "netflix", str(csv), "--json"])
    assert res.exit_code == 0, res.output

    cache_path = isolated_env / "twin_cache.json"
    # Cache may exist (created with {}) but should hold no entries since
    # all incoming events are low-confidence
    if cache_path.exists():
        cache = json.loads(cache_path.read_text())
        assert cache == {}


def test_cache_not_populated_on_check_only(isolated_env, monkeypatch):
    csv = isolated_env / "small.csv"
    csv.write_text('Title,Date\n"Movie","5/12/26"\n')

    monkeypatch.setattr(
        "fulcra_media.fulcra.FulcraClient.run_import",
        lambda self, events, state, **kw: ImportResult(
            total=len(events), skipped_existing=0, posted=len(events), verified=0,
        ),
    )

    res = CliRunner().invoke(cli, ["import", "netflix", str(csv),
                                   "--check-only", "--json"])
    assert res.exit_code == 0, res.output
    cache_path = isolated_env / "twin_cache.json"
    # No cache writes on check-only
    assert not cache_path.exists()


def test_low_conf_twin_detected_against_cached_event(isolated_env, monkeypatch):
    """Pre-populate cache, then ingest a low-conf event with same fingerprint."""
    from fulcra_media import twin_cache
    from fulcra_csv import find_low_conf_twins

    # Seed cache as if Netflix had imported Dune Part Two
    twin_cache.save(
        {
            "movie:dune-part-two": {
                "source_id": "com.fulcra.media.netflix-rich.abc123",
                "importer": "netflix-rich",
                "start_time": "2026-04-01T20:00:00+00:00",
                "confidence": "high",
            }
        },
        isolated_env / "twin_cache.json",
    )

    # Construct a low-conf incoming event with the same fingerprint
    from fulcra_media.importers.base import NormalizedEvent
    incoming = NormalizedEvent(
        importer="trakt", service="trakt", category="watched",
        note="Dune Part Two",
        title="Dune Part Two",
        start_time=datetime(2026, 5, 16, 18, tzinfo=timezone.utc),
        end_time=datetime(2026, 5, 16, 20, tzinfo=timezone.utc),
        deterministic_id="com.fulcra.media.trakt.v1.history.999",
        timestamp_confidence="low",
        external_ids={
            "content_fingerprint": "movie:dune-part-two",
            "timestamp_cluster_size": 1000,
        },
    )
    cached = twin_cache.load_for_twin_lookup(isolated_env / "twin_cache.json")
    pairs = find_low_conf_twins([incoming], extra_pool=cached)
    assert len(pairs) == 1
    low, high = pairs[0]
    assert low.deterministic_id == "com.fulcra.media.trakt.v1.history.999"
    assert high.source_id == "com.fulcra.media.netflix-rich.abc123"
