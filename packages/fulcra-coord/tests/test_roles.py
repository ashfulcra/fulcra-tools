"""Pure tests for roles-as-durable-identity (spec 2026-06-10): the role
registry record (schema.make_role/validate_role) and the lease/vacancy folds
(roles.py).

No I/O anywhere in this file: roles.py imports ONLY stdlib (pinned by the
fitness test below — stricter than loops.py, which may import schema). The
presence-liveness thresholds arrive BY PARAMETER (stale_hours/grace_seconds),
never by import, so the fold stays injectable and machine-agnostic.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from fulcra_coord import roles, schema

NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)
STALE_HOURS = 2.0   # mirrors views.STALE_HOURS_DEFAULT — passed BY PARAMETER


def _z(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _presence(agent: str, *, hours_ago: float = 0.0) -> dict:
    return {"agent": agent, "last_seen": _z(NOW - timedelta(hours=hours_ago))}


def _lease(agent: str, *, hours_ago: float = 0.0) -> dict:
    return {"agent": agent, "at": _z(NOW - timedelta(hours=hours_ago))}


# ---------------------------------------------------------------------------
# Registry record: schema.make_role / validate_role
# ---------------------------------------------------------------------------


class TestRoleRecord:
    def test_make_role_round_trips_validate(self):
        r = schema.make_role(
            "reviewer", "reviews artifacts for the fleet",
            standing_instructions="run the review runbook; verdicts via review-done",
            policy="exclusive", sla_hours=24, maintainer="ops:h:r",
        )
        assert r["schema"] == schema.ROLE_SCHEMA
        assert r["name"] == "reviewer"
        assert r["policy"] == "exclusive"
        assert r["sla_hours"] == 24
        assert r["maintainer"] == "ops:h:r"
        # checkpoint_ref is RESERVED for continuity phase 2 — present, None.
        assert r["checkpoint_ref"] is None
        # holders is a projection (not authoritative) — starts empty.
        assert r["holders"] == []
        assert schema.validate_role(r) == []

    def test_make_role_defaults_are_minimal_and_valid(self):
        r = schema.make_role("groomer", "")
        assert r["policy"] == "shared"
        assert r["sla_hours"] is None
        assert r["maintainer"] is None
        assert r["standing_instructions"] == ""
        assert schema.validate_role(r) == []

    def test_make_role_rejects_bad_policy(self):
        with pytest.raises(ValueError):
            schema.make_role("x", "d", policy="solo")

    def test_make_role_rejects_empty_name(self):
        with pytest.raises(ValueError):
            schema.make_role("   ", "d")

    def test_validate_role_flags_problems(self):
        r = schema.make_role("reviewer", "d")
        r["schema"] = "fulcra.coordination.task.v1"
        r["policy"] = "solo"
        del r["created_at"]
        problems = "\n".join(schema.validate_role(r))
        assert "schema" in problems
        assert "policy" in problems or "solo" in problems
        assert "created_at" in problems

    def test_validate_role_returns_all_problems_not_just_first(self):
        problems = schema.validate_role({})
        assert len(problems) > 1


# ---------------------------------------------------------------------------
# Lease freshness: a lease is fresh iff its HOLDER'S PRESENCE is fresh — no
# new heartbeat machinery; the existing presence heartbeat keeps leases alive.
# ---------------------------------------------------------------------------


class TestLeaseFresh:
    def test_fresh_presence_means_fresh_lease(self):
        assert roles.lease_fresh(
            _lease("a:h:r", hours_ago=30), _presence("a:h:r", hours_ago=0.1),
            NOW, stale_hours=STALE_HOURS) is True

    def test_stale_presence_lapses_the_lease(self):
        # Session died 3h ago (past the 2h staleness threshold): the lease
        # lapses even though the lease shard itself still exists on the bus.
        assert roles.lease_fresh(
            _lease("a:h:r", hours_ago=0.1), _presence("a:h:r", hours_ago=3),
            NOW, stale_hours=STALE_HOURS) is False

    def test_missing_presence_record_is_stale(self):
        assert roles.lease_fresh(
            _lease("a:h:r"), None, NOW, stale_hours=STALE_HOURS) is False

    def test_unparseable_last_seen_is_stale(self):
        bad = {"agent": "a:h:r", "last_seen": "not-a-time"}
        assert roles.lease_fresh(
            _lease("a:h:r"), bad, NOW, stale_hours=STALE_HOURS) is False

    def test_grace_seconds_tolerates_a_missed_heartbeat(self):
        # 2h10m old presence is past the 2h threshold but inside a 20-minute
        # grace — the same wall-clock grace routing applies so one missed
        # heartbeat / a laptop sleep-wake never flaps a role to VACANT.
        p = _presence("a:h:r", hours_ago=2 + 10 / 60)
        assert roles.lease_fresh(_lease("a:h:r"), p, NOW,
                                 stale_hours=STALE_HOURS) is False
        assert roles.lease_fresh(_lease("a:h:r"), p, NOW,
                                 stale_hours=STALE_HOURS,
                                 grace_seconds=1200.0) is True


# ---------------------------------------------------------------------------
# role_status: holders / vacant / vacant_since / contested
# ---------------------------------------------------------------------------


def _role(name="reviewer", *, policy="shared", sla_hours=None,
          maintainer=None, created_hours_ago=100.0):
    r = schema.make_role(name, f"the {name} role", policy=policy,
                         sla_hours=sla_hours, maintainer=maintainer)
    r["created_at"] = _z(NOW - timedelta(hours=created_hours_ago))
    return r


class TestRoleStatus:
    def test_fresh_lease_holder_is_held(self):
        st = roles.role_status(
            _role(), [_lease("a:h:r", hours_ago=5)],
            {"a:h:r": _presence("a:h:r")}, NOW, stale_hours=STALE_HOURS)
        assert [h["agent"] for h in st["holders"]] == ["a:h:r"]
        assert st["vacant"] is False
        assert st["vacant_since"] is None
        assert st["contested"] is False

    def test_all_leases_stale_reads_vacant_since_latest_lease(self):
        newest = _lease("a:h:r", hours_ago=30)
        st = roles.role_status(
            _role(), [_lease("b:h:r", hours_ago=50), newest],
            {}, NOW, stale_hours=STALE_HOURS)   # nobody's presence is fresh
        assert st["vacant"] is True
        assert st["holders"] == []
        # vacant since the LAST time anyone held it — the newest lease stamp.
        assert st["vacant_since"] == newest["at"]

    def test_never_claimed_role_is_vacant_since_creation(self):
        role = _role(created_hours_ago=72)
        st = roles.role_status(role, [], {}, NOW, stale_hours=STALE_HOURS)
        assert st["vacant"] is True
        assert st["vacant_since"] == role["created_at"]

    def test_exclusive_with_two_fresh_leases_is_contested(self):
        st = roles.role_status(
            _role(policy="exclusive"),
            [_lease("a:h:r"), _lease("b:h:r")],
            {"a:h:r": _presence("a:h:r"), "b:h:r": _presence("b:h:r")},
            NOW, stale_hours=STALE_HOURS)
        assert st["contested"] is True
        assert st["vacant"] is False
        assert {h["agent"] for h in st["holders"]} == {"a:h:r", "b:h:r"}

    def test_shared_with_two_fresh_leases_is_not_contested(self):
        st = roles.role_status(
            _role(policy="shared"),
            [_lease("a:h:r"), _lease("b:h:r")],
            {"a:h:r": _presence("a:h:r"), "b:h:r": _presence("b:h:r")},
            NOW, stale_hours=STALE_HOURS)
        assert st["contested"] is False

    def test_exclusive_with_one_fresh_one_stale_is_not_contested(self):
        # A stale lease is claimable, not a contest: only FRESH double-holding
        # of an exclusive role is the visible-never-silent conflict.
        st = roles.role_status(
            _role(policy="exclusive"),
            [_lease("a:h:r"), _lease("b:h:r")],
            {"a:h:r": _presence("a:h:r"),
             "b:h:r": _presence("b:h:r", hours_ago=9)},
            NOW, stale_hours=STALE_HOURS)
        assert st["contested"] is False
        assert [h["agent"] for h in st["holders"]] == ["a:h:r"]

    def test_duplicate_leases_for_one_agent_collapse_to_latest(self):
        # Per-agent lease files make this impossible on a healthy bus, but the
        # PURE fold must not assume — two shards for one agent are one holder.
        st = roles.role_status(
            _role(), [_lease("a:h:r", hours_ago=10), _lease("a:h:r", hours_ago=1)],
            {"a:h:r": _presence("a:h:r")}, NOW, stale_hours=STALE_HOURS)
        assert len(st["holders"]) == 1
        assert st["holders"][0]["since"] == _z(NOW - timedelta(hours=1))

    def test_malformed_lease_records_never_break_the_fold(self):
        st = roles.role_status(
            _role(), [{}, {"agent": ""}, {"at": "x"}, _lease("a:h:r")],
            {"a:h:r": _presence("a:h:r")}, NOW, stale_hours=STALE_HOURS)
        assert [h["agent"] for h in st["holders"]] == ["a:h:r"]


# ---------------------------------------------------------------------------
# vacancy_escalation_due: vacant past sla_hours -> escalate to the maintainer
# ---------------------------------------------------------------------------


class TestVacancyEscalation:
    def _status(self, *, vacant=True, hours_vacant=30.0):
        return {"holders": [], "vacant": vacant, "contested": False,
                "vacant_since": _z(NOW - timedelta(hours=hours_vacant))
                if vacant else None}

    def test_vacant_past_sla_is_due(self):
        role = _role(sla_hours=24, maintainer="ops:h:r")
        assert roles.vacancy_escalation_due(
            role, self._status(hours_vacant=30), NOW) is True

    def test_vacant_within_sla_is_not_due(self):
        role = _role(sla_hours=24)
        assert roles.vacancy_escalation_due(
            role, self._status(hours_vacant=3), NOW) is False

    def test_no_sla_means_never_due(self):
        role = _role(sla_hours=None)
        assert roles.vacancy_escalation_due(
            role, self._status(hours_vacant=9999), NOW) is False

    def test_held_role_is_never_due(self):
        role = _role(sla_hours=24)
        assert roles.vacancy_escalation_due(
            role, self._status(vacant=False), NOW) is False

    def test_malformed_sla_or_since_is_not_due(self):
        # Mirror loops._is_overdue: garbage off the bus fails toward NOT
        # escalating (an escalation writes a directive — never spam on noise).
        role = _role(sla_hours=24)
        bad_since = {"holders": [], "vacant": True, "contested": False,
                     "vacant_since": "not-a-time"}
        assert roles.vacancy_escalation_due(role, bad_since, NOW) is False
        role_bad = _role()
        role_bad["sla_hours"] = "soon"
        assert roles.vacancy_escalation_due(
            role_bad, self._status(hours_vacant=9999), NOW) is False


# ---------------------------------------------------------------------------
# Fitness pins (layering) — same AST-scan idiom as TestLoopsLayering in
# test_fulcra_coord.py, duplicated locally like test_directive_dualwrite does.
# ---------------------------------------------------------------------------


def _first_party_imports(module_filename: str) -> set:
    import ast
    pkg = Path(__file__).resolve().parents[1] / "fulcra_coord"
    src = (pkg / module_filename).read_text(encoding="utf-8")
    imported: set = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            if (node.level or 0) >= 1:
                if node.module:
                    imported.add(node.module.split(".")[0])
                else:
                    for a in node.names:
                        imported.add(a.name.split(".")[0])
            elif (node.module or "").split(".")[0] == "fulcra_coord":
                parts = node.module.split(".")
                if len(parts) >= 2:
                    imported.add(parts[1])
                else:
                    for a in node.names:
                        imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] == "fulcra_coord" and len(parts) >= 2:
                    imported.add(parts[1])
    return imported


class TestRolesLayering:
    def test_roles_is_pure_stdlib_only(self):
        # roles.py is the PURE fold layer: STDLIB ONLY — stricter than
        # loops.py (which may import schema). Liveness thresholds arrive by
        # PARAMETER, never by importing views; any first-party import here
        # would let I/O or policy leak into the injectable fold.
        offenders = _first_party_imports("roles.py")
        assert offenders == set(), (
            f"roles.py must be stdlib-only; imports: {offenders}")

    def test_role_ops_imports_no_up_layer_module(self):
        # role_ops.py is the thin I/O layer over roles.py (the lease/registry
        # writer): it may reach DOWN (schema/remote/roles/log/output), but an
        # import of cli/views/lifecycle/inbox/writepipe/routing_ops/listener/
        # query/presence would couple the lease path to command/rendering
        # layers — the same creep the loop_ops.py pin forbids.
        forbidden = {"cli", "views", "lifecycle", "inbox", "writepipe",
                     "routing_ops", "listener", "query", "presence"}
        offenders = _first_party_imports("role_ops.py") & forbidden
        assert offenders == set(), (
            f"role_ops.py imports up-layer modules: {offenders}")


# ---------------------------------------------------------------------------
# Generalization rule (non-negotiable, spec 2026-06-10): core ships the
# MECHANISM only — zero role names, zero fleet ids. Our fleet's roles are
# registry records we write at runtime; any adopter writes theirs.
# ---------------------------------------------------------------------------


_FLEET_ID_PATTERN = re.compile(r"ArcBot|Mac\.localdomain|coord-maintainer",
                               re.IGNORECASE)


class TestGeneralization:
    def test_no_fleet_ids_in_role_core(self):
        pkg = Path(__file__).resolve().parents[1] / "fulcra_coord"
        for module in ("roles.py", "role_ops.py", "schema.py"):
            src = (pkg / module).read_text(encoding="utf-8")
            hits = _FLEET_ID_PATTERN.findall(src)
            assert not hits, (
                f"{module} contains fleet identifiers {hits} — role core must "
                "ship mechanism only; fleet roles are adopter DATA")
