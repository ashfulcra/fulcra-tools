"""Role-alias addressing (multi-holder fan-out) for fulcra-coord.

A directive can be addressed to a logical ROLE (``@coord-maintainer``) rather
than a frozen agent id. Delivery resolves the role at READ time against the
calling agent's declared capabilities (its presence ``capabilities``/roles), so
the directive reaches whoever currently HOLDS the role — every live holder
(fan-out), and nobody who doesn't.

This is the fix for silent message loss: a directive to "the coord maintainer"
used to be pinned to one frozen, presence-stale agent id and rotted in a dead
inbox. Addressing the ROLE means the live holder(s) see it.

Two tiers of test:

  * PURE unit tests for the ``is_role_audience`` / ``role_of`` helpers and the
    ``inbox_for`` role-resolution membership logic (no backend, no I/O).
  * INTEGRATION tests (coord_backend) driving presence writes + ``cmd_inbox``
    end-to-end, proving the calling agent's roles are loaded from its presence
    record and threaded into membership — including multi-holder fan-out.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import inbox, presence, schema, views


# ---------------------------------------------------------------------------
# Helpers — pure
# ---------------------------------------------------------------------------

def _directive(assignee, *, owner="boss:h:r", status="proposed",
               task_id=None, title="do the thing"):
    """A minimal directive summary as inbox_for consumes (summaries fast-path).

    inbox_for reads assignee/status/owner_agent/acked_by/priority/updated_at —
    all flat on a summary — so a dict with those keys is sufficient.
    """
    return {
        "id": task_id or f"TASK-{abs(hash((assignee, title))) % 10_000_000:07d}",
        "title": title,
        "assignee": assignee,
        "owner_agent": owner,
        "status": status,
        "priority": "P2",
        "updated_at": "2026-06-09T12:00:00Z",
        "acked_by": [],
    }


# ---------------------------------------------------------------------------
# is_role_audience / role_of — pure unit tests
# ---------------------------------------------------------------------------

class TestIsRoleAudience:

    def test_at_role_is_role_audience(self):
        assert views.is_role_audience("@coord-maintainer") is True

    def test_short_role_is_role_audience(self):
        assert views.is_role_audience("@x") is True

    def test_concrete_agent_is_not_role_audience(self):
        assert views.is_role_audience("claude-code:host:repo") is False

    def test_plain_name_is_not_role_audience(self):
        assert views.is_role_audience("x") is False

    def test_broadcast_is_not_role_audience(self):
        assert views.is_role_audience("*") is False

    def test_empty_is_not_role_audience(self):
        assert views.is_role_audience("") is False

    def test_bare_at_is_not_role_audience(self):
        # "@" with no role name is malformed, not a role audience.
        assert views.is_role_audience("@") is False

    def test_none_is_not_role_audience(self):
        assert views.is_role_audience(None) is False


class TestRoleOf:

    def test_strips_at(self):
        assert views.role_of("@coord-maintainer") == "coord-maintainer"

    def test_strips_at_short(self):
        assert views.role_of("@x") == "x"


# ---------------------------------------------------------------------------
# inbox_for role-resolution — pure membership tests
# ---------------------------------------------------------------------------

class TestInboxForRoleResolution:

    def test_role_directive_seen_by_holder(self):
        d = _directive("@coord-maintainer")
        got = views.inbox_for("alice:h:r", [d], roles={"coord-maintainer"})
        assert [s["id"] for s in got] == [d["id"]]

    def test_role_directive_not_seen_by_non_holder(self):
        d = _directive("@coord-maintainer")
        got = views.inbox_for("bob:h:r", [d], roles={"reviewer"})
        assert got == []

    def test_role_directive_not_seen_when_no_roles(self):
        d = _directive("@coord-maintainer")
        got = views.inbox_for("nobody:h:r", [d], roles=set())
        assert got == []

    def test_role_directive_not_seen_when_roles_none(self):
        # roles defaults to None -> empty role set -> no role directives.
        d = _directive("@coord-maintainer")
        got = views.inbox_for("nobody:h:r", [d])
        assert got == []

    def test_multi_holder_fan_out(self):
        d = _directive("@coord-maintainer")
        a = views.inbox_for("alice:h:r", [d], roles={"coord-maintainer"})
        b = views.inbox_for("bob:h2:r", [d], roles={"coord-maintainer", "reviewer"})
        assert [s["id"] for s in a] == [d["id"]]
        assert [s["id"] for s in b] == [d["id"]]

    def test_concrete_assignee_unchanged_regression(self):
        # A concrete-assignee directive behaves EXACTLY as before — roles are
        # irrelevant to it.
        d = _directive("alice:h:r")
        assert [s["id"] for s in views.inbox_for("alice:h:r", [d], roles=set())] == [d["id"]]
        assert views.inbox_for("bob:h:r", [d], roles={"coord-maintainer"}) == []

    def test_broadcast_unchanged_regression(self):
        # A "*" broadcast still reaches everyone regardless of roles.
        d = _directive("*")
        assert [s["id"] for s in views.inbox_for("alice:h:r", [d], roles=set())] == [d["id"]]
        assert [s["id"] for s in views.inbox_for("bob:h:r", [d], roles={"x"})] == [d["id"]]

    def test_role_holder_does_not_get_other_roles_directive(self):
        # Holding coord-maintainer must NOT pull in a @reviewer directive.
        d = _directive("@reviewer")
        assert views.inbox_for("alice:h:r", [d], roles={"coord-maintainer"}) == []

    def test_already_acked_role_directive_cleared(self):
        d = _directive("@coord-maintainer")
        d["acked_by"] = ["alice:h:r"]
        got = views.inbox_for("alice:h:r", [d], roles={"coord-maintainer"})
        assert got == []

    def test_role_directive_not_open_status_excluded(self):
        d = _directive("@coord-maintainer", status="active")
        assert views.inbox_for("alice:h:r", [d], roles={"coord-maintainer"}) == []


# ---------------------------------------------------------------------------
# Integration — presence-declared roles drive cmd_inbox membership
# ---------------------------------------------------------------------------

def _connect_with_roles(agent, roles, *, backend):
    """Write a durable presence record declaring ``roles`` for ``agent``."""
    rec = schema.make_presence(agent, capabilities=list(roles))
    presence._write_presence(rec, backend=backend)
    return rec


def _tell_role(from_agent, role_audience, title, *, backend):
    """Create a directive addressed to a @role via cmd_tell."""
    from fulcra_coord import lifecycle
    args = SimpleNamespace(
        assignee=role_audience, title=title, workstream="general",
        priority="P2", summary="", next=None,
    )
    setattr(args, "from", from_agent)
    rc = lifecycle.cmd_tell(args, backend=backend)
    assert rc == 0


def _inbox_ids(agent, *, backend):
    """Run cmd_inbox in json mode and return the directive ids it surfaces."""
    args = SimpleNamespace(agent=agent, format="json", ack=None, all=False)
    items = inbox._load_inbox(agent, backend=backend)
    return {i["id"] for i in items}


def test_tell_role_assignee_is_literal_at_role(coord_backend):
    """tell @coord-maintainer stores assignee == '@coord-maintainer' (NOT
    resolved at send time — delivery-time resolution is the whole point)."""
    from fulcra_coord.io import _load_task_summaries
    _tell_role("boss:h:r", "@coord-maintainer", "fix the bus", backend=coord_backend)
    summaries = _load_task_summaries(backend=coord_backend)
    matching = [s for s in summaries if s.get("title") == "fix the bus"]
    assert matching, "directive was not created"
    assert matching[0]["assignee"] == "@coord-maintainer"


def test_role_directive_in_holder_inbox(coord_backend):
    """A @coord-maintainer directive lands in the inbox of an agent whose
    presence capabilities include coord-maintainer."""
    _connect_with_roles("alice:h:r", {"coord-maintainer"}, backend=coord_backend)
    _tell_role("boss:h:r", "@coord-maintainer", "fix the bus", backend=coord_backend)
    ids = _inbox_ids("alice:h:r", backend=coord_backend)
    titles = {s["title"] for s in inbox._load_inbox("alice:h:r", backend=coord_backend)}
    assert "fix the bus" in titles


def test_role_directive_absent_for_non_holder(coord_backend):
    """An agent WITHOUT the role does not see the @role directive."""
    _connect_with_roles("bob:h:r", {"reviewer"}, backend=coord_backend)
    _tell_role("boss:h:r", "@coord-maintainer", "fix the bus", backend=coord_backend)
    titles = {s["title"] for s in inbox._load_inbox("bob:h:r", backend=coord_backend)}
    assert "fix the bus" not in titles


def test_role_directive_multi_holder_fan_out(coord_backend):
    """TWO agents both holding coord-maintainer BOTH see the @role directive."""
    _connect_with_roles("alice:h:r", {"coord-maintainer"}, backend=coord_backend)
    _connect_with_roles("bob:h2:r", {"coord-maintainer"}, backend=coord_backend)
    _tell_role("boss:h:r", "@coord-maintainer", "fix the bus", backend=coord_backend)
    a = {s["title"] for s in inbox._load_inbox("alice:h:r", backend=coord_backend)}
    b = {s["title"] for s in inbox._load_inbox("bob:h2:r", backend=coord_backend)}
    assert "fix the bus" in a
    assert "fix the bus" in b


def test_agent_with_no_capabilities_sees_no_role_directives(coord_backend):
    """An agent with no declared roles sees only concrete/broadcast directives,
    never role ones."""
    _connect_with_roles("carol:h:r", set(), backend=coord_backend)
    _tell_role("boss:h:r", "@coord-maintainer", "role-only", backend=coord_backend)
    # A concrete directive to carol DOES land.
    from fulcra_coord import lifecycle
    cargs = SimpleNamespace(
        assignee="carol:h:r", title="concrete-for-carol", workstream="general",
        priority="P2", summary="", next=None,
    )
    setattr(cargs, "from", "boss:h:r")
    assert lifecycle.cmd_tell(cargs, backend=coord_backend) == 0

    titles = {s["title"] for s in inbox._load_inbox("carol:h:r", backend=coord_backend)}
    assert "role-only" not in titles
    assert "concrete-for-carol" in titles


def test_old_presence_without_capabilities_no_crash(coord_backend):
    """A presence record predating the capabilities field (None) is treated as
    an empty role set — no crash, no role directives surfaced."""
    # Build a record then strip capabilities entirely, as an old bus would have.
    rec = schema.make_presence("dave:h:r")
    rec.pop("capabilities", None)
    presence._write_presence(rec, backend=coord_backend)
    _tell_role("boss:h:r", "@coord-maintainer", "role-only", backend=coord_backend)
    # Must not raise, and dave (no roles) sees nothing role-directed.
    titles = {s["title"] for s in inbox._load_inbox("dave:h:r", backend=coord_backend)}
    assert "role-only" not in titles
