from datetime import date

from fulcra_media.importers.netflix import (
    make_note_and_title,
    parse_netflix_date,
)


def test_parse_netflix_date_full_year():
    assert parse_netflix_date("5/12/26") == date(2026, 5, 12)
    assert parse_netflix_date("1/1/10") == date(2010, 1, 1)
    assert parse_netflix_date("12/31/99") == date(2099, 12, 31)


def test_parse_netflix_date_single_digit_month_and_day():
    assert parse_netflix_date("6/4/10") == date(2010, 6, 4)


def test_parse_netflix_date_invalid_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_netflix_date("2024-05-12")
    with pytest.raises(ValueError):
        parse_netflix_date("")
    with pytest.raises(ValueError):
        parse_netflix_date("13/45/26")


def test_make_note_and_title_movie_no_colon():
    note, title = make_note_and_title("Tetris")
    assert note == "Tetris"
    assert title == "Tetris"


def test_make_note_and_title_episode_three_parts():
    note, title = make_note_and_title("Stranger Things: Season 1: Chapter Three: The Body")
    # All trailing parts after the show stay in the episode portion
    assert title == "Stranger Things"
    assert "Stranger Things" in note
    assert "Season 1" in note
    assert "Chapter Three: The Body" in note


def test_make_note_and_title_episode_two_parts():
    note, title = make_note_and_title("Slow Horses: Failure's Contagious")
    assert title == "Slow Horses"
    assert note == "Slow Horses: Failure's Contagious"


def test_make_note_and_title_leading_colon_malformed():
    # Real Netflix data has rows like " : Episode 10" where the show name is missing
    note, title = make_note_and_title(" : Episode 10")
    assert note == ": Episode 10"
    assert title == ""


from datetime import datetime, timezone
from pathlib import Path

from fulcra_media.importers.netflix import parse_slim
from fulcra_media.importers.base import NormalizedEvent


FIXTURE = Path(__file__).parent / "fixtures" / "netflix_slim_small.csv"


def test_parse_slim_yields_one_event_per_row():
    events = list(parse_slim(FIXTURE))
    assert len(events) == 8


def test_parse_slim_first_event_is_movie():
    events = list(parse_slim(FIXTURE))
    e = events[0]
    assert isinstance(e, NormalizedEvent)
    assert e.importer == "netflix-slim"
    assert e.service == "netflix"
    assert e.category == "watched"
    assert e.note == "Movie One"
    assert e.title == "Movie One"
    # Honest point-in-time at noon UTC + 1s sentinel so Fulcra indexes it
    # (the API silently drops events with start_time == end_time).
    assert e.start_time == datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)
    assert e.end_time == datetime(2026, 5, 12, 12, 0, 1, tzinfo=timezone.utc)
    assert e.timestamp_confidence == "low"
    assert e.external_ids["time_estimated"] is True
    assert e.external_ids["point_in_time"] is True
    assert "duration_estimated" not in e.external_ids


def test_parse_slim_same_day_rewatch_gets_distinct_ids():
    """The 27 real-data same-day rewatches must each produce a unique annotation."""
    events = list(parse_slim(FIXTURE))
    # rows 4 and 5 (zero-indexed 3 and 4): same date, same raw title
    a, b = events[3], events[4]
    assert a.start_time == b.start_time
    assert a.note == b.note
    assert a.deterministic_id != b.deterministic_id
    # And both deterministic_ids start with the expected prefix
    assert a.deterministic_id.startswith("com.fulcra.media.netflix.")
    assert b.deterministic_id.startswith("com.fulcra.media.netflix.")


def test_parse_slim_clean_same_day_rewatch_also_distinct():
    events = list(parse_slim(FIXTURE))
    a, b = events[5], events[6]  # "Show B: ... Episode 1" twice on 5/1/26
    assert a.note == b.note
    assert a.deterministic_id != b.deterministic_id


def test_parse_slim_deterministic_id_is_stable_across_runs():
    """Same CSV in -> same IDs out."""
    a = list(parse_slim(FIXTURE))
    b = list(parse_slim(FIXTURE))
    assert [e.deterministic_id for e in a] == [e.deterministic_id for e in b]


def test_parse_slim_malformed_leading_colon_row_has_empty_title():
    events = list(parse_slim(FIXTURE))
    e = events[3]
    assert e.title == ""
    assert e.note  # non-empty


