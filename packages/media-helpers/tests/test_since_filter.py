"""Tests for fulcra_media.since_filter.parse_since / parse_window."""

from datetime import datetime, timezone

import pytest

from fulcra_media.since_filter import parse_since, parse_window


_NOW = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)


def test_none_returns_none():
    assert parse_since(None) is None


def test_empty_string_returns_none():
    assert parse_since("") is None
    assert parse_since("   ") is None


def test_all_returns_none():
    assert parse_since("all") is None
    assert parse_since("ALL") is None
    assert parse_since(" All ") is None


def test_relative_days():
    out = parse_since("30d", now=_NOW)
    assert out is not None
    assert (_NOW - out).days == 30


def test_relative_weeks():
    out = parse_since("2w", now=_NOW)
    assert out is not None
    assert (_NOW - out).days == 14


def test_relative_months_approx_30_days():
    out = parse_since("3m", now=_NOW)
    assert out is not None
    assert (_NOW - out).days == 90


def test_relative_years():
    out = parse_since("1y", now=_NOW)
    assert out is not None
    # 1y -> 365 days
    assert (_NOW - out).days == 365


def test_relative_case_insensitive_and_spaces():
    out = parse_since(" 1Y ", now=_NOW)
    assert out is not None
    assert (_NOW - out).days == 365


def test_absolute_date():
    out = parse_since("2024-01-01", now=_NOW)
    assert out == datetime(2024, 1, 1, tzinfo=timezone.utc)


def test_garbage_raises():
    with pytest.raises(ValueError, match="Unrecognised window spec"):
        parse_since("yesterday")


def test_bad_unit_raises():
    # "30x" doesn't match the relative pattern and isn't a date
    with pytest.raises(ValueError):
        parse_since("30x")


def test_returns_utc_aware():
    out = parse_since("7d", now=_NOW)
    assert out.tzinfo is not None
    assert out.utcoffset().total_seconds() == 0


def test_default_now_is_utc_now():
    # Smoke: without explicit `now`, returns something close to current UTC.
    out = parse_since("0d")
    assert out is not None
    diff = abs((datetime.now(timezone.utc) - out).total_seconds())
    assert diff < 5  # very loose; just ensures we're using UTC now


# parse_window is the new canonical name (parse_since is a back-compat alias).
# These tests pin the alias relationship and the until-style usage shape.
def test_parse_window_is_same_callable_as_parse_since():
    # Not just behaviour-equivalent — literally the same function. This
    # protects against someone splitting them and forgetting to keep the
    # window spec format symmetric across since and until.
    assert parse_window is parse_since


def test_parse_window_absolute_date_for_until_usage():
    # The typical 'until' use case: user pins to the date their realtime
    # source started, and we treat that as the upper bound.
    out = parse_window("2025-06-01", now=_NOW)
    assert out == datetime(2025, 6, 1, tzinfo=timezone.utc)


def test_parse_window_empty_means_no_bound():
    # Empty string is the default for the 'until' setting — no upper bound.
    assert parse_window("") is None
    assert parse_window(None) is None
