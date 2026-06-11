"""Roles + presence: transport READ FAILURE must never read as vacancy/absence.

The 2026-06-11 blind adversarial audit's second wave (F4/F5/F8) — the same
absence-vs-read-failure class PR #170 fixed on the task write path, now in the
roles and presence layers:

  * F4 ``role_ops.read_leases``: returned [] on ANY failure, so one failed
    lease listing made a HELD role fold as VACANT with ``vacant_since`` =
    the role's (old) ``created_at`` — and ``cli._maybe_escalate_role_vacancy``
    then wrote a FALSE "Role VACANT past SLA" P1 directive onto a human's
    plate (the daily marker caps it at one/day, but it is a durable false
    alarm).
  * F5 ``presence._reconcile_presence`` / ``_upsert_presence_aggregate``:
    ``remote.list_json`` silently DROPS per-agent records whose individual
    download fails, and the rebuild uploaded the SURVIVORS as the
    authoritative aggregate — a live reviewer whose one record 504'd vanished
    from presence, and the truncated roster threaded into the review-route
    sweep + role health that tick ("no reviewer live" escalations while the
    reviewer was up — lived incident).
  * F8 ``presence.cmd_connect`` / ``_rewrite_own_capabilities`` /
    ``cmd_workstream``: a failed read of the agent's OWN presence record was
    treated as "never connected", and the subsequent whole-record write wiped
    capabilities/workstreams/summary/session.

House idiom (bug hunt C1 + PR #170): a failed read is disambiguated by a stat
probe and ``remote.probe_reachable`` BEFORE anyone acts on "absent"; the probe
is spent only on failure paths; callers act on a READ_ERROR sentinel, never on
a guess.
"""

from __future__ import annotations

import json
import os
import types
from datetime import datetime, timezone
from unittest import mock

from fulcra_coord import (
    cli, inbox, presence, query, remote, role_ops, roles as roles_fold, schema,
    views,
)


# ---------------------------------------------------------------------------
# helpers (the test_write_path_read_errors.py idiom)
# ---------------------------------------------------------------------------

def _store_file(tmp_path, remote_path: str):
    """The fake store's local file for a remote path (FULCRA_FAKE_ROOT layout)."""
    return tmp_path / remote_path.lstrip("/")


def _read_store_json(tmp_path, remote_path: str):
    return json.loads(_store_file(tmp_path, remote_path).read_text())


def _patch_download_none_for(monkeypatch, predicate):
    """remote.download_json -> None for paths matching ``predicate``; real
    otherwise. Models a targeted transport read failure (504 weather)."""
    real = remote.download_json

    def fake(path, **kw):
        if predicate(path):
            return None
        return real(path, **kw)

    monkeypatch.setattr(remote, "download_json", fake)


def _seed_presence(coord_backend, agent, **over):
    rec = schema.make_presence(
        agent,
        workstreams=over.pop("workstreams", ["ws"]),
        summary=over.pop("summary", ""),
        session=over.pop("session", None),
        capabilities=over.pop("capabilities", None),
    )
    rec.update(over)
    path = remote.presence_remote_path(views.agent_slug(agent))
    assert remote.upload_json(rec, path, backend=coord_backend)
    return rec, path


def _as_me(agent="a:h:r"):
    """Context manager pinning the resolved identity (the test_role_ops idiom)."""
    return mock.patch.dict(os.environ, {"FULCRA_COORD_AGENT": agent})


def _connect_args(**over):
    base = dict(agent=None, workstream=None, summary="", role=None,
                can_review=False, format="table")
    base.update(over)
    return types.SimpleNamespace(**base)


# ===========================================================================
# F4 — read_leases: a failed listing is READ_ERROR, never "no leases"
# ===========================================================================

