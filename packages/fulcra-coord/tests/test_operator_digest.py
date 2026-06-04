"""Tests for the Operator Digest (views.build_operator_digest, cli._render_digest,
the digest command + dedup guard, emit_digest_annotation, install-digest)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import views, schema, cli

NOW = datetime(2026, 6, 4, 18, 0, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=12)


def _summary(**over):
    """A task_summary-shaped dict with sane defaults (mirrors schema.task_summary keys)."""
    base = {
        "id": "20260604-x", "title": "X", "status": "active", "priority": "P2",
        "workstream": "devops", "owner_agent": "claude-code:mb:repo",
        "assignee": None, "last_touched_by": "claude-code:mb:repo",
        "current_summary": "", "next_action": "", "blocked_on": None,
        "not_before": None, "due": None, "tags": [], "updated_at": "2026-06-04T17:00:00Z",
        "done_at": None, "acked_by": [],
    }
    base.update(over)
    return base


class TestBuildOperatorDigestEmpty(unittest.TestCase):
    def test_all_blocks_present_and_empty(self):
        d = views.build_operator_digest([], [], human="ash", now=NOW, since=SINCE)
        self.assertEqual(d["blocked_on_you"], [])
        self.assertEqual(d["upcoming"], [])
        self.assertEqual(d["per_agent"], [])
        self.assertEqual(d["stale"], [])


class TestBlockedRanking(unittest.TestCase):
    def test_due_soonest_then_oldest_age(self):
        # Three blocked-on-user asks: B due first, A&C undated; among undated,
        # oldest updated_at leads. needs:human tag makes them blocked-on-user.
        a = _summary(id="A", status="blocked", tags=["needs:human"],
                     updated_at="2026-06-04T09:00:00Z", due=None)
        b = _summary(id="B", status="blocked", tags=["needs:human"],
                     updated_at="2026-06-04T17:00:00Z",
                     due="2026-06-05T00:00:00Z")
        c = _summary(id="C", status="blocked", tags=["needs:human"],
                     updated_at="2026-06-04T08:00:00Z", due=None)
        d = views.build_operator_digest([a, b, c], [], human="ash",
                                        now=NOW, since=SINCE)
        self.assertEqual([s["id"] for s in d["blocked_on_you"]], ["B", "C", "A"])


class TestPerAgentAndWindows(unittest.TestCase):
    def test_finished_since_filters_by_done_at(self):
        recent = _summary(id="R", status="done", owner_agent="claude-code:mb:repo",
                          done_at="2026-06-04T12:00:00Z")           # after SINCE
        old = _summary(id="O", status="done", owner_agent="claude-code:mb:repo",
                       done_at="2026-06-03T12:00:00Z")              # before SINCE
        presence = [{"agent": "claude-code:mb:repo",
                     "workstreams": ["devops"], "summary": "shipping",
                     "last_seen": "2026-06-04T17:55:00Z"}]
        d = views.build_operator_digest([recent, old], presence, human="ash",
                                        now=NOW, since=SINCE)
        self.assertEqual(len(d["per_agent"]), 1)
        entry = d["per_agent"][0]
        self.assertEqual(entry["liveness"], "live")
        self.assertEqual([s["id"] for s in entry["finished_since"]], ["R"])

    def test_upcoming_and_stale_blocks(self):
        # upcoming: future not_before within 7d, blocked-on-user.
        up = _summary(id="U", status="waiting", tags=["needs:human"],
                      not_before="2026-06-06T00:00:00Z")
        # stale: active, updated_at older than the 2h default threshold.
        st = _summary(id="S", status="active", updated_at="2026-06-04T10:00:00Z")
        d = views.build_operator_digest([up, st], [], human="ash",
                                        now=NOW, since=SINCE)
        self.assertEqual([s["id"] for s in d["upcoming"]], ["U"])
        self.assertEqual([s["id"] for s in d["stale"]], ["S"])


class TestRenderDigest(unittest.TestCase):
    def _full_digest(self):
        return {
            "schema": "fulcra.coordination.operator_digest.v1",
            "human": "ash", "now": NOW.isoformat().replace("+00:00", "Z"),
            "since": SINCE.isoformat().replace("+00:00", "Z"),
            "blocked_on_you": [
                _summary(id="B1", title="Re-auth GitHub", status="blocked",
                         owner_agent="claude-code:mb:repo",
                         blocked_on="approve the OAuth scope"),
                _summary(id="B2", title="Review PR", status="waiting",
                         owner_agent="codex:mb:main"),
            ],
            "upcoming": [_summary(id="U1", title="Rotate key",
                                  not_before="2026-06-06T00:00:00Z")],
            "per_agent": [{
                "agent": "claude-code:mb:repo", "workstreams": ["devops"],
                "summary": "shipping the digest", "liveness": "live",
                "finished_since": [_summary(id="F1", title="Land annotations",
                                            status="done")],
            }],
            "stale": [_summary(id="S1", title="Old churn", status="active")],
        }

    def test_name_summarizes_counts(self):
        name, note = cli._render_digest(self._full_digest(), window="evening")
        self.assertIn("evening", name)
        self.assertIn("2 on you", name)
        self.assertIn("1 upcoming", name)

    def test_note_has_all_sections(self):
        _, note = cli._render_digest(self._full_digest(), window="evening")
        self.assertIn("Re-auth GitHub", note)
        self.assertIn("approve the OAuth scope", note)
        self.assertIn("Rotate key", note)
        self.assertIn("claude-code:mb:repo", note)
        self.assertIn("Land annotations", note)
        self.assertIn("Old churn", note)

    def test_empty_digest_is_clean(self):
        empty = {"blocked_on_you": [], "upcoming": [], "per_agent": [], "stale": []}
        name, note = cli._render_digest(empty, window="morning")
        self.assertIn("0 on you", name)
        self.assertEqual(note, "")  # no empty section headers

    def test_missing_fields_do_not_crash(self):
        # A digest with a sparse summary (only id/status) must still render.
        d = {"blocked_on_you": [{"id": "Z", "status": "blocked"}],
             "upcoming": [], "per_agent": [], "stale": []}
        name, note = cli._render_digest(d, window="morning")
        self.assertIn("1 on you", name)

    def test_long_block_caps_with_more(self):
        many = [_summary(id=f"B{i}", title=f"ask {i}", status="blocked")
                for i in range(12)]
        d = {"blocked_on_you": many, "upcoming": [], "per_agent": [], "stale": []}
        _, note = cli._render_digest(d, window="evening")
        self.assertIn("…and 4 more", note)
