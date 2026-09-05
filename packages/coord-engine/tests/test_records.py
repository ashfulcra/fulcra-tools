"""Coord v3 control-plane record tests.

Weighted toward the fail-closed edges rather than the happy path: every bug
this substrate can have is a window that looks complete when it isn't.
"""
from __future__ import annotations

import json

import pytest

from coord_engine import __version__, records


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
                      "ptr": "task/fix-the-thing.md", "fyi": False,
                      "for": None, "on": None, "state": None,
                      "writer": {
                          # the RELEASE, not a frozen literal: a version bump is
                          # release discipline, not a reason to edit this test
                          "engine_version": __version__,
                          "protocol_version": 1,
                          "cursor_schema_version": 2,
                      }}


def test_payload_omits_ptr_when_there_is_no_body():
    assert "ptr" not in json.loads(_payload())


def test_payload_stamps_engine_protocol_and_cursor_versions():
    assert json.loads(_payload())["writer"] == records.engine_stamp()


def test_unstamped_writer_warns():
    old = json.dumps({"v": 1, "to": "a", "kind": "claim", "pri": "P1",
                      "slug": "adopt"})
    warnings = records.observed_version_warnings([_rec(old, rid="old")])
    assert any("lack writer-version stamps" in warning for warning in warnings)


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


# --- blocked as a bus signal -------------------------------------------------
# `blocked_on` was read by seven modules and announced by none, so anything that
# wanted "what is blocked, and on whom" had to enumerate the task corpus. As an
# event it is available to anything reading the bus forward from a cursor.

def test_blocked_is_a_control_plane_kind():
    assert "blocked" in records.KINDS


def test_a_blocked_event_carries_what_it_waits_on_and_which_way_it_went():
    parsed = records.parse_payload(_payload(
        to="ash", kind="blocked", priority="P1", slug="409a",
        ptr="task/409a.md", on="user:ash", state="blocked"))
    assert parsed["kind"] == "blocked"
    assert parsed["on"] == "user:ash"
    assert parsed["state"] == "blocked"


def test_the_clear_is_carried_too():
    """A block announced but never retracted leaves every downstream queue
    growing forever, and a queue that only grows stops being read."""
    parsed = records.parse_payload(_payload(
        to="ash", kind="blocked", priority="P1", slug="409a",
        on="user:ash", state="cleared"))
    assert parsed["state"] == "cleared"


def test_an_unknown_state_fails_at_the_write():
    with pytest.raises(ValueError):
        _payload(to="ash", kind="blocked", priority="P1", slug="x", state="maybe")


def test_the_raw_blocked_on_value_is_preserved_not_classified():
    """Carried verbatim so a consumer applies its own classifier rather than
    inheriting ours — an agent name and a role must survive as written."""
    for raw in ("codex-coder", "role:build-lane", "user:ash"):
        parsed = records.parse_payload(_payload(
            to="x", kind="blocked", priority="P2", slug="s", on=raw, state="blocked"))
        assert parsed["on"] == raw


def test_an_older_reader_skips_a_blocked_event_rather_than_poisoning_on_it():
    """THE COMPATIBILITY PROPERTY that made adding a kind safe: parse_payload
    returns None for a kind it does not know, and None means 'not a
    control-plane event — skip silently'. Simulated by parsing against a KINDS
    tuple that predates this change."""
    note = _payload(to="ash", kind="blocked", priority="P1", slug="409a",
                    on="user:ash", state="blocked")
    original = records.KINDS
    try:
        records.KINDS = ("directive", "response", "verdict", "claim")
        assert records.parse_payload(note) is None
    finally:
        records.KINDS = original
    assert records.parse_payload(note) is not None