def test_f4_read_leases_listing_failure_is_read_error_not_empty(coord_backend,
                                                                monkeypatch):
    """Three failure shapes, one verdict: a lease state that could not be READ
    must come back as the READ_ERROR sentinel, never as the [] that folds to
    VACANT downstream."""
    role_ops.upsert_role(schema.make_role("reviewer", "d"),
                         backend=coord_backend)
    assert role_ops.claim_role("reviewer", "a:h:r",
                               backend=coord_backend) is True
    # Healthy read sees the lease.
    assert [l["agent"] for l in
            role_ops.read_leases("reviewer", backend=coord_backend)] == ["a:h:r"]

    # (a) shards LISTED but their downloads fail: definitely a read error.
    _patch_download_none_for(monkeypatch, lambda p: "/leases/" in p)
    assert role_ops.read_leases(
        "reviewer", backend=coord_backend) is role_ops.READ_ERROR

    # (b) the listing itself raises.
    monkeypatch.setattr(remote, "list_files",
                        mock.Mock(side_effect=RuntimeError("bus down")))
    assert role_ops.read_leases(
        "reviewer", backend=coord_backend) is role_ops.READ_ERROR

    # (c) an EMPTY listing while the bus is unreachable: list_files swallows
    # transport failures into [], so emptiness is only trustworthy when the
    # reachability probe confirms the bus answered.
    monkeypatch.setattr(remote, "list_files", lambda *a, **kw: [])
    monkeypatch.setattr(remote, "probe_reachable", lambda backend=None: False)
    assert role_ops.read_leases(
        "reviewer", backend=coord_backend) is role_ops.READ_ERROR


def test_f4_read_leases_confirmed_empty_is_still_empty(coord_backend):
    """Counter-case: a reachable bus with genuinely no lease shards keeps the
    [] contract — a never-claimed role really is vacant."""
    role_ops.upsert_role(schema.make_role("reviewer", "d"),
                         backend=coord_backend)
    assert role_ops.read_leases("reviewer", backend=coord_backend) == []


def test_f4_role_status_propagates_unknown_not_vacant():
    """The pure fold: unknowable inputs (leases=None from a READ_ERROR caller,
    or an unknowable presence roster) yield an explicit unknown outcome —
    NOT vacant, so no SLA clock starts on a transport blip."""
    now = datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)
    role = {"name": "reviewer", "policy": "exclusive", "sla_hours": 1,
            "created_at": "2020-01-01T00:00:00.000000Z"}
    for st in (
        roles_fold.role_status(role, None, {}, now, stale_hours=4),
        roles_fold.role_status(role, [{"agent": "a", "at": "x"}], None, now,
                               stale_hours=4),
    ):
        assert st["unknown"] is True
        assert st["vacant"] is False
        assert st["vacant_since"] is None
        assert st["holders"] == []
        assert st["contested"] is False
        # The escalation predicate must never fire on unknown.
        assert roles_fold.vacancy_escalation_due(role, st, now) is False
    # And a normal fold carries the explicit negative.
    st = roles_fold.role_status(role, [], {}, now, stale_hours=4)
    assert st["unknown"] is False
    assert st["vacant"] is True


def test_f4_lease_read_failure_produces_no_false_vacancy_escalation(
        coord_backend, monkeypatch, tmp_path):
    """THE F4 FAILURE SEQUENCE: a HELD role (lease shard on the bus) whose
    lease listing fails one tick. The old fold read [] -> vacant with
    vacant_since = created_at (years old) -> past SLA -> a FALSE 'Role VACANT
    past SLA' P1 directive landed on the maintainer's plate. The health check
    must report the role UNKNOWN and SKIP the escalation."""
    role = schema.make_role("reviewer", "d", sla_hours=1,
                            maintainer="ops:h:r")
    role["created_at"] = "2020-01-01T00:00:00.000000Z"
    assert role_ops.upsert_role(role, backend=coord_backend) is True
    assert role_ops.claim_role("reviewer", "a:h:r",
                               backend=coord_backend) is True

    _patch_download_none_for(monkeypatch, lambda p: "/leases/" in p)

    out = cli._role_health_check(backend=coord_backend)
    row = {r["name"]: r for r in out["roles"]}["reviewer"]
    assert row["unknown"] is True, \
        "an unreadable lease listing must surface as unknown, not vacant"
    assert row["vacant"] is False
    assert row["escalation_due"] is False
    assert out["escalated"] == 0
    assert out["vacant"] == 0

    # No escalation artifacts: neither the daily marker nor the directive task.
    esc_dir = _store_file(
        tmp_path, f"{remote.roles_prefix()}reviewer/escalations/")
    assert not (esc_dir.exists() and any(esc_dir.iterdir())), \
        "a false-vacancy escalation marker was claimed on a failed read"
    tasks_dir = _store_file(tmp_path, remote.task_remote_path("x")).parent
    assert not (tasks_dir.exists() and any(tasks_dir.iterdir())), \
        "a false 'Role VACANT past SLA' directive was written to a human"


