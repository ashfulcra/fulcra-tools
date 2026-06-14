"""HOT-scoring correctness in map.py.

F4: a scalar `tags:` value was iterated char-by-char, dropping classification.
F5: _has_recent_decision matched the substring 'decid', firing on 'undecided'.
F3: _reverse_timestamp keyed on the raw ISO string, so equal instants spelled
    differently sorted differently.
"""
from datetime import datetime, timezone

from fulcra_vault.map import _hot_reasons, _has_recent_decision, _reverse_timestamp

NOW = datetime(2026, 6, 13, tzinfo=timezone.utc)


def test_scalar_tags_still_classified():
    assert "standing-correction" in _hot_reasons({"tags": "correction"}, "", NOW)


def test_list_tags_still_classified():
    assert "standing-correction" in _hot_reasons({"tags": ["correction"]}, "", NOW)


def test_undecided_is_not_a_recent_decision():
    body = "## Log\n- 2026-06-12T00:00:00+00:00 a: undecided whether to proceed\n"
    assert _has_recent_decision(body, NOW) is False


def test_decided_is_a_recent_decision():
    body = "## Log\n- 2026-06-12T00:00:00+00:00 a: decided to proceed\n"
    assert _has_recent_decision(body, NOW) is True


def test_reverse_timestamp_equal_for_equal_instants():
    assert _reverse_timestamp("2026-06-12T12:00:00Z") == \
        _reverse_timestamp("2026-06-12T12:00:00+00:00")


def test_reverse_timestamp_orders_newer_first():
    # sort key: smaller string sorts first, and we want newer-first
    assert _reverse_timestamp("2026-06-12T13:00:00+00:00") < \
        _reverse_timestamp("2026-06-12T12:00:00+00:00")
