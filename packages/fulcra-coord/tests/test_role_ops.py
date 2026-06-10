"""Role registry/lease I/O (role_ops.py) + the connect-time lease claim.

Same fixture idiom as test_loop_ops.py: every test runs against the per-test
fake Fulcra backend (coord_backend), and every role_ops surface is best-effort
never-raise — the assertions below check durable bus state, not return paths
alone.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import remote, role_ops, schema


# ---------------------------------------------------------------------------
# Registry CRUD
# ---------------------------------------------------------------------------


def test_registry_crud_round_trip(coord_backend):
    r = schema.make_role("reviewer", "reviews artifacts",
                         standing_instructions="run the runbook",
                         policy="exclusive", sla_hours=24,
                         maintainer="ops:h:r")
    assert role_ops.upsert_role(r, backend=coord_backend) is True
    got = role_ops.read_role("reviewer", backend=coord_backend)
    assert got is not None
    assert got["standing_instructions"] == "run the runbook"
    assert got["policy"] == "exclusive"
    listed = role_ops.list_roles(backend=coord_backend)
    assert [x["name"] for x in listed] == ["reviewer"]


def test_upsert_fails_closed_when_verify_after_write_misses(coord_backend):
    # The registry record is the AUTHORITATIVE write — verify-after-write via
    # remote.stat. If the stat probe can't confirm the record landed, upsert
    # reports failure even though upload claimed success.
    r = schema.make_role("reviewer", "d")
    with mock.patch("fulcra_coord.remote.stat", return_value=None):
        assert role_ops.upsert_role(r, backend=coord_backend) is False


def test_read_role_absent_is_none_and_list_empty(coord_backend):
    assert role_ops.read_role("ghost", backend=coord_backend) is None
    assert role_ops.list_roles(backend=coord_backend) == []


def test_list_roles_ignores_lease_shards(coord_backend):
    # roles/<name>/leases/* shards live UNDER the registry prefix; the listing
    # must apply the same top-level-only filter load_loop_records uses, or a
    # lease would inflate the registry.
    role_ops.upsert_role(schema.make_role("reviewer", "d"),
                         backend=coord_backend)
    role_ops.claim_role("reviewer", "a:h:r", backend=coord_backend)
    listed = role_ops.list_roles(backend=coord_backend)
    assert [x["name"] for x in listed] == ["reviewer"]


# ---------------------------------------------------------------------------
# Leases: per-agent shard, no clobber, self-registration
# ---------------------------------------------------------------------------


def test_claim_writes_per_agent_lease_shard(coord_backend):
    role_ops.upsert_role(schema.make_role("reviewer", "d"),
                         backend=coord_backend)
    assert role_ops.claim_role("reviewer", "a:h:r",
                               backend=coord_backend) is True
    leases = role_ops.read_leases("reviewer", backend=coord_backend)
    assert len(leases) == 1
    assert leases[0]["agent"] == "a:h:r"
    assert leases[0]["at"]   # stamped by the writer


def test_two_agents_claim_without_clobbering(coord_backend):
    role_ops.upsert_role(schema.make_role("reviewer", "d"),
                         backend=coord_backend)
    role_ops.claim_role("reviewer", "a:h:r", backend=coord_backend)
    role_ops.claim_role("reviewer", "b:h:r", backend=coord_backend)
    leases = role_ops.read_leases("reviewer", backend=coord_backend)
    assert {l["agent"] for l in leases} == {"a:h:r", "b:h:r"}


def test_reclaim_overwrites_own_lease_only(coord_backend):
    role_ops.upsert_role(schema.make_role("reviewer", "d"),
                         backend=coord_backend)
    role_ops.claim_role("reviewer", "a:h:r", backend=coord_backend)
    role_ops.claim_role("reviewer", "b:h:r", backend=coord_backend)
    role_ops.claim_role("reviewer", "a:h:r", backend=coord_backend)  # re-claim
    leases = role_ops.read_leases("reviewer", backend=coord_backend)
    # Still exactly two shards: re-claiming refreshed a's OWN file (idempotent)
    # and never touched b's.
    assert sorted(l["agent"] for l in leases) == ["a:h:r", "b:h:r"]


def test_claim_self_registers_an_unregistered_role(coord_backend):
    # Claims must not fail on unregistered roles: connect --role X on a fresh
    # bus self-registers a minimal record (empty instructions) with a warn, so
    # the lease lands and the operator can flesh the registry out later.
    assert role_ops.claim_role("brand-new", "a:h:r",
                               backend=coord_backend) is True
    role = role_ops.read_role("brand-new", backend=coord_backend)
    assert role is not None
    assert role["name"] == "brand-new"
    assert role["standing_instructions"] == ""
    leases = role_ops.read_leases("brand-new", backend=coord_backend)
    assert [l["agent"] for l in leases] == ["a:h:r"]


def test_release_removes_own_lease_only(coord_backend):
    role_ops.upsert_role(schema.make_role("reviewer", "d"),
                         backend=coord_backend)
    role_ops.claim_role("reviewer", "a:h:r", backend=coord_backend)
    role_ops.claim_role("reviewer", "b:h:r", backend=coord_backend)
    assert role_ops.release_role("reviewer", "a:h:r",
                                 backend=coord_backend) is True
    leases = role_ops.read_leases("reviewer", backend=coord_backend)
    assert [l["agent"] for l in leases] == ["b:h:r"]


def test_release_without_a_lease_is_best_effort_false(coord_backend):
    assert role_ops.release_role("reviewer", "a:h:r",
                                 backend=coord_backend) is False


def test_claim_on_exclusive_role_with_other_lease_warns(coord_backend, capsys):
    role_ops.upsert_role(schema.make_role("deployer", "d", policy="exclusive"),
                         backend=coord_backend)
    role_ops.claim_role("deployer", "a:h:r", backend=coord_backend)
    assert role_ops.claim_role("deployer", "b:h:r",
                               backend=coord_backend) is True
    err = capsys.readouterr().err
    assert "exclusive" in err
    # Visible, never silently double-held — but the claim still LANDS (a stale
    # holder is claimable; freshness is judged at read time by role_status).
    leases = role_ops.read_leases("deployer", backend=coord_backend)
    assert {l["agent"] for l in leases} == {"a:h:r", "b:h:r"}


# ---------------------------------------------------------------------------
# connect --role X also claims the role (additive: capabilities unchanged)
# ---------------------------------------------------------------------------


def test_connect_with_role_writes_a_lease(coord_backend):
    from fulcra_coord import presence
    args = SimpleNamespace(agent="a:h:r", workstream=None, summary="",
                           role=["reviewer"], can_review=False, format="table")
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "a:h:r"
    try:
        assert presence.cmd_connect(args, backend=coord_backend) == 0
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    # Capabilities behavior unchanged (the presence record carries the role)...
    rec = remote.download_json(
        remote.presence_remote_path("a-h-r"), backend=coord_backend)
    assert rec and rec["capabilities"] == ["reviewer"]
    # ...AND the lease layer rides on top: connect claimed the role.
    leases = role_ops.read_leases("reviewer", backend=coord_backend)
    assert [l["agent"] for l in leases] == ["a:h:r"]


def test_connect_without_roles_writes_no_lease(coord_backend):
    from fulcra_coord import presence
    args = SimpleNamespace(agent="a:h:r", workstream=None, summary="",
                           role=None, can_review=False, format="table")
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "a:h:r"
    try:
        assert presence.cmd_connect(args, backend=coord_backend) == 0
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert role_ops.list_roles(backend=coord_backend) == []


def test_connect_lease_claim_failure_never_fails_the_session_boot(coord_backend):
    from fulcra_coord import presence
    args = SimpleNamespace(agent="a:h:r", workstream=None, summary="",
                           role=["reviewer"], can_review=False, format="table")
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "a:h:r"
    try:
        with mock.patch("fulcra_coord.role_ops.claim_role",
                        side_effect=RuntimeError("bus down")):
            assert presence.cmd_connect(args, backend=coord_backend) == 0
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev


# ---------------------------------------------------------------------------
# CLI: `fulcra-coord roles` (list/set/claim/release)
# ---------------------------------------------------------------------------


def _roles_args(action=None, name=None, **over):
    base = dict(roles_action=action, format="table", agent=None,
                description=None, instructions=None, policy=None,
                sla_hours=None, maintainer=None)
    if name is not None:
        base["name"] = name
    base.update(over)
    return SimpleNamespace(**base)


def test_cmd_roles_set_creates_and_list_renders(coord_backend, capsys):
    from fulcra_coord import cli
    rc = cli.cmd_roles(_roles_args(
        "set", "reviewer", description="reviews artifacts",
        instructions="run the runbook", policy="exclusive",
        sla_hours=24, maintainer="ops:h:r"), backend=coord_backend)
    assert rc == 0
    role = role_ops.read_role("reviewer", backend=coord_backend)
    assert role["policy"] == "exclusive"
    assert role["standing_instructions"] == "run the runbook"
    assert role["sla_hours"] == 24
    capsys.readouterr()

    rc = cli.cmd_roles(_roles_args(), backend=coord_backend)
    assert rc == 0
    out = capsys.readouterr().out
    assert "reviewer" in out
    assert "VACANT" in out          # nobody has claimed it yet
    assert "ops:h:r" in out         # the maintainer edge is visible


def test_cmd_roles_set_update_preserves_unspecified_fields(coord_backend):
    from fulcra_coord import cli
    cli.cmd_roles(_roles_args("set", "reviewer", description="d",
                              policy="exclusive"), backend=coord_backend)
    created = role_ops.read_role("reviewer", backend=coord_backend)["created_at"]
    rc = cli.cmd_roles(_roles_args("set", "reviewer", sla_hours=24),
                       backend=coord_backend)
    assert rc == 0
    role = role_ops.read_role("reviewer", backend=coord_backend)
    assert role["sla_hours"] == 24
    assert role["policy"] == "exclusive"       # untouched by the update
    assert role["description"] == "d"          # untouched by the update
    assert role["created_at"] == created       # creation stamp survives upsert


def test_cmd_roles_set_rejects_bad_policy(coord_backend):
    from fulcra_coord import cli
    rc = cli.cmd_roles(_roles_args("set", "reviewer", policy="solo"),
                       backend=coord_backend)
    assert rc == 1
    assert role_ops.read_role("reviewer", backend=coord_backend) is None


def test_cmd_roles_claim_and_release(coord_backend):
    from fulcra_coord import cli
    cli.cmd_roles(_roles_args("set", "reviewer", description="d"),
                  backend=coord_backend)
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        rc = cli.cmd_roles(_roles_args("claim", "reviewer"),
                           backend=coord_backend)
        assert rc == 0
        leases = role_ops.read_leases("reviewer", backend=coord_backend)
        assert [l["agent"] for l in leases] == ["me:h:r"]
        rc = cli.cmd_roles(_roles_args("release", "reviewer"),
                           backend=coord_backend)
        assert rc == 0
        assert role_ops.read_leases("reviewer", backend=coord_backend) == []
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev


def test_cmd_roles_json_list_includes_status(coord_backend, capsys):
    import json as _json
    from fulcra_coord import cli
    cli.cmd_roles(_roles_args("set", "reviewer", description="d"),
                  backend=coord_backend)
    capsys.readouterr()
    rc = cli.cmd_roles(_roles_args(format="json"), backend=coord_backend)
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    by_name = {r["name"]: r for r in payload["roles"]}
    assert by_name["reviewer"]["vacant"] is True
    assert by_name["reviewer"]["holders"] == []
    assert by_name["reviewer"]["standing_instructions"] == ""


def test_roles_is_wired_into_map():
    from fulcra_coord import cli, entry
    assert entry.COMMAND_MAP["roles"] is cli.cmd_roles
    p = entry.build_parser()
    args = p.parse_args(["roles"])
    assert args.roles_action is None
    args = p.parse_args(["roles", "set", "reviewer", "--policy", "exclusive",
                         "--sla-hours", "24", "--maintainer", "ops:h:r",
                         "--description", "d", "--instructions", "i"])
    assert args.roles_action == "set"
    assert args.name == "reviewer"
    assert args.sla_hours == 24
    args = p.parse_args(["roles", "claim", "reviewer"])
    assert args.roles_action == "claim"
    args = p.parse_args(["roles", "release", "reviewer"])
    assert args.roles_action == "release"