def test_f4_cmd_roles_table_says_unreadable_not_vacant(coord_backend,
                                                       monkeypatch, capsys):
    role_ops.upsert_role(schema.make_role("reviewer", "d"),
                         backend=coord_backend)
    role_ops.claim_role("reviewer", "a:h:r", backend=coord_backend)
    capsys.readouterr()
    _patch_download_none_for(monkeypatch, lambda p: "/leases/" in p)
    args = types.SimpleNamespace(roles_action=None, format="table", agent=None)
    assert cli.cmd_roles(args, backend=coord_backend) == 0
    out = capsys.readouterr().out
    assert "UNREADABLE" in out
    assert "VACANT" not in out, \
        "an unreadable lease listing rendered as VACANT on the roles surface"


def test_f4_board_roles_section_marks_unknown(coord_backend, monkeypatch,
                                              capsys):
    role_ops.upsert_role(schema.make_role("reviewer", "d"),
                         backend=coord_backend)
    role_ops.claim_role("reviewer", "a:h:r", backend=coord_backend)
    capsys.readouterr()
    _patch_download_none_for(monkeypatch, lambda p: "/leases/" in p)
    with _as_me():
        rc = query.cmd_board(types.SimpleNamespace(agent=None, format="json"),
                             backend=coord_backend)
    assert rc == 0
    board = json.loads(capsys.readouterr().out)
    row = {r["name"]: r for r in board["roles"]}["reviewer"]
    assert row["unknown"] is True
    assert row["vacant"] is False


def test_f4_claim_still_lands_when_lease_listing_unreadable(coord_backend,
                                                            monkeypatch):
    """The exclusive-policy contested check inside claim_role reads the lease
    sub-log; a READ_ERROR there must not fail the claim (the per-agent shard
    is clobber-free) — it only skips the advisory warn."""
    role_ops.upsert_role(schema.make_role("deployer", "d", policy="exclusive"),
                         backend=coord_backend)
    role_ops.claim_role("deployer", "a:h:r", backend=coord_backend)
    _patch_download_none_for(monkeypatch, lambda p: "/leases/" in p)
    assert role_ops.claim_role("deployer", "b:h:r",
                               backend=coord_backend) is True


# ===========================================================================
# F5 — presence rebuild: survivors of a partial read are not the roster
# ===========================================================================

def test_f5_list_json_checked_exposes_drops(coord_backend, monkeypatch):
    _seed_presence(coord_backend, "agent-a")
    _, path_b = _seed_presence(coord_backend, "agent-b")

    items, complete = remote.list_json_checked(remote.presence_prefix(),
                                               backend=coord_backend)
    assert complete is True
    assert {rec["agent"] for _, rec in items} == {"agent-a", "agent-b"}

    _patch_download_none_for(monkeypatch, lambda p: p == path_b)
    items, complete = remote.list_json_checked(remote.presence_prefix(),
                                               backend=coord_backend)
    assert complete is False, \
        "a dropped per-item download must be visible to the caller"
    assert {rec["agent"] for _, rec in items} == {"agent-a"}

    monkeypatch.setattr(remote, "list_files",
                        mock.Mock(side_effect=RuntimeError("bus down")))
    assert remote.list_json_checked(remote.presence_prefix(),
                                    backend=coord_backend) == ([], False)


def test_f5_list_json_checked_empty_prefix_is_complete(coord_backend):
    assert remote.list_json_checked(remote.presence_prefix(),
                                    backend=coord_backend) == ([], True)


