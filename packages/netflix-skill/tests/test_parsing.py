def test_script_loads_and_declares_contract(ni):
    assert ni.DEF_NAME == "Watched"
    assert ni.DEF_MARKER == "com.fulcradynamics.annotation.media.watched"
    assert ni.API_BASE.startswith("https://")


import hashlib
from datetime import datetime, timezone


def test_parse_netflix_date(ni):
    d = ni.parse_netflix_date("4/12/23")
    assert (d.year, d.month, d.day) == (2023, 4, 12)


def test_parse_netflix_date_rejects_iso(ni):
    import pytest
    with pytest.raises(ValueError):
        ni.parse_netflix_date("2023-04-12")


def test_make_note_and_title(ni):
    note, title = ni.make_note_and_title("BEEF: Season 1: Episode 2")
    assert title == "BEEF"
    assert note == "BEEF: Season 1: Episode 2"
    note, title = ni.make_note_and_title("Dune")
    assert (note, title) == ("Dune", "Dune")


def test_slim_det_id_matches_fulcra_media_scheme(ni):
    # MUST equal fulcra-media's _det_id exactly (cross-tool dedup contract).
    h = hashlib.sha256("4/12/23|BEEF: Season 1: Episode 2|0".encode()).hexdigest()
    assert ni.det_id_slim("4/12/23", "BEEF: Season 1: Episode 2", 0) == \
        f"com.fulcra.media.netflix.v2.{h[:16]}"


def test_parse_slim_occurrence_disambiguates_same_day_rewatch(ni, fixtures_dir):
    events = list(ni.parse_slim(fixtures_dir / "slim.csv"))
    assert len(events) == 5
    a, b = events[0], events[1]           # the duplicate pair
    assert a.det_id != b.det_id           # review finding #2
    # deterministic across re-parses
    again = list(ni.parse_slim(fixtures_dir / "slim.csv"))
    assert [e.det_id for e in again] == [e.det_id for e in events]


def test_parse_slim_event_shape(ni, fixtures_dir):
    ev = list(ni.parse_slim(fixtures_dir / "slim.csv"))[2]  # Dune: Part Two
    assert ev.start == datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert (ev.end - ev.start).total_seconds() == 1  # Fulcra drops start==end
    assert ev.confidence == "low"
    assert ev.external["point_in_time"] is True
    assert ev.external["occurrence_index"] == 0


def test_parse_slim_rejects_wrong_header(ni, tmp_path):
    import pytest
    p = tmp_path / "bad.csv"
    p.write_text("Name,When\nx,4/1/23\n")
    with pytest.raises(ValueError):
        list(ni.parse_slim(p))


def test_fingerprints_match_fulcra_media_cases(ni):
    fp = ni.fingerprint_from_joined_title
    assert fp("Dune") == "movie:dune"
    assert fp("Dune: Part Two", is_episode=False) == "movie:dune-part-two"
    assert fp("The Crown: Season 4: Episode 7") == "tv:the-crown:s04e07"
    assert fp("BEEF: Season 2: Episode Title") == "tv:beef:s02:episode-title"
    assert fp("Anthology: 2021: Episode Title") == "tv:anthology:s2021:episode-title"
    assert fp("Show: Limited Series: Episode 1") == "tv:show:limited-series:e01"
    assert fp("Show: Episode Title") == "tv:show:episode-title"


def test_fingerprint_in_slim_events(ni, fixtures_dir):
    events = list(ni.parse_slim(fixtures_dir / "slim.csv"))
    assert events[2].fingerprint == "tv:dune:part-two"   # "Dune: Part Two" via slim path (is_episode=None)
    assert events[3].fingerprint == "tv:the-crown:s04e07"
