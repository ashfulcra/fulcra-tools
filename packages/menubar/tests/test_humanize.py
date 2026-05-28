import pytest

from fulcra_menubar._humanize import humanize_minutes, parse_duration_seconds


def test_under_one_hour():
    assert humanize_minutes(30) == "30 minutes"


def test_one_hour():
    assert humanize_minutes(60) == "1 hour"


def test_exact_hours():
    assert humanize_minutes(360) == "6 hours"


def test_mixed_hours_minutes():
    assert humanize_minutes(90) == "1h 30m"


def test_one_day():
    assert humanize_minutes(1440) == "1 day"


def test_exact_days():
    assert humanize_minutes(2880) == "2 days"


def test_one_minute():
    assert humanize_minutes(1) == "1 minute"


def test_zero():
    assert humanize_minutes(0) == "0 minutes"


# ---------------------------------------------------------------------------
# parse_duration_seconds — used by the quick-record popover's inline
# duration input on Duration-type annotation rows.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_seconds"),
    [
        ("90m", 90 * 60),
        ("90 m", 90 * 60),
        ("1h 30m", 90 * 60),
        ("1h30m", 90 * 60),
        ("45 min", 45 * 60),
        ("45 minutes", 45 * 60),
        ("2h", 2 * 60 * 60),
        ("2 hours", 2 * 60 * 60),
        ("2hr", 2 * 60 * 60),
        ("30s", 30),
        ("30 sec", 30),
        ("30 seconds", 30),
        ("1h 30m 15s", 3600 + 30 * 60 + 15),
        # Bare integer is interpreted as minutes (the common shorthand).
        ("90", 90 * 60),
        # Mixed casing tolerated.
        ("1H 30M", 90 * 60),
        # Surrounding whitespace tolerated.
        ("  45m  ", 45 * 60),
    ],
)
def test_parse_duration_accepts_common_forms(text, expected_seconds):
    assert parse_duration_seconds(text) == float(expected_seconds)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "abc",
        "1 30",          # missing unit on first token
        "1h 30",         # missing unit on second token
        "1.5",           # bare decimal is ambiguous
        "h",             # no magnitude
        "0m",            # zero duration not useful
        "garbage 1h",    # leading garbage
    ],
)
def test_parse_duration_rejects_garbage(text):
    assert parse_duration_seconds(text) is None


def test_parse_duration_none_input():
    assert parse_duration_seconds(None) is None  # type: ignore[arg-type]