def test_f5_partial_per_agent_read_keeps_previous_aggregate(coord_backend,
                                                            monkeypatch,
                                                            tmp_path):
    """THE F5 FAILURE SEQUENCE: agent-b's one presence record 504s during the
    reconcile rebuild. The old code uploaded the SURVIVORS as the
    authoritative aggregate — agent-b (live!) vanished from presence. The
    rebuild must skip the upload, keep the previous full aggregate, and hand
    THAT roster to the tick's consumers."""
    rec_a, _ = _seed_presence(coord_backend, "agent-a")
    rec_b, path_b = _seed_presence(coord_backend, "agent-b")
    agg = views.build_presence([rec_a, rec_b])
    view_path = remote.presence_view_path()
    assert remote.upload_json(agg, view_path, backend=coord_backend)
    before = _store_file(tmp_path, view_path).read_text()

    _patch_download_none_for(monkeypatch, lambda p: p == path_b)

    out = presence._reconcile_presence(backend=coord_backend)
    assert _store_file(tmp_path, view_path).read_text() == before, \
        "a partial per-agent read was uploaded as the authoritative roster"
    assert isinstance(out, dict)
    assert {a["agent"] for a in out["agents"]} == {"agent-a", "agent-b"}, \
        "the tick must be handed the previous FULL aggregate, not survivors"


def test_f5_partial_read_and_unreadable_aggregate_is_read_error(coord_backend,
                                                                monkeypatch,
                                                                tmp_path):
    """Boundary case (documented policy: fail toward NO-ACTION): per-agent
    read partial AND the previous aggregate unreadable — nothing trustworthy
    exists, so the rebuild reports PRESENCE_READ_ERROR and uploads nothing."""
    rec_a, _ = _seed_presence(coord_backend, "agent-a")
    rec_b, path_b = _seed_presence(coord_backend, "agent-b")
    view_path = remote.presence_view_path()
    assert remote.upload_json(views.build_presence([rec_a, rec_b]), view_path,
                              backend=coord_backend)
    before = _store_file(tmp_path, view_path).read_text()

    _patch_download_none_for(monkeypatch,
                             lambda p: p in (path_b, view_path))

    out = presence._reconcile_presence(backend=coord_backend)
    assert out is presence.PRESENCE_READ_ERROR
    assert _store_file(tmp_path, view_path).read_text() == before


def test_f5_reconcile_tick_skips_review_sweep_on_unknown_presence(
        coord_backend, monkeypatch, tmp_path):
    """When presence is unknowable this tick (partial per-agent read + the
    previous aggregate unreadable), the review-route sweep must make NO
    rerouting decision — rerouting away from a live reviewer is the durable
    wrong call this finding exists to stop."""
    rec_a, _ = _seed_presence(coord_backend, "agent-a")
    rec_b, path_b = _seed_presence(coord_backend, "agent-b")
    view_path = remote.presence_view_path()
    assert remote.upload_json(views.build_presence([rec_a, rec_b]), view_path,
                              backend=coord_backend)

    _patch_download_none_for(monkeypatch,
                             lambda p: p in (path_b, view_path))

    with mock.patch("fulcra_coord.cli._sweep_review_routes") as sweep:
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0
    assert not sweep.called, \
        "the review sweep ran on a tick whose presence roster is unknown"


def test_f5_reconcile_tick_threads_previous_aggregate_on_partial_read(
        coord_backend, monkeypatch, tmp_path):
    """Partial per-agent read but the previous aggregate IS readable: the
    sweep still runs, but against the previous FULL roster — never the
    survivors."""
    rec_a, _ = _seed_presence(coord_backend, "agent-a")
    rec_b, path_b = _seed_presence(coord_backend, "agent-b")
    view_path = remote.presence_view_path()
    assert remote.upload_json(views.build_presence([rec_a, rec_b]), view_path,
                              backend=coord_backend)

    _patch_download_none_for(monkeypatch, lambda p: p == path_b)

    with mock.patch("fulcra_coord.cli._sweep_review_routes") as sweep:
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0
    assert sweep.called
    roster = sweep.call_args.kwargs["presence"]
    assert {a["agent"] for a in roster} == {"agent-a", "agent-b"}, \
        "the sweep was handed a truncated roster (survivors of a partial read)"


def test_f5_role_health_reports_unknown_when_presence_unknown(coord_backend,
                                                              monkeypatch,
                                                              tmp_path):
    """Same tick, the role-health side: lease freshness IS presence freshness,
    so an unknowable roster makes every role's vacancy judgment unknown — no
    SLA escalation may fire."""
    role = schema.make_role("reviewer", "d", sla_hours=1, maintainer="ops:h:r")
    role["created_at"] = "2020-01-01T00:00:00.000000Z"
    role_ops.upsert_role(role, backend=coord_backend)
    role_ops.claim_role("reviewer", "agent-b", backend=coord_backend)
    out = cli._role_health_check(backend=coord_backend,
                                 presence_agents=presence.PRESENCE_READ_ERROR)
    row = {r["name"]: r for r in out["roles"]}["reviewer"]
    assert row["unknown"] is True
    assert row["vacant"] is False
    assert out["escalated"] == 0
    tasks_dir = _store_file(tmp_path, remote.task_remote_path("x")).parent
    assert not (tasks_dir.exists() and any(tasks_dir.iterdir()))


