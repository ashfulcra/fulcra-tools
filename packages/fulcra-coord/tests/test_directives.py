"""Tests for the first-class Directive record schema, builder, validator, and
path helpers introduced in Phase 3a.

These are PURE tests — no backend, no cache, no remote I/O. The hermetic
conftest autouse fixture still applies (it redirects XDG_CACHE_HOME and
defaults the backend to the safe no-op), but nothing here reaches either.

Run standalone:
  pytest packages/fulcra-coord/tests/test_directives.py -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord.schema import (
    DIRECTIVE_SCHEMA,
    make_directive,
    make_directive_id,
    validate_directive,
)
from fulcra_coord.remote import (
    directives_prefix,
    directive_remote_path,
    remote_root,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIXED_DT = datetime(2026, 6, 9, 12, 0, 0, tzinfo=timezone.utc)


def _good_directive(**overrides) -> dict:
    """Minimal valid directive — override specific fields for negative tests."""
    kwargs = dict(
        directive_type="tell",
        from_agent="agent-a",
        audience="agent-b",
        title="Do the thing",
        workstream="devops",
        dt=_FIXED_DT,
    )
    kwargs.update(overrides)
    return make_directive(**kwargs)


# ---------------------------------------------------------------------------
# make_directive_id
# ---------------------------------------------------------------------------

class TestMakeDirectiveId(unittest.TestCase):

    def test_basic_format_contains_type_and_date(self):
        did = make_directive_id("tell", dt=_FIXED_DT)
        self.assertIn("tell", did.lower())
        self.assertIn("20260609", did)

    def test_ids_are_unique_across_100_calls(self):
        ids = {make_directive_id("broadcast") for _ in range(100)}
        self.assertEqual(len(ids), 100, "Expected 100 unique IDs, got collisions")

    def test_time_sortable_prefix(self):
        # Two IDs built at different times should sort in time order (prefix).
        dt1 = datetime(2026, 6, 1, tzinfo=timezone.utc)
        dt2 = datetime(2026, 6, 9, tzinfo=timezone.utc)
        id1 = make_directive_id("tell", dt=dt1)
        id2 = make_directive_id("tell", dt=dt2)
        # The date portion (YYYYMMDD) should sort correctly as a string prefix.
        self.assertLess(id1, id2)

    def test_different_types_produce_different_ids(self):
        id_tell = make_directive_id("tell", dt=_FIXED_DT)
        id_broadcast = make_directive_id("broadcast", dt=_FIXED_DT)
        self.assertNotEqual(id_tell, id_broadcast)


# ---------------------------------------------------------------------------
# make_directive — key set and value correctness
# ---------------------------------------------------------------------------

class TestMakeDirectiveKeySet(unittest.TestCase):
    """make_directive must return EXACTLY the required key set."""

    REQUIRED_KEYS = {
        "schema", "id", "directive_type", "from", "audience",
        "title", "summary", "next_action", "priority", "workstream",
        "status", "acked_by", "artifact_ref", "not_before", "due",
        "routing", "created_at", "updated_at", "task_id",
    }

    def test_all_required_keys_present(self):
        d = _good_directive()
        missing = self.REQUIRED_KEYS - set(d.keys())
        self.assertEqual(missing, set(), f"Missing keys: {missing}")

    def test_no_extra_keys(self):
        d = _good_directive()
        extra = set(d.keys()) - self.REQUIRED_KEYS
        self.assertEqual(extra, set(), f"Unexpected extra keys: {extra}")


class TestMakeDirectiveValues(unittest.TestCase):

    def test_schema_constant(self):
        d = _good_directive()
        self.assertEqual(d["schema"], DIRECTIVE_SCHEMA)
        self.assertEqual(d["schema"], "fulcra.coordination.directive.v1")

    def test_from_field_is_from_agent(self):
        d = _good_directive(from_agent="claude-code")
        self.assertEqual(d["from"], "claude-code")

    def test_audience_concrete_agent(self):
        d = _good_directive(audience="agent-b")
        self.assertEqual(d["audience"], "agent-b")

    def test_audience_broadcast_wildcard(self):
        d = _good_directive(audience="*")
        self.assertEqual(d["audience"], "*")

    def test_default_status_is_proposed(self):
        d = _good_directive()
        self.assertEqual(d["status"], "proposed")

    def test_default_priority_is_p2(self):
        d = _good_directive()
        self.assertEqual(d["priority"], "P2")

    def test_default_acked_by_is_empty_list(self):
        d = _good_directive()
        self.assertEqual(d["acked_by"], [])

    def test_default_routing_is_empty_list(self):
        d = _good_directive()
        self.assertEqual(d["routing"], [])

    def test_default_artifact_ref_is_none(self):
        d = _good_directive()
        self.assertIsNone(d["artifact_ref"])

    def test_default_task_id_is_none(self):
        d = _good_directive()
        self.assertIsNone(d["task_id"])

    def test_default_not_before_and_due_none(self):
        d = _good_directive()
        self.assertIsNone(d["not_before"])
        self.assertIsNone(d["due"])

    def test_created_at_and_updated_at_iso_z(self):
        d = _good_directive()
        self.assertTrue(d["created_at"].endswith("Z"), d["created_at"])
        self.assertTrue(d["updated_at"].endswith("Z"), d["updated_at"])
        self.assertEqual(d["created_at"], d["updated_at"])

    def test_created_at_reflects_dt_param(self):
        d = _good_directive()
        # ISO-Z format uses hyphens: 2026-06-09T...
        self.assertIn("2026-06-09", d["created_at"])

    def test_explicit_directive_id_is_honoured(self):
        d = make_directive(
            directive_type="tell",
            from_agent="a",
            audience="b",
            title="T",
            workstream="main",
            directive_id="DIR-custom-id",
            dt=_FIXED_DT,
        )
        self.assertEqual(d["id"], "DIR-custom-id")

    def test_explicit_task_id_backref(self):
        d = make_directive(
            directive_type="review",
            from_agent="a",
            audience="b",
            title="Review PR",
            workstream="devops",
            task_id="TASK-20260609-review-pr-abc12345",
            dt=_FIXED_DT,
        )
        self.assertEqual(d["task_id"], "TASK-20260609-review-pr-abc12345")

    def test_optional_fields_passed_through(self):
        d = make_directive(
            directive_type="broadcast",
            from_agent="coordinator",
            audience="*",
            title="All agents: stand down",
            workstream="ops",
            summary="Planned maintenance starting now",
            next_action="Ack this broadcast",
            priority="P1",
            status="proposed",
            not_before="2026-06-10T00:00:00Z",
            due="2026-06-10T06:00:00Z",
            artifact_ref={"type": "pr", "url": "https://github.com/foo/bar/pull/1"},
            dt=_FIXED_DT,
        )
        self.assertEqual(d["summary"], "Planned maintenance starting now")
        self.assertEqual(d["next_action"], "Ack this broadcast")
        self.assertEqual(d["priority"], "P1")
        self.assertIsNotNone(d["artifact_ref"])
        self.assertEqual(d["not_before"], "2026-06-10T00:00:00Z")
        self.assertEqual(d["due"], "2026-06-10T06:00:00Z")


# ---------------------------------------------------------------------------
# make_directive — validation / ValueError raises
# ---------------------------------------------------------------------------

class TestMakeDirectiveValidation(unittest.TestCase):

    def test_raises_on_bad_directive_type(self):
        with self.assertRaises(ValueError):
            make_directive(
                directive_type="shout",  # not in _DIRECTIVE_TYPES
                from_agent="a",
                audience="b",
                title="T",
                workstream="w",
            )

    def test_raises_on_empty_audience(self):
        with self.assertRaises(ValueError):
            make_directive(
                directive_type="tell",
                from_agent="a",
                audience="",
                title="T",
                workstream="w",
            )

    def test_raises_on_whitespace_audience(self):
        with self.assertRaises(ValueError):
            make_directive(
                directive_type="tell",
                from_agent="a",
                audience="   ",
                title="T",
                workstream="w",
            )

    def test_raises_on_empty_from_agent(self):
        with self.assertRaises(ValueError):
            make_directive(
                directive_type="tell",
                from_agent="",
                audience="b",
                title="T",
                workstream="w",
            )

    def test_raises_on_empty_title(self):
        with self.assertRaises(ValueError):
            make_directive(
                directive_type="tell",
                from_agent="a",
                audience="b",
                title="",
                workstream="w",
            )

    def test_all_valid_directive_types_accepted(self):
        for dtype in ("tell", "broadcast", "review", "verdict", "human-ask"):
            with self.subTest(dtype=dtype):
                d = make_directive(
                    directive_type=dtype,
                    from_agent="a",
                    audience="b",
                    title="T",
                    workstream="w",
                    dt=_FIXED_DT,
                )
                self.assertEqual(d["directive_type"], dtype)


# ---------------------------------------------------------------------------
# validate_directive
# ---------------------------------------------------------------------------

class TestValidateDirective(unittest.TestCase):

    def test_valid_record_returns_empty_list(self):
        d = _good_directive()
        errs = validate_directive(d)
        self.assertEqual(errs, [], f"Unexpected errors: {errs}")

    def test_wrong_schema_string(self):
        d = _good_directive()
        d["schema"] = "fulcra.coordination.task.v1"
        errs = validate_directive(d)
        self.assertTrue(any("schema" in e for e in errs), errs)

    def test_unknown_directive_type(self):
        d = _good_directive()
        d["directive_type"] = "yell"
        errs = validate_directive(d)
        self.assertTrue(any("directive_type" in e for e in errs), errs)

    def test_missing_required_id(self):
        d = _good_directive()
        del d["id"]
        errs = validate_directive(d)
        self.assertTrue(any("id" in e for e in errs), errs)

    def test_empty_audience_flagged(self):
        d = _good_directive()
        d["audience"] = ""
        errs = validate_directive(d)
        self.assertTrue(any("audience" in e for e in errs), errs)

    def test_missing_title_flagged(self):
        d = _good_directive()
        del d["title"]
        errs = validate_directive(d)
        self.assertTrue(any("title" in e for e in errs), errs)

    def test_missing_from_flagged(self):
        d = _good_directive()
        del d["from"]
        errs = validate_directive(d)
        self.assertTrue(any("from" in e for e in errs), errs)

    def test_missing_status_flagged(self):
        d = _good_directive()
        del d["status"]
        errs = validate_directive(d)
        self.assertTrue(any("status" in e for e in errs), errs)

    def test_multiple_problems_all_reported(self):
        d = _good_directive()
        del d["id"]
        d["directive_type"] = "nonsense"
        errs = validate_directive(d)
        self.assertGreaterEqual(len(errs), 2)

    def test_missing_each_schema_key_is_flagged(self):
        d = _good_directive()
        for field in sorted(TestMakeDirectiveKeySet.REQUIRED_KEYS):
            with self.subTest(field=field):
                broken = dict(d)
                del broken[field]
                errs = validate_directive(broken)
                self.assertTrue(any(field in e for e in errs), errs)

    def test_unexpected_key_is_flagged(self):
        d = _good_directive()
        d["unexpected"] = "value"
        errs = validate_directive(d)
        self.assertTrue(
            any("Unexpected field" in e and "unexpected" in e for e in errs),
            errs,
        )

    def test_invalid_priority_is_flagged(self):
        d = _good_directive()
        d["priority"] = "urgent"
        errs = validate_directive(d)
        self.assertTrue(any("priority" in e.lower() for e in errs), errs)

    def test_acked_by_and_routing_must_be_lists(self):
        d = _good_directive()
        d["acked_by"] = "agent-a"
        d["routing"] = "hop"
        errs = validate_directive(d)
        self.assertTrue(any("acked_by" in e for e in errs), errs)
        self.assertTrue(any("routing" in e for e in errs), errs)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

class TestDirectivePathHelpers(unittest.TestCase):

    def test_directives_prefix_ends_with_slash(self):
        p = directives_prefix()
        self.assertTrue(p.endswith("/"), f"Expected trailing slash: {p!r}")

    def test_directives_prefix_rooted_in_remote_root(self):
        p = directives_prefix()
        root = remote_root()
        self.assertTrue(p.startswith(root), f"{p!r} should start with {root!r}")

    def test_directive_remote_path_format(self):
        did = "DIR-20260609-tell-abc12345"
        path = directive_remote_path(did)
        root = remote_root()
        self.assertEqual(path, f"{root}/directives/{did}.json")

    def test_directive_remote_path_ends_with_json(self):
        path = directive_remote_path("some-id")
        self.assertTrue(path.endswith(".json"))

    def test_directive_path_contains_directives_segment(self):
        path = directive_remote_path("x")
        self.assertIn("/directives/", path)

    def test_directives_prefix_is_prefix_of_directive_path(self):
        did = "DIR-20260609-x"
        self.assertTrue(directive_remote_path(did).startswith(directives_prefix()))


if __name__ == "__main__":
    unittest.main()


def test_make_directive_rejects_unknown_status():
    import pytest
    with pytest.raises(ValueError):
        make_directive(directive_type="tell", from_agent="a", audience="b",
                              title="t", workstream="ws", status="banana")


def test_validate_directive_flags_unknown_status():
    d = make_directive(directive_type="tell", from_agent="a", audience="b",
                              title="t", workstream="ws")
    d["status"] = "banana"
    errs = validate_directive(d)
    assert any("status" in e.lower() for e in errs)


def test_make_directive_accepts_each_valid_status():
    for s in ("proposed", "delivered", "acked", "acted", "expired"):
        d = make_directive(directive_type="tell", from_agent="a", audience="b",
                                  title="t", workstream="ws", status=s)
        assert d["status"] == s
        assert validate_directive(d) == []
