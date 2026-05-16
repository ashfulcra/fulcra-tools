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
    # Honest point-in-time at noon UTC — no fake duration
    assert e.start_time == datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    assert e.end_time == e.start_time
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
