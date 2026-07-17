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

from coord_engine import budget, cli, okf, reconcile, tasks
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


class RoleDocMalformed(FakeTransport):
    """The role doc READS, but its body is not frontmatter — corrupt or truncated."""

    def read(self, path):
        if path == cli._role_doc_path(TEAM, "reviewer"):
            return "not frontmatter\n"
        return super().read(path)


def test_role_doc_malformed_while_listed_is_degraded_not_a_non_role(capsys):
    # Same rule as the read-None case above, and the reviewer-reproduced hole in
    # the code written to close that class: a body that does not PARSE is not
    # evidence that the name isn't a role. The roles/ listing has already proved
    # `reviewer` IS a registered role, so an unusable doc is UNKNOWN. Treating the
    # failed parse as affirmative "not a role" served the holder a clean, empty
    # queue with no marker at all — a failure that type-checks as success.
    t = _team_with_role_directive(RoleDocMalformed)
    b = _briefing(t, "bob", capsys)
    assert b["role_degraded"] == {"type": "role-degraded", "roles": ["reviewer"]}
    # and the queue must NOT read as a clean "nothing for you"
    assert [r["name"] for r in b["inbox"]] == []
    assert [r["name"] for r in b["needs_me"]] == []


def test_role_doc_malformed_while_listed_is_loud_in_text(capsys):
    t = _team_with_role_directive(RoleDocMalformed)
    assert cli.main(["briefing", TEAM, "-a", "bob"], transport=t) == 0
    out = capsys.readouterr().out
    assert "role resolution degraded: reviewer" in out
    assert "unknown" in out


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


def test_multi_lease_role_costs_a_read_per_shard(capsys):
    # The HONEST bound, pinned. This docstring used to claim `1 + 3R` ops, R =
    # distinct roles — and the claim was false and shipped anyway. Every LISTED
    # lease shard is read, and shards accumulate per claiming agent forever (only
    # `roles release` prunes one), so the real cost is 1 + sum(2 + L_r): one role
    # with ten shards is 13 ops, not the 4 that `1 + 3R` predicts. `3R` is just the
    # L_r == 1 case. If a future change makes the cost formula in
    # `_held_roles_for_rows` false again, this test says so in op counts.
    t = ListCountingTransport()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")
    for i in range(9):  # nine agents who claimed the role and never released
        _put_lease(t, "reviewer", f"ghost{i}")
    _put_directive(t, "role-do-1", "Review", owner="alice", assignee="reviewer")
    _reconcile(t)
    t.lists.clear(); t.reads.clear()
    b = _briefing(t, "bob", capsys)
    assert [r["name"] for r in b["inbox"]] == ["role-do-1"]  # still routes
    role_ops = ([p for p in t.reads if "/roles/" in p]
                + [p for p in t.lists if "/roles/" in p])
    # 1 roles/ listing + 1 doc read + 1 leases listing + 10 shard reads
    assert len(role_ops) == 13, sorted(role_ops)
    assert len([p for p in t.reads if "/leases/" in p]) == 10


# --- the wall-clock bound: a budget cut is UNKNOWN, not "no roles" --------

class RoleClockTransport(FakeTransport):
    """A degraded transport, deterministically: every op under `roles/` advances a
    fake monotonic clock by `cost` seconds instead of sleeping. No wall clock, no
    flake, and only the role fold's ops spend time — so a cut here is unambiguously
    the role budget's, not another section's."""

    def __init__(self):
        super().__init__()
        self.clock = 0.0
        self.cost = 0.0

    def _tick(self, path):
        if "/roles/" in path:
            self.clock += self.cost

    def read(self, path):
        self._tick(path)
        return super().read(path)

    def list_dir(self, prefix):
        self._tick(prefix)
        return super().list_dir(prefix)


def _slow_role_team(monkeypatch, budget_seconds, *, cost=1.0):
    t = _team_with_role_directive(RoleClockTransport)
    t.cost = cost  # setup above ran free; the fold pays
    monkeypatch.setattr(budget.time, "monotonic", lambda: t.clock)
    monkeypatch.setenv("COORD_ROLE_FOLD_BUDGET", str(budget_seconds))
    return t


def test_role_budget_cut_before_any_role_marks_candidates_unresolved(capsys, monkeypatch):
    # The roles/ listing alone spends the budget. `reviewer` is then never scanned
    # — and an unscanned candidate is UNKNOWN. Rendering the empty held-set we
    # happen to have would serve bob a role-blind queue because the clock ran out:
    # the same silent failure as before, triggered by latency instead of a missing
    # fold. Bob DOES hold this role and role-do-1 IS his work.
    t = _slow_role_team(monkeypatch, 0.5)
    b = _briefing(t, "bob", capsys)
    assert b["role_degraded"] == {"type": "role-degraded", "roles": ["reviewer"]}
    assert [r["name"] for r in b["inbox"]] == []
    assert not [r for r in b["needs_me"] if r.get("name") == "role-do-1"]


def test_role_budget_cut_mid_role_marks_it_unresolved(capsys, monkeypatch):
    # The partial-scan half: the budget survives the roles/ listing and the doc
    # read, then the LEASE listing spends it with shards still unread. A lease we
    # never read is UNKNOWN exactly as if its read had failed — the role must not
    # fold out as "bob isn't a holder".
    t = _slow_role_team(monkeypatch, 2.5)  # listing + doc read fit; shard reads do not
    b = _briefing(t, "bob", capsys)
    assert b["role_degraded"] == {"type": "role-degraded", "roles": ["reviewer"]}
    assert [r["name"] for r in b["inbox"]] == []
    assert not [r for r in b["needs_me"] if r.get("name") == "role-do-1"]


def test_role_budget_cut_is_loud_on_inbox_and_needs_me(capsys, monkeypatch):
    # The cut must reach the verbs agents actually run, in text — not just the
    # briefing bundle's json.
    t = _slow_role_team(monkeypatch, 0.5)
    assert cli.main(["inbox", TEAM, "-a", "bob"], transport=t) == 0
    assert "role resolution degraded: reviewer" in capsys.readouterr().out
    assert cli.main(["needs-me", TEAM, "--agent", "bob"], transport=t) == 0
    assert "role resolution degraded: reviewer" in capsys.readouterr().out


def test_role_fold_completing_late_is_not_degraded(capsys, monkeypatch):
    # The other side of the rule: a COMPLETED fold is definitive knowledge, so
    # finishing late must not degrade it. Only an UNREAD op is unknown. Without
    # this, a tight budget would degrade every fold whose last read happened to
    # land on the boundary — and a marker that fires on healthy folds is a marker
    # agents learn to ignore.
    t = _slow_role_team(monkeypatch, 4.5)  # listing + doc + lease listing + shard = 4
    b = _briefing(t, "bob", capsys)
    assert "role_degraded" not in b
    assert [r["name"] for r in b["inbox"]] == ["role-do-1"]


# --- the pure fold --------------------------------------------------------

def test_query_needs_me_expands_held_roles():
    from coord_engine import query

    rows = [{"name": "x", "status": "active", "assignee": "reviewer", "priority": "P2"}]
    assert query.needs_me(rows, "bob", now=NOW) == []
    assert [r["name"] for r in query.needs_me(rows, "bob", now=NOW,
                                              held_roles={"reviewer"})] == ["x"]
    # a role the agent does NOT hold stays out
    assert query.needs_me(rows, "bob", now=NOW, held_roles={"oncall"}) == []
