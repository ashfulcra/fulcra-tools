"""`blocked` on the bus — the signal, not just the field.

`blocked_on` was read by seven engine modules and announced by none, so every
consumer that wanted "what is blocked, and on whom" had to enumerate the task
corpus. That is the fold-by-enumeration the stream architecture rejects, and in
practice it meant exactly one bespoke tracker bridge ever surfaced it, into
exactly one tracker. These tests pin the event.
"""

import argparse
import json

import pytest

from coord_engine import cli, records
from test_reconcile import FakeTransport


TEAM = "acme"
CFG_PATH = "team/acme/_coord/bus-v3/records.json"
DOC = """---
type: Task
title: A thing
id: a-thing
status: active
priority: P1
owner: coord-boss
assignee: codex-coder
---

# A thing
"""


class RecordingTransport(FakeTransport):
    """FakeTransport that captures bus writes instead of performing them."""

    def __init__(self):
        super().__init__()
        self.writes: list[dict] = []

    def record_write(self, data_type, api_version, note, source,
                     recorded_at=None, tags=None):
        self.writes.append({"data_type": data_type, "note": note,
                            "source": source})
        return True

    def blocked_events(self):
        out = []
        for w in self.writes:
            parsed = records.parse_payload(w["note"])
            if parsed and parsed["kind"] == "blocked":
                out.append(parsed)
        return out


def _transport():
    t = RecordingTransport()
    t.put(CFG_PATH, json.dumps({
        "data_type": "MomentAnnotation/test", "api_version": "v0"}))
    t.put(f"team/{TEAM}/task/a-thing.md", DOC)
    return t


def _args(**kw):
    base = dict(team=TEAM, name="a-thing", agent="coord-boss", status=None,
                summary=None, next=None, assignee=None, blocked_on=None,
                priority=None, evidence=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_blocking_on_the_operator_emits_a_bus_event():
    t = _transport()
    assert cli.cmd_task_update(_args(blocked_on="user:ash"), t) == 0
    events = t.blocked_events()
    assert len(events) == 1, events
    assert events[0]["to"] == "ash"          # addressed to the BLOCKER
    assert events[0]["on"] == "user:ash"     # raw, unclassified
    assert events[0]["state"] == "blocked"
    assert events[0]["slug"] == "a-thing"
    assert events[0]["ptr"] == "task/a-thing.md"
    assert events[0]["pri"] == "P1"          # the task's own priority


def test_clearing_the_block_emits_the_other_half():
    """A block announced but never retracted leaves every downstream queue
    growing forever, and a queue that only grows stops being read."""
    t = _transport()
    cli.cmd_task_update(_args(blocked_on="user:ash"), t)
    cli.cmd_task_update(_args(blocked_on=""), t)
    events = t.blocked_events()
    assert [e["state"] for e in events] == ["blocked", "cleared"]
    assert events[1]["to"] == "ash"  # the clear reaches whoever was holding it


def test_an_agent_block_is_a_signal_too_not_just_a_human_one():
    """The blocker being an agent is exactly when telling them matters."""
    t = _transport()
    cli.cmd_task_update(_args(blocked_on="codex-coder"), t)
    event = t.blocked_events()[0]
    assert event["to"] == "codex-coder" and event["on"] == "codex-coder"


def test_an_unchanged_blocked_on_emits_nothing():
    """NEGATIVE CONTROL. Every task update would otherwise re-announce the same
    block, and a signal that fires on every touch is noise, not a signal."""
    t = _transport()
    cli.cmd_task_update(_args(blocked_on="user:ash"), t)
    cli.cmd_task_update(_args(summary="unrelated edit"), t)
    assert [e["state"] for e in t.blocked_events()] == ["blocked"]


def test_an_update_that_touches_nothing_relevant_emits_nothing():
    t = _transport()
    cli.cmd_task_update(_args(summary="just a note"), t)
    assert t.blocked_events() == []


def test_a_bus_that_is_down_does_not_fail_the_update():
    """The doc is the truth and the event is delivery: a dead bus degrades
    latency, never the record of what is blocked."""
    t = _transport()

    def boom(*a, **k):
        raise RuntimeError("bus down")

    t.record_write = boom
    assert cli.cmd_task_update(_args(blocked_on="user:ash"), t) == 0
    assert "blocked_on: user:ash" in t.store[f"team/{TEAM}/task/a-thing.md"]


def test_no_bus_config_is_not_an_error_either():
    t = RecordingTransport()
    t.put(f"team/{TEAM}/task/a-thing.md", DOC)
    assert cli.cmd_task_update(_args(blocked_on="user:ash"), t) == 0
    assert t.blocked_events() == []