from fulcra_media.importers.netflix import parse_rich

RICH_FIXTURE = Path(__file__).parent / "fixtures" / "netflix_rich_small.csv"


def test_parse_rich_filters_trailers():
    events = list(parse_rich(RICH_FIXTURE))
    assert len(events) == 5
    assert all("Trailer" not in e.title for e in events)


def test_parse_rich_movie_first_event():
    events = list(parse_rich(RICH_FIXTURE))
    e = next(e for e in events if "Dune" in e.title)
    assert e.importer == "netflix-rich"
    assert e.service == "netflix"
    assert e.category == "watched"
    assert e.note == "Dune: Part Two"
    # Movie subtitles are preserved — no episode markers in the title
    assert e.title == "Dune: Part Two"
    assert e.start_time == datetime(2026, 5, 12, 20, 32, 15, tzinfo=timezone.utc)
    assert e.end_time == datetime(2026, 5, 12, 22, 14, 45, tzinfo=timezone.utc)
    assert e.timestamp_confidence == "high"
    assert e.external_ids["profile"] == "Ash"
    assert "Apple TV" in e.external_ids["device_type"]
    assert e.external_ids["country"].startswith("US")
    assert "time_estimated" not in e.external_ids
    assert "duration_estimated" not in e.external_ids
    assert "point_in_time" not in e.external_ids


def test_parse_rich_episode_title_is_show_name():
    """For shows, title is the show name (first colon-separated segment)."""
    events = list(parse_rich(RICH_FIXTURE))
    e = next(e for e in events if "Severance" in e.note and "We We Are" in e.note)
    assert e.title == "Severance"
    assert "Season 2" in e.note


def test_parse_rich_movie_no_colon_title_preserved():
    events = list(parse_rich(RICH_FIXTURE))
    e = next(e for e in events if "Killers" in e.title)
    assert e.title == "Killers of the Flower Moon"
    assert e.note == "Killers of the Flower Moon"


def test_parse_rich_idempotency_key_per_session():
    events = list(parse_rich(RICH_FIXTURE))
    ids = [e.deterministic_id for e in events]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("com.fulcra.media.netflix-rich.") for i in ids)


def test_parse_rich_duration_parsed_to_seconds():
    events = list(parse_rich(RICH_FIXTURE))
    e = next(e for e in events if e.title == "Killers of the Flower Moon")
    # 2:38:45 = 9525 s
    assert (e.end_time - e.start_time).total_seconds() == 9525


def test_parse_rich_rejects_slim_header(tmp_path):
    csv = tmp_path / "slim.csv"
    csv.write_text('Title,Date\n"Movie","5/12/26"\n')
    import pytest
    with pytest.raises(ValueError, match="parse_rich handles the 10-column"):
        list(parse_rich(csv))


from fulcra_media.importers.netflix import parse_auto


def test_parse_auto_routes_slim():
    events = list(parse_auto(FIXTURE))
    assert all(e.importer == "netflix-slim" for e in events)
    assert len(events) == 8


def test_parse_auto_routes_rich():
    events = list(parse_auto(RICH_FIXTURE))
    assert all(e.importer == "netflix-rich" for e in events)
    assert len(events) == 5


def test_parse_auto_rejects_unknown_header(tmp_path):
    csv = tmp_path / "weird.csv"
    csv.write_text("Foo,Bar\n1,2\n")
    import pytest
    with pytest.raises(ValueError, match="unrecognized Netflix CSV"):
        list(parse_auto(csv))


def test_parse_slim_includes_content_fingerprint():
    events = list(parse_slim(FIXTURE))
    movie = next(e for e in events if e.title == "Movie One")
    assert movie.external_ids["content_fingerprint"] == "movie:movie-one"
    ep = next(e for e in events if e.note == "Show A: Season 1: Episode 1")
    assert ep.external_ids["content_fingerprint"].startswith("tv:show-a:")


def test_parse_rich_includes_content_fingerprint():
    events = list(parse_rich(RICH_FIXTURE))
    movie = next(e for e in events if e.title == "Dune: Part Two")
    assert movie.external_ids["content_fingerprint"] == "movie:dune-part-two"
    ep = next(e for e in events if "We We Are" in e.note)
    assert ep.external_ids["content_fingerprint"] == "tv:severance:s02e01"
