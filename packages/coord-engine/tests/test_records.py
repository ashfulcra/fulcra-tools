"""Coord v3 control-plane record tests.

Weighted toward the fail-closed edges rather than the happy path: every bug
this substrate can have is a window that looks complete when it isn't.
"""
from __future__ import annotations

import json

import pytest

from coord_engine import records


def _rec(note, *, at="2026-07-27T00:00:00+00:00", sources=("coord-boss",), rid="r1"):
    return {"id": rid, "recorded_at": at, "sources": list(sources), "note": note}


def _payload(**kw):
    kw.setdefault("to", "codex-coder")
    kw.setdefault("kind", "directive")
    kw.setdefault("priority", "P0")
    kw.setdefault("slug", "fix-the-thing")
    return records.build_payload(**kw)


# --- payload round-trip ------------------------------------------------------

def test_payload_round_trips():
    parsed = records.parse_payload(_payload(ptr="task/fix-the-thing.md"))
    assert parsed == {"to": "codex-coder", "kind": "directive",
                      "slug": "fix-the-thing", "pri": "P0",
                      "ptr": "task/fix-the-thing.md"}


def test_payload_omits_ptr_when_there_is_no_body():
    assert "ptr" not in json.loads(_payload())


def test_build_rejects_unknown_kind():
    with pytest.raises(ValueError):
        records.build_payload(to="a", kind="gossip", priority="P1", slug="s")


def test_p0_survives_the_payload():
    """P0 is the class the router silently dropped. Pin it here too."""
    assert records.parse_payload(_payload(priority="P0"))["pri"] == "P0"


# --- "not a control-plane event" is not "malformed" --------------------------

@pytest.mark.parametrize("note", [
    None, "", "just a human note", 42, "{not json",
])
def test_non_payload_notes_are_skipped_not_errors(note):
    assert records.parse_payload(note) is None


def test_unknown_payload_version_is_ignored_rather_than_guessed():
    future = json.dumps({"v": 999, "to": "a", "kind": "directive", "slug": "s"})
    assert records.parse_payload(future) is None


def test_payload_missing_slug_is_rejected():
    bad = json.dumps({"v": 1, "to": "a", "kind": "directive", "slug": ""})
    assert records.parse_payload(bad) is None


# --- fail-closed windows -----------------------------------------------------

def test_unknown_window_propagates_as_unknown_never_empty():
    """The single most important property: None must not become []."""
    assert records.events_for(None, "codex-coder") is None


def test_empty_window_is_a_legitimate_empty_list():
    assert records.events_for([], "codex-coder") == []


def test_malformed_record_makes_the_whole_window_unknown():
    assert records.events_for([_rec(_payload()), "not-a-dict"], "codex-coder") is None


# --- addressing and ordering -------------------------------------------------

def test_only_events_addressed_to_this_agent_are_returned():
    got = records.events_for(
        [_rec(_payload(to="codex-coder")), _rec(_payload(to="someone-else"))],
        "codex-coder")
    assert [e["slug"] for e in got] == ["fix-the-thing"]


def test_broadcast_events_reach_every_agent():
    got = records.events_for(
        [_rec(_payload(to="all", slug="fleet-wide"))], "codex-coder")
    assert [e["slug"] for e in got] == ["fleet-wide"]


def test_broadcast_event_returned_exactly_once_despite_duplicate_records():
    dup = _rec(_payload(to="all", slug="fleet-wide"), rid="same-id")
    got = records.events_for([dup, dict(dup)], "codex-coder")
    assert [e["slug"] for e in got] == ["fleet-wide"]


def test_broadcast_does_not_leak_events_for_a_named_third_party():
    got = records.events_for(
        [_rec(_payload(to="someone-else", slug="not-mine"))], "codex-coder")
    assert got == []


def test_duplicate_directed_records_collapse_to_one_event():
    dup = _rec(_payload(slug="once"), rid="same-id")
    got = records.events_for([dup, dict(dup)], "codex-coder")
    assert [e["slug"] for e in got] == ["once"]


def test_events_are_ordered_oldest_first():
    got = records.events_for([
        _rec(_payload(slug="second"), at="2026-07-27T00:00:02+00:00", rid="b"),
        _rec(_payload(slug="first"), at="2026-07-27T00:00:01+00:00", rid="a"),
    ], "codex-coder")
    assert [e["slug"] for e in got] == ["first", "second"]


def test_sender_comes_from_sources_and_ignores_platform_ids():
    got = records.events_for(
        [_rec(_payload(), sources=["com.fulcradynamics.annotation.x", "coord-boss"])],
        "codex-coder")
    assert got[0]["from"] == "coord-boss"


def test_unattributed_record_yields_none_sender_not_a_crash():
    got = records.events_for([_rec(_payload(), sources=[])], "codex-coder")
    assert got[0]["from"] is None


# --- shadow comparison -------------------------------------------------------

def test_unknown_window_never_reports_match():
    """An unknown window agrees with everything; calling that a match would
    green-light a cutover on no evidence."""
    assert records.compare_to_file_fold(None, {"a"})["status"] == "unknown"


def test_identical_sets_match():
    ev = records.events_for([_rec(_payload(slug="a"))], "codex-coder")
    assert records.compare_to_file_fold(ev, {"a"})["status"] == "match"


def test_divergence_names_both_directions():
    ev = records.events_for([_rec(_payload(slug="a"))], "codex-coder")
    out = records.compare_to_file_fold(ev, {"b"})
    assert out["status"] == "divergent"
    assert out["only_in_records"] == ["a"] and out["only_in_files"] == ["b"]