def test_f5_connect_upsert_partial_listing_does_not_drop_peers(coord_backend,
                                                               monkeypatch,
                                                               tmp_path):
    """The connect-time aggregate upsert has the same hole: with three agents
    on the bus and ONE record 504ing, the old code uploaded self + the one
    survivor — the failed-read peer vanished from the roster. A partial
    enumeration must fall back to the previous aggregate's peers."""
    rec_a, _ = _seed_presence(coord_backend, "agent-a")
    rec_b, path_b = _seed_presence(coord_backend, "agent-b")
    rec_c, _ = _seed_presence(coord_backend, "agent-c")
    view_path = remote.presence_view_path()
    assert remote.upload_json(views.build_presence([rec_a, rec_b, rec_c]),
                              view_path, backend=coord_backend)

    _patch_download_none_for(monkeypatch, lambda p: p == path_b)

    fresh_a = schema.make_presence("agent-a", workstreams=["new"], summary="")
    presence._upsert_presence_aggregate(fresh_a, backend=coord_backend)
    agg = _read_store_json(tmp_path, view_path)
    assert {a["agent"] for a in agg["agents"]} == \
        {"agent-a", "agent-b", "agent-c"}, \
        "the upsert uploaded survivors of a partial read — a live peer vanished"


def test_f5_connect_upsert_partial_listing_and_unreadable_aggregate_no_upload(
        coord_backend, monkeypatch, tmp_path):
    """Boundary case again, on the connect path: enumeration partial AND the
    previous aggregate unreadable — fail toward no-action (the durable
    per-agent record already landed; reconcile heals the view later)."""
    rec_a, _ = _seed_presence(coord_backend, "agent-a")
    rec_b, path_b = _seed_presence(coord_backend, "agent-b")
    rec_c, _ = _seed_presence(coord_backend, "agent-c")
    view_path = remote.presence_view_path()
    assert remote.upload_json(views.build_presence([rec_a, rec_b, rec_c]),
                              view_path, backend=coord_backend)
    before = _store_file(tmp_path, view_path).read_text()

    _patch_download_none_for(monkeypatch,
                             lambda p: p in (path_b, view_path))

    fresh_a = schema.make_presence("agent-a", workstreams=["new"], summary="")
    presence._upsert_presence_aggregate(fresh_a, backend=coord_backend)
    assert _store_file(tmp_path, view_path).read_text() == before, \
        "with no trustworthy source at all the aggregate must not be rewritten"


# ===========================================================================
# F8 — own-presence read failure must not become a wiped record
# ===========================================================================

def test_f8_load_own_presence_distinguishes_error_from_absent(coord_backend,
                                                              monkeypatch):
    # Confirmed absent on a reachable empty bus: None (genuinely-first-connect).
    assert presence._load_own_presence("a:h:r", backend=coord_backend) is None

    # Record exists but its download fails (stat still sees it): READ_ERROR.
    _, path = _seed_presence(coord_backend, "a:h:r")
    _patch_download_none_for(monkeypatch, lambda p: p == path)
    assert presence._load_own_presence(
        "a:h:r", backend=coord_backend) is presence.PRESENCE_READ_ERROR

    # Nothing visible AND the bus unreachable: unconfirmable, READ_ERROR.
    monkeypatch.setattr(remote, "stat", lambda path, **kw: None)
    monkeypatch.setattr(remote, "probe_reachable", lambda backend=None: False)
    assert presence._load_own_presence(
        "a:h:r", backend=coord_backend) is presence.PRESENCE_READ_ERROR

    # A raising transport reads as error too.
    monkeypatch.setattr(remote, "download_json",
                        mock.Mock(side_effect=RuntimeError("bus down")))
    assert presence._load_own_presence(
        "a:h:r", backend=coord_backend) is presence.PRESENCE_READ_ERROR


