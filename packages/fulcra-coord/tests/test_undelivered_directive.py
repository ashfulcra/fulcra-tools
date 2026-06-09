"""Undelivered-directive safety net tests (report-only reconcile sub-pass).

WHY THIS EXISTS — a real, demonstrated bug:

Agents sent directives to the coord-maintainer identity, but no live session was
running as that identity (its presence had been stale for 4 days). The directives
silently rotted in a dead inbox: the bus accepted the messages into a void and
never flagged that nobody was reading them. ``cli._undelivered_directive_check``
makes that condition VISIBLE — it reconciles open directives addressed to an
OFFLINE/stale agent and surfaces them as health debt.

REPORT-ONLY by construction (mirrors ``_event_parity_check`` /
``_event_dual_write_health`` / ``_directive_parity_check``): the check NEVER
mutates a task or view, NEVER reroutes, and a failure here can NEVER change
reconcile's exit code. Rerouting to a live role-holder is a deliberately
separate, later phase — NOT built here.

An "undelivered" directive is a task where ALL of:

  * it's a DIRECTED directive: ``assignee`` is a concrete agent id — NOT ``"*"``
    (broadcast), NOT the human handle, NOT empty;
  * it's still OPEN and un-picked-up: ``status == "proposed"`` (an
    active/done/abandoned task was received);
  * the assignee has NOT acked it (no ``inbox_ack`` event / ``acked_by`` entry
    from the assignee);
  * the assignee is NOT in the LIVE set (no presence record, or stale presence).

Liveness reuses the EXISTING rule ``cmd_agents`` uses — ``views.build_presence``
+ ``views.presence_liveness`` over the presence aggregate — so "stale" means the
same thing everywhere. An agent is LIVE iff its presence ``liveness`` is ``live``
or ``idle``; ``stale`` (or absent) is NOT live.

Written test-first (TDD): these FAIL before ``_undelivered_directive_check`` is
added to cli.py, and PASS once the function and its wiring are in place.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from fulcra_coord import cli, identity, query, remote, schema, views


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _seed_presence(backend, *, agents):
    """Write a presence aggregate to the bus from a list of (agent, last_seen)
    pairs, building it exactly the way the live write-path / cmd_agents does
    (``views.build_presence`` annotates liveness from last_seen)."""
    records = [
        schema.make_presence(agent, last_seen=last_seen)
        for agent, last_seen in agents
    ]
    view = views.build_presence(records)
    remote.upload_json(view, remote.presence_view_path(), backend=backend)


def _directed_directive(*, task_id, assignee, status="proposed", acked_by=None,
                        owner="alice"):
    """A directed directive-task: assignee is a concrete agent, status proposed."""
    task = schema.make_task(
        title="do the thing", workstream="ws", agent=owner,
        owner_agent=owner, assignee=assignee, task_id=task_id,
    )
    task["status"] = status
    if acked_by:
        task["acked_by"] = list(acked_by)
    return task


# ---------------------------------------------------------------------------
# 1. The core bug: a proposed directive to an OFFLINE/absent agent is flagged.
# ---------------------------------------------------------------------------

def test_undelivered_flagged_when_assignee_offline(coord_backend):
    """A proposed directive assigned to an agent with NO presence record is
    undelivered — nobody is reading that inbox."""
    _seed_presence(coord_backend, agents=[("alice", _iso(datetime.now(timezone.utc)))])
    task = _directed_directive(task_id="TASK-OFF", assignee="bob")  # bob: no presence
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert report["count"] == 1
    ids = {u["id"] for u in report["undelivered"]}
    assert "TASK-OFF" in ids
    flagged = next(u for u in report["undelivered"] if u["id"] == "TASK-OFF")
    assert flagged["assignee"] == "bob"
    assert "age_days" in flagged


def test_undelivered_flagged_when_assignee_presence_stale(coord_backend):
    """A proposed directive to an agent whose presence is STALE (older than the
    stale threshold) is undelivered — the session is dead, not merely idle.

    A LIVE agent (alice) is seeded alongside so the live set is NON-EMPTY: only
    then can the check confidently say bob's stale inbox is undelivered (an empty
    live set is indeterminate — see the presence-unavailable tests)."""
    stale_seen = _iso(datetime.now(timezone.utc) - timedelta(days=4))
    _seed_presence(coord_backend, agents=[
        ("alice", _iso(datetime.now(timezone.utc))),  # live -> non-empty roster
        ("bob", stale_seen),
    ])
    task = _directed_directive(task_id="TASK-STALE", assignee="bob")
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert "TASK-STALE" in {u["id"] for u in report["undelivered"]}


# ---------------------------------------------------------------------------
# 2. The negatives — each gate is necessary.
# ---------------------------------------------------------------------------

def test_not_flagged_when_assignee_live(coord_backend):
    """Same directive, but the assignee IS live (recent presence) -> NOT flagged."""
    _seed_presence(coord_backend, agents=[("bob", _iso(datetime.now(timezone.utc)))])
    task = _directed_directive(task_id="TASK-LIVE", assignee="bob")
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert report["count"] == 0
    assert "TASK-LIVE" not in {u["id"] for u in report["undelivered"]}


def test_broadcast_never_flagged(coord_backend):
    """A broadcast (assignee == '*') has no single recipient inbox — NOT flagged."""
    _seed_presence(coord_backend, agents=[("alice", _iso(datetime.now(timezone.utc)))])
    task = _directed_directive(task_id="TASK-BCAST", assignee="*")
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert "TASK-BCAST" not in {u["id"] for u in report["undelivered"]}
    assert report["count"] == 0


def test_acked_directive_not_flagged(coord_backend):
    """A directive the assignee already ACKED (inbox_ack event) is delivered ->
    NOT flagged, even if the assignee is now offline."""
    task = _directed_directive(task_id="TASK-ACKED", assignee="bob")
    # bob saw it: an inbox_ack event from bob (the form task_summary folds).
    task = schema.apply_event(task, event_type="inbox_ack", by="bob")
    # bob is offline (no presence) — but the ack means it was delivered.
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert "TASK-ACKED" not in {u["id"] for u in report["undelivered"]}
    assert report["count"] == 0


def test_acked_via_acked_by_field_not_flagged(coord_backend):
    """An ack recorded as an ``acked_by`` entry (summary-only ack path) also counts
    as delivered -> NOT flagged."""
    task = _directed_directive(task_id="TASK-ACKBY", assignee="bob", acked_by=["bob"])
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert "TASK-ACKBY" not in {u["id"] for u in report["undelivered"]}


def test_active_task_not_flagged(coord_backend):
    """An active task was received and picked up — NOT undelivered."""
    task = _directed_directive(task_id="TASK-ACTIVE", assignee="bob", status="active")
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert "TASK-ACTIVE" not in {u["id"] for u in report["undelivered"]}


def test_done_task_not_flagged(coord_backend):
    """A done task was clearly received and acted on — NOT undelivered."""
    task = _directed_directive(task_id="TASK-DONE", assignee="bob", status="done")
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert "TASK-DONE" not in {u["id"] for u in report["undelivered"]}


def test_human_assignee_not_flagged(coord_backend):
    """A directive to the human handle is NOT a presence agent — humans don't have
    presence records, so this must NOT be flagged as undelivered."""
    human = identity.resolve_human()
    task = _directed_directive(task_id="TASK-HUMAN", assignee=human)
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert "TASK-HUMAN" not in {u["id"] for u in report["undelivered"]}
    assert report["count"] == 0


def test_empty_assignee_not_flagged(coord_backend):
    """A task with no assignee is not a directed directive — NOT flagged."""
    task = _directed_directive(task_id="TASK-NOASSIGN", assignee=None)
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert report["count"] == 0


# ---------------------------------------------------------------------------
# 3. Report-only: never mutates, and the list is capped.
# ---------------------------------------------------------------------------

def test_check_does_not_mutate_tasks(coord_backend):
    """The check must not mutate the task dicts it inspects (report-only)."""
    import copy
    task = _directed_directive(task_id="TASK-NOMUT", assignee="bob")
    before = copy.deepcopy(task)
    cli._undelivered_directive_check([task], backend=coord_backend)
    assert task == before


def test_list_is_capped_but_count_is_total(coord_backend):
    """The undelivered LIST is capped (so the timeline note stays bounded), but
    ``count`` reflects the TRUE total — the count is never silently truncated."""
    # A live agent so the roster is NON-EMPTY (an empty roster is indeterminate
    # and would short-circuit to presence_unavailable before any enumeration).
    _seed_presence(coord_backend, agents=[("alice", _iso(datetime.now(timezone.utc)))])
    tasks = [
        _directed_directive(task_id=f"TASK-CAP-{i:03d}", assignee="bob")
        for i in range(60)
    ]
    report = cli._undelivered_directive_check(tasks, backend=coord_backend)
    assert report["count"] == 60          # true total, not truncated
    assert len(report["undelivered"]) <= 50  # list capped
    assert report.get("truncated") is True


def test_check_never_raises_on_bad_presence(coord_backend):
    """A presence-load failure must NOT raise — best-effort: when liveness can't be
    determined, the check degrades safely (returns a valid report, no crash)."""
    task = _directed_directive(task_id="TASK-BADPRES", assignee="bob")
    with mock.patch.object(remote, "download_json", side_effect=RuntimeError("boom")):
        report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert "count" in report
    assert "undelivered" in report


# ---------------------------------------------------------------------------
# 3b. The cry-wolf flood: a presence-READ failure (download_json -> None, the
#     non-raising transport-failure path) must NOT enumerate every directive as
#     undelivered. It is INDETERMINATE, not "all dead" — emit a single distinct
#     ``presence_unavailable`` signal instead of a flood.
# ---------------------------------------------------------------------------

def test_no_flood_when_presence_read_returns_none(coord_backend):
    """THE FLOOD REGRESSION (load-bearing). ``remote.download`` returns ``None``
    (it does NOT raise) on any transport failure, so the presence-aggregate read
    can come back ``None`` without the helper's try/except firing. When that
    happens while there ARE directed ``proposed`` directives to (would-be) live
    agents, the check must NOT flag them all undelivered — we can't distinguish
    "assignee offline" from "presence unavailable". Assert NO flood: count 0,
    empty list, and the distinct ``presence_unavailable`` signal set True.

    RED before the fix: the old code took ``_live_agent_ids`` -> empty set ->
    every directed proposed directive matched ``assignee not in live`` and was
    flagged, so count == 2 here (a flood)."""
    tasks = [
        _directed_directive(task_id="TASK-FLOOD-1", assignee="bob"),
        _directed_directive(task_id="TASK-FLOOD-2", assignee="carol"),
    ]
    # Drive ONLY the presence-aggregate read to None (the real transport-failure
    # shape: remote.download_json returns None, no raise).
    with mock.patch.object(remote, "download_json", return_value=None):
        report = cli._undelivered_directive_check(tasks, backend=coord_backend)
    assert report["count"] == 0
    assert report["undelivered"] == []
    assert report.get("presence_unavailable") is True


def test_no_flood_when_roster_empty(coord_backend):
    """Empty roster: the presence aggregate LOADS fine but contains zero live
    agents. We still cannot confirm any specific assignee is offline (an empty
    live set is indistinguishable from "presence unavailable"), so this is also
    INDETERMINATE — ``presence_unavailable`` True, no enumeration."""
    # An aggregate that builds to zero live agents (all entries stale).
    stale_seen = _iso(datetime.now(timezone.utc) - timedelta(days=10))
    _seed_presence(coord_backend, agents=[("ghost", stale_seen)])
    task = _directed_directive(task_id="TASK-EMPTYROSTER", assignee="bob")
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert report["count"] == 0
    assert report["undelivered"] == []
    assert report.get("presence_unavailable") is True


def test_genuine_undelivered_still_flagged_with_nonempty_roster(coord_backend):
    """The genuine detection is PRESERVED: with a NON-EMPTY live set (≥1 live
    agent) we CAN confidently say a proposed directive to a DIFFERENT, offline
    agent is undelivered. ``presence_unavailable`` is False and the directive is
    flagged — the flood fix must not blunt the real signal."""
    _seed_presence(coord_backend, agents=[("alice", _iso(datetime.now(timezone.utc)))])
    task = _directed_directive(task_id="TASK-REALDEAD", assignee="bob")  # bob offline
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    assert report["count"] == 1
    assert "TASK-REALDEAD" in {u["id"] for u in report["undelivered"]}
    assert report.get("presence_unavailable") is False


def test_age_days_not_inf_on_missing_timestamp(coord_backend):
    """A directive whose timestamps are missing/unparseable must NOT render an
    ``inf`` age in the warn line (``_age_hours`` returns +inf for an unparseable
    stamp). With a non-empty live set so the directive is genuinely flagged, the
    rendered ``age_days`` must be a finite number or the ``"?"`` sentinel."""
    _seed_presence(coord_backend, agents=[("alice", _iso(datetime.now(timezone.utc)))])
    task = _directed_directive(task_id="TASK-NOTS", assignee="bob")
    task["created_at"] = ""
    task["updated_at"] = ""
    report = cli._undelivered_directive_check([task], backend=coord_backend)
    flagged = next(u for u in report["undelivered"] if u["id"] == "TASK-NOTS")
    age = flagged["age_days"]
    assert age == "?" or (isinstance(age, (int, float)) and age != float("inf"))


# ---------------------------------------------------------------------------
# 4. Wiring: reconcile populates record["undelivered_directives"]; an exception
#    in the check NEVER changes reconcile's exit code (report-only).
# ---------------------------------------------------------------------------

def test_reconcile_populates_undelivered_directives(coord_backend):
    """A reconcile run folds the undelivered-directives block into the health
    record. Captures the health-record upload and asserts the block is present."""
    captured = {}
    real_upload = remote.upload_json

    def _capture(record, path, **kwargs):
        if "/health/" in path and isinstance(record, dict):
            captured["record"] = record
        return real_upload(record, path, **kwargs)

    task = _directed_directive(task_id="TASK-RECON-UD", assignee="bob")
    remote.upload_json(task, remote.task_remote_path("TASK-RECON-UD"), backend=coord_backend)
    with mock.patch.object(remote, "upload_json", side_effect=_capture):
        rc = cli.cmd_reconcile(mock.Mock(), backend=coord_backend)
    assert rc == 0
    assert "record" in captured, "no health record was uploaded"
    assert "undelivered_directives" in captured["record"]
    assert "count" in captured["record"]["undelivered_directives"]


def test_reconcile_exit_code_unaffected_by_undelivered_exception(coord_backend):
    """If _undelivered_directive_check raises, reconcile must STILL return 0 and the
    undelivered_directives block is simply absent (best-effort / report-only)."""
    task = _directed_directive(task_id="TASK-RAISE-UD", assignee="bob")
    remote.upload_json(task, remote.task_remote_path("TASK-RAISE-UD"), backend=coord_backend)
    with mock.patch.object(
        cli, "_undelivered_directive_check", side_effect=RuntimeError("boom")
    ):
        rc = cli.cmd_reconcile(mock.Mock(), backend=coord_backend)
    assert rc == 0


# ---------------------------------------------------------------------------
# 5. Operator surfacing: `status` warns when health records report undelivered
#    directives, reading the persisted health surface (no cli import cycle).
# ---------------------------------------------------------------------------

def test_status_warns_on_undelivered_directives(coord_backend, capsys):
    """A maintainer running `status` SEES a one-line warning when a per-host health
    record reports undelivered directives. Read from the persisted health surface
    reconcile writes — query.py never imports cli."""
    # Seed a health record carrying the undelivered block (as reconcile would write).
    remote.upload_json(
        {"host": "host-a", "undelivered_directives": {"count": 3, "undelivered": []}},
        remote.health_remote_path("host-a"), backend=coord_backend,
    )
    rc = query.cmd_status(mock.Mock(workstream=None, agent=None, format="table"),
                          backend=coord_backend)
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 directive(s) undelivered" in out


def test_status_no_warning_when_none_undelivered(coord_backend, capsys):
    """status prints NO undelivered warning when no host reports any (count 0)."""
    remote.upload_json(
        {"host": "host-a", "undelivered_directives": {"count": 0, "undelivered": []}},
        remote.health_remote_path("host-a"), backend=coord_backend,
    )
    rc = query.cmd_status(mock.Mock(workstream=None, agent=None, format="table"),
                          backend=coord_backend)
    assert rc == 0
    out = capsys.readouterr().out
    assert "undelivered" not in out
