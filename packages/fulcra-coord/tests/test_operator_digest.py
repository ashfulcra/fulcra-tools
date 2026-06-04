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

from fulcra_coord import views, schema

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
