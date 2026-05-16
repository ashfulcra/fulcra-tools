from datetime import date, timedelta

from fulcra_media.importers.netflix import (
    estimate_duration,
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


def test_estimate_duration_movie_no_colon():
    assert estimate_duration("Tetris") == timedelta(minutes=100)


def test_estimate_duration_episode_with_season_marker():
    assert estimate_duration("Show: Season 1: Ep") == timedelta(minutes=30)
    assert estimate_duration("Show: Limited Series: Episode 1") == timedelta(minutes=30)


def test_estimate_duration_two_part_default():
    assert estimate_duration("Some Show: Some Title") == timedelta(minutes=45)
