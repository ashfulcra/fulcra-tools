from datetime import date

from fulcra_media.importers.netflix import parse_netflix_date


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
