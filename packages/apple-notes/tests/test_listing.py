from datetime import datetime, timezone

from fulcra_apple_notes.listing import parse

SAMPLE = """_attachments/
683B    2026-09-05 06:58PM UTC  10-14-18-poor-5a596956.md
1KiB    2026-09-05 07:11PM UTC  note-two.md
2KiB    2026-09-05 12:05AM UTC  after-midnight.md
3KiB    2026-09-05 01:05AM UTC  one-am.md
"""


def test_directories_are_skipped():
    assert all(not e.name.endswith("/") for e in parse(SAMPLE))
    assert len(parse(SAMPLE)) == 4


def test_pm_times_parse_to_afternoon():
    e = next(e for e in parse(SAMPLE) if e.name.startswith("10-14"))
    assert e.modified == datetime(2026, 9, 5, 18, 58, tzinfo=timezone.utc)


def test_midnight_hour_is_earlier_than_one_am_despite_sorting_later():
    """The whole reason this module exists.

    Lexically "12:05AM" > "01:05AM", but it is an hour EARLIER. A freshness
    check comparing the raw strings is wrong for one hour every day.
    """
    entries = {e.name: e.modified for e in parse(SAMPLE)}
    assert entries["after-midnight.md"] < entries["one-am.md"]
    assert entries["after-midnight.md"].hour == 0
    assert entries["one-am.md"].hour == 1


def test_unparseable_lines_are_ignored_not_crashed_on():
    assert parse("garbage\n\n???") == []
