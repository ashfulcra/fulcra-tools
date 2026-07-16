"""Role inboxes in the READ folds — `briefing`, `inbox`, `needs-me`.

AGENTS.md has told the fleet for months that `briefing` prints "your identity,
role inboxes, and everything that needs you" and that the fold IS your work
queue. Until this suite it did not: only `listen` expanded a role assignee, so a
role-addressed `tell` landed in the store, returned 0, and never surfaced for the
holder. These tests pin the promise.

The load-bearing one is the LAST group: a role lookup that fails is UNKNOWN, not
"no roles". If it folded to an empty held-role set the fold would render a clean,
role-blind queue indistinguishable from "you have no role work" — the same class
of silent failure, one layer down, and worse than the original bug because the
doc promise would then be true-except-when-it-silently-isn't.
"""

import json

import pytest

from coord_engine import cli, okf, reconcile, tasks
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

TEAM = "r"
NOW = "2026-07-10T00:00:00Z"
TODAY = "2026-07-10"

# clock-pin (see #378): folds compute lease freshness off cli._now() against the
# real clock, so unpinned fixtures rot the day they age past an SLA.
from datetime import datetime, timezone

PINNED_NOW = datetime(2026, 7, 10, 0, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _put_role(t, role, *, sla_hours=8760000, policy="shared"):
    t.put(cli._role_doc_path(TEAM, role),
          f"---\ntype: Role\npolicy: {policy}\nsla_hours: {sla_hours}\n---\n")


def _put_lease(t, role, agent, *, ts=NOW):
    t.put(cli._leases_prefix(TEAM, role) + f"{tasks.agent_key(agent)}.md",
          f"---\ntype: Lease\nagent: {agent}\ntimestamp: {ts}\n---\n")


def _put_directive(t, slug, title, *, owner, assignee, status="proposed"):
    fm = {"type": "Task", "id": slug, "title": title, "status": status,
          "priority": "P2", "owner": owner, "assignee": assignee}
    t.put(cli._task_path(TEAM, slug), okf.render_frontmatter(fm) + "\nbody\n")


def _reconcile(t):
    reconcile.reconcile(t, TEAM, now=NOW, today=TODAY, host="h")


def _team_with_role_directive(transport_cls=FakeTransport, *, holder="bob",
                              lease_ts=NOW, sla_hours=8760000):
    t = transport_cls()
    _put_role(t, "reviewer", sla_hours=sla_hours)
    _put_lease(t, "reviewer", holder, ts=lease_ts)
    _put_directive(t, "role-do-1", "Review the PR", owner="alice", assignee="reviewer")
    _reconcile(t)
    return t


def _briefing(t, agent, capsys):
    assert cli.main(["briefing", TEAM, "-a", agent, "--json"], transport=t) == 0
    return json.loads(capsys.readouterr().out)


# --- the promise ----------------------------------------------------------

def test_role_directive_surfaces_in_holders_briefing(capsys):
    t = _team_with_role_directive()
    b = _briefing(t, "bob", capsys)
    assert [r["name"] for r in b["inbox"]] == ["role-do-1"]
    assert [r["name"] for r in b["needs_me"]] == ["role-do-1"]
    assert "role_degraded" not in b


def test_role_directive_absent_for_non_holder(capsys):
    t = _team_with_role_directive(holder="bob")
    b = _briefing(t, "carol", capsys)
    assert b["inbox"] == [] and b["needs_me"] == []
    assert "role_degraded" not in b


def test_role_directive_surfaces_in_inbox_and_needs_me_verbs(capsys):
    t = _team_with_role_directive()
    assert cli.main(["inbox", TEAM, "-a", "bob", "--json"], transport=t) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["role-do-1"]
    assert cli.main(["needs-me", TEAM, "--agent", "bob", "--json"], transport=t) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["role-do-1"]
    # non-holder sees neither
    assert cli.main(["inbox", TEAM, "-a", "carol", "--json"], transport=t) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert cli.main(["needs-me", TEAM, "--agent", "carol", "--json"], transport=t) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_stale_lease_hides_role_directive(capsys):
    # Same rule `listen` already applies (test_listen: lease expiry stops it): a
    # holder is whoever holds a FRESH lease per the role's own sla_hours. A stale
    # lease is not a holder, so the directive is not this agent's work.
    t = _team_with_role_directive(lease_ts="2020-01-01T00:00:00Z", sla_hours=24)
    b = _briefing(t, "bob", capsys)
    assert b["inbox"] == [] and b["needs_me"] == []
    assert "role_degraded" not in b  # a stale lease is KNOWN, not degraded


def test_ack_hides_a_role_routed_directive(capsys):
    t = _team_with_role_directive()
    assert cli.main(["inbox", TEAM, "-a", "bob", "--ack", "role-do-1"], transport=t) == 0
    capsys.readouterr()
    b = _briefing(t, "bob", capsys)
    assert b["inbox"] == []


# --- fail-closed: a failed lookup is UNKNOWN, never "no roles" -------------

class LeaseListFails(FakeTransport):
    """The role's lease listing raises — holder membership is UNKNOWN."""

    def list_dir(self, prefix):
        if prefix.endswith("/leases/"):
            raise TransportError("boom")
        return super().list_dir(prefix)


def test_briefing_role_resolution_failure_is_loud_json(capsys):
    t = _team_with_role_directive(LeaseListFails)
    b = _briefing(t, "bob", capsys)
    assert b["role_degraded"] == {"type": "role-degraded", "roles": ["reviewer"]}
    # and the queue must not read as a clean "nothing for you"
    assert b["inbox"] == [] and b["needs_me"] == []


def test_briefing_role_resolution_failure_is_loud_text(capsys):
    t = _team_with_role_directive(LeaseListFails)
    assert cli.main(["briefing", TEAM, "-a", "bob"], transport=t) == 0
    out = capsys.readouterr().out
    assert "role resolution degraded: reviewer" in out
    assert "unknown" in out


def test_needs_me_role_resolution_failure_is_loud(capsys):
    t = _team_with_role_directive(LeaseListFails)
    assert cli.main(["needs-me", TEAM, "--agent", "bob", "--json"], transport=t) == 0
    rows = json.loads(capsys.readouterr().out)
    assert any(r.get("type") == "role-degraded" and r.get("roles") == ["reviewer"]
               for r in rows)
    assert cli.main(["needs-me", TEAM, "--agent", "bob"], transport=t) == 0
    assert "role resolution degraded: reviewer" in capsys.readouterr().out


def test_inbox_role_resolution_failure_is_loud(capsys):
    t = _team_with_role_directive(LeaseListFails)
    assert cli.main(["inbox", TEAM, "-a", "bob", "--json"], transport=t) == 0
    rows = json.loads(capsys.readouterr().out)
    assert any(r.get("type") == "role-degraded" and r.get("roles") == ["reviewer"]
               for r in rows)
    assert cli.main(["inbox", TEAM, "-a", "bob"], transport=t) == 0
    assert "role resolution degraded: reviewer" in capsys.readouterr().out


def test_roles_listing_failure_degrades_every_candidate(capsys):
    # The roles/ listing is what settles "is this assignee a role at all". If it
    # RAISES, membership is unknown for every foreign assignee — including the
    # ones that look like literal agent ids. Loud and fail-closed: noisy is the
    # correct answer here, because we genuinely cannot tell whether a name routes.
    class RolesListFails(FakeTransport):
        def list_dir(self, prefix):
            if prefix == f"team/{TEAM}/roles/":
                raise TransportError("boom")
            return super().list_dir(prefix)

    t = RolesListFails()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")
    _put_directive(t, "role-do-1", "Review", owner="alice", assignee="reviewer")
    _put_directive(t, "for-carol", "Theirs", owner="alice", assignee="carol")
    _reconcile(t)
    b = _briefing(t, "bob", capsys)
    # reviewer's doc still reads, so it resolves and its directive DOES surface —
    # a broken listing must not cost us work we can in fact route.
    assert [r["name"] for r in b["inbox"]] == ["role-do-1"]
    # carol is unknowable (doc absent AND listing unreadable) -> visibly degraded
    assert b["role_degraded"]["roles"] == ["carol"]


def test_role_doc_unreadable_while_listed_is_degraded_not_a_non_role(capsys):
    # The disambiguation `_role_fresh_holders` owns: listed-but-unreadable is a
    # transport failure, not "that assignee isn't a role".
    class RoleDocFails(FakeTransport):
        def read(self, path):
            if path == cli._role_doc_path(TEAM, "reviewer"):
                return None
            return super().read(path)

    t = _team_with_role_directive(RoleDocFails)
    b = _briefing(t, "bob", capsys)
    assert b["role_degraded"]["roles"] == ["reviewer"]


def test_literal_agent_assignee_is_not_degraded(capsys):
    # A directive addressed to another literal agent id is affirmatively NOT a
    # role (absent from the roles/ listing) — resolving it must stay quiet.
    t = FakeTransport()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")
    _put_directive(t, "for-carol", "Not yours", owner="alice", assignee="carol")
    _reconcile(t)
    b = _briefing(t, "bob", capsys)
    assert "role_degraded" not in b
    assert b["inbox"] == []


# --- cost: one resolution pass, shared by both folds ----------------------

class ListCountingTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.lists: list[str] = []
        self.reads: list[str] = []

    def list_dir(self, prefix):
        self.lists.append(prefix)
        return super().list_dir(prefix)

    def read(self, path):
        self.reads.append(path)
        return super().read(path)


def test_no_role_shaped_assignees_costs_no_role_reads(capsys):
    # The bound that keeps `briefing` cheap: role resolution is O(distinct
    # foreign assignees on OPEN rows). A team whose open work is all self- or
    # broadcast-addressed pays nothing at all.
    t = ListCountingTransport()
    _put_directive(t, "mine", "Mine", owner="alice", assignee="bob")
    _put_directive(t, "all", "Everyone", owner="alice", assignee="*")
    _reconcile(t)
    t.lists.clear(); t.reads.clear()
    _briefing(t, "bob", capsys)
    assert not [p for p in t.reads if "/roles/" in p]
    assert not [p for p in t.lists if "/roles/" in p]


def test_literal_agent_assignees_cost_no_role_doc_reads(capsys):
    # The bound that makes this affordable on the hot path (a real transport op is
    # a `fulcra-api` subprocess + HTTPS round trip, ~0.8s): the roles/ LISTING
    # settles "is this assignee a role" for every candidate at once, so the
    # literal-agent-id majority costs zero reads. Only genuine roles pay.
    t = ListCountingTransport()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")
    _put_directive(t, "role-do-1", "Review", owner="alice", assignee="reviewer")
    for who in ("carol", "dave", "erin", "frank"):
        _put_directive(t, f"for-{who}", "Theirs", owner="alice", assignee=who)
    _reconcile(t)
    t.lists.clear(); t.reads.clear()
    _briefing(t, "bob", capsys)
    assert not [p for p in t.reads if p.endswith(("/carol.md", "/dave.md",
                                                  "/erin.md", "/frank.md"))]
    assert t.lists.count(f"team/{TEAM}/roles/") == 1  # ONE listing answers them all


def test_role_resolution_is_one_pass_for_both_folds(capsys):
    # inbox and needs_me share ONE resolution: the role doc is read once, the
    # lease listing happens once — not once per fold.
    t = ListCountingTransport()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")
    _put_directive(t, "role-do-1", "Review", owner="alice", assignee="reviewer")
    _put_directive(t, "role-do-2", "Review again", owner="alice", assignee="reviewer")
    _reconcile(t)
    t.lists.clear(); t.reads.clear()
    _briefing(t, "bob", capsys)
    assert t.reads.count(cli._role_doc_path(TEAM, "reviewer")) == 1
    assert t.lists.count(cli._leases_prefix(TEAM, "reviewer")) == 1


# --- the pure fold --------------------------------------------------------

def test_query_needs_me_expands_held_roles():
    from coord_engine import query

    rows = [{"name": "x", "status": "active", "assignee": "reviewer", "priority": "P2"}]
    assert query.needs_me(rows, "bob", now=NOW) == []
    assert [r["name"] for r in query.needs_me(rows, "bob", now=NOW,
                                              held_roles={"reviewer"})] == ["x"]
    # a role the agent does NOT hold stays out
    assert query.needs_me(rows, "bob", now=NOW, held_roles={"oncall"}) == []