def test_f8_connect_preserves_remote_record_on_own_read_failure(coord_backend,
                                                                monkeypatch,
                                                                tmp_path):
    """THE F8 FAILURE SEQUENCE (bare connect — the shipped SessionStart hook
    shape): the agent's own presence read 504s, the old code treated that as
    'never connected' and the whole-record write stamped capabilities=[] /
    workstreams=[] over the declared record. On a read failure connect must
    not clobber what it cannot see: the remote record survives unshrunk."""
    _, path = _seed_presence(coord_backend, "a:h:r",
                             workstreams=["x"], summary="on x",
                             capabilities=["reviewer"])
    before = _store_file(tmp_path, path).read_text()

    _patch_download_none_for(monkeypatch, lambda p: p == path)

    with _as_me():
        rc = presence.cmd_connect(_connect_args(), backend=coord_backend)
    assert rc == 0, "a presence write problem must never fail the session boot"
    after = _read_store_json(tmp_path, path)
    assert after["capabilities"] == ["reviewer"], \
        "bare connect on a failed own-read shrank the declared capabilities"
    assert after["workstreams"] == ["x"]
    assert _store_file(tmp_path, path).read_text() == before


def test_f8_connect_confirmed_absent_still_writes_fresh_record(coord_backend,
                                                               tmp_path):
    """Counter-case (current behavior preserved): a genuinely-first connect —
    probe-confirmed absent on a reachable bus — writes the full fresh record."""
    with _as_me():
        rc = presence.cmd_connect(_connect_args(role=["reviewer"]),
                                  backend=coord_backend)
    assert rc == 0
    rec = _read_store_json(
        tmp_path, remote.presence_remote_path(views.agent_slug("a:h:r")))
    assert rec["agent"] == "a:h:r"
    assert rec["capabilities"] == ["reviewer"]


def test_f8_workstream_mutations_abort_on_own_read_failure(coord_backend,
                                                           monkeypatch,
                                                           tmp_path):
    """workstream add/set/clear rebuild the WHOLE record from the read — on a
    failed read they must abort with a clear error, never write a wiped one."""
    _, path = _seed_presence(coord_backend, "a:h:r",
                             workstreams=["x"], summary="on x",
                             capabilities=["reviewer"])
    before = _store_file(tmp_path, path).read_text()
    _patch_download_none_for(monkeypatch, lambda p: p == path)

    for action, ws in (("add", "extra"), ("set", "a,b"), ("clear", None)):
        args = types.SimpleNamespace(agent=None, ws_action=action,
                                     workstreams=ws, summary=None,
                                     format="table")
        with _as_me():
            rc = presence.cmd_workstream(args, backend=coord_backend)
        assert rc == 1, f"workstream {action} must abort on an unreadable own record"
    assert _store_file(tmp_path, path).read_text() == before, \
        "a workstream mutation wrote a wiped record over an unreadable one"


def test_f8_capability_rmw_aborts_on_own_read_failure(coord_backend,
                                                      monkeypatch, tmp_path):
    """add_capabilities / remove_capability (the C4/C5 merge-safe RMW) start
    from the own-record read; a failed read must abort the rewrite (False),
    never rebuild from an empty base."""
    _, path = _seed_presence(coord_backend, "a:h:r",
                             workstreams=["x"], capabilities=["alpha", "beta"])
    before = _store_file(tmp_path, path).read_text()
    _patch_download_none_for(monkeypatch, lambda p: p == path)

    assert presence.add_capabilities("a:h:r", ["gamma"],
                                     backend=coord_backend) is False
    assert presence.remove_capability("a:h:r", "alpha",
                                      backend=coord_backend) is False
    assert _store_file(tmp_path, path).read_text() == before, \
        "a capability RMW rebuilt the record from an unreadable base"


def test_f8_inbox_roles_read_failure_is_empty_set_not_crash(coord_backend,
                                                            monkeypatch):
    """inbox._my_roles reads the own record through the same loader; the
    sentinel must collapse to the documented fail-safe empty set (no @role
    delivery on a blind read), never an AttributeError into the inbox."""
    _, path = _seed_presence(coord_backend, "a:h:r", capabilities=["reviewer"])
    _patch_download_none_for(monkeypatch, lambda p: p == path)
    assert inbox._my_roles("a:h:r", backend=coord_backend) == set()
