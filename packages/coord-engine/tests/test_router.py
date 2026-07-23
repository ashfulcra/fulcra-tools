"""Tests for the wake-router core (W4) — `coord-engine router run`.

Normative contract: docs/coord/wake-router-PLAN.md §2/§2.5 + wake-router-SPEC.md
§4 (relay contract). The load-bearing pins, each demanded by the plan:

- tie-safe inclusive scan: equal-mtime shards are the COMMON case (minute
  granularity) — `>= watermark` rescan + processed-ledger suppression, with the
  equal-mtime two-shard case tested explicitly (plan §2, REQUIRED);
- watermark is monotonic (never written backwards);
- missing/corrupt cursor → observe-only pass that reports loudly, enqueues
  nothing, then bootstraps a fresh cursor;
- enqueue-only: W4 executes nothing, and the router writes ONLY under
  `team/<team>/_coord/router/` (spec §4 namespace-writer rule);
- absent agent in config ⇒ observe-only for that agent — enablement is
  explicit, never default;
- LAPSED agents (W3 marker, consumed here) get reduced-cadence check-in
  decisions, roles intact, no park (the shared W3/W4 acceptance case);
- config validation is fail-visible: free-form adapter_args keys, out-of-range
  lapsed_checkin_min, unknown adapters, and unallowlisted executors are
  validation errors → the unroutable lane, never a silent drop.

Cheap-beats-clever: stdlib-only, FakeTransport, pinned clock.
"""

import argparse
import json
from datetime import datetime, timezone

import pytest

from coord_engine import cli, okf, router, tasks
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
RP = f"team/{TEAM}/_coord/router/"
TASKP = f"team/{TEAM}/task/"

PINNED_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-07-23T12:00:00Z"


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _args(**kw):
    ns = argparse.Namespace(team=TEAM, once=True, json=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _task(tid, assignee, priority="P2", status="proposed"):
    return okf.render_frontmatter(
        {"type": "Task", "title": tid, "id": tid, "status": status,
         "priority": priority, "assignee": assignee,
         "timestamp": "2026-07-23T11:00:00Z"}
    ) + f"\n# {tid}\n"


def _presence(agent, ts, engagement=None):
    fm = {"type": "Presence", "title": f"presence — {agent}", "agent": agent,
          "timestamp": ts}
    if engagement is not None:
        fm["engagement"] = engagement
    return okf.render_frontmatter(fm) + "\n# beat\n"


def _put_presence(t, agent, ts, engagement=None):
    t.put(f"team/{TEAM}/presence/{tasks.agent_key(agent)}.md",
          _presence(agent, ts, engagement))


def _cursor(watermark, processed=None):
    return json.dumps({"watermark": watermark, "processed": processed or {}})


def _config(agents=None, executors=None):
    doc = dict(agents or {})
    if executors is not None:
        doc["executors"] = executors
    return json.dumps(doc)


AGENT = "worker-a"
CLOUD_CFG = {"priority_floor": "P2", "debounce_min": 15,
             "adapter": "managed-agents-message",
             "adapter_args": {"session_ref": "s-1"}}


def _base(t, *, cursor=_cursor("2026-07-23T11:00:00Z"), config=None):
    if cursor is not None:
        t.put(RP + "cursor.json", cursor)
    t.put(RP + "config.json", config if config is not None
          else _config({AGENT: dict(CLOUD_CFG)}))


def _queue_entries(t):
    return {p: json.loads(c) for p, c in t.store.items()
            if p.startswith(RP + "queue/")}


# --- pure units -------------------------------------------------------------

def test_poll_interval_is_the_fixed_plan_constant():
    assert router.ROUTER_POLL_SECONDS == 60  # plan §2.5: FIXED, not tunable


def test_parse_store_mtime():
    dt = router.parse_store_mtime("2026-07-22 04:22PM UTC")
    assert dt == datetime(2026, 7, 22, 16, 22, tzinfo=timezone.utc)
    assert router.parse_store_mtime("garbage") is None
    assert router.parse_store_mtime(None) is None


def test_cursor_parse_absent_and_corrupt():
    cur, reason = router.parse_cursor(None)
    assert cur is None and "missing" in reason
    cur, reason = router.parse_cursor("{not json")
    assert cur is None and "corrupt" in reason
    cur, reason = router.parse_cursor(_cursor("2026-07-23T11:00:00Z", {"k": NOW_ISO}))
    assert reason is None and cur["processed"] == {"k": NOW_ISO}


def test_config_validation_rejects_free_form_adapter_args():
    agents, _, errors = router.validate_config(_config({
        AGENT: {**CLOUD_CFG, "adapter_args": {"session_ref": "s", "cmd": "rm"}}}))
    assert AGENT not in agents and "cmd" in errors[AGENT]


def test_config_validation_lapsed_checkin_range():
    agents, _, errors = router.validate_config(_config({
        AGENT: {**CLOUD_CFG, "lapsed_checkin_min": 30}}))
    assert AGENT not in agents and "lapsed_checkin_min" in errors[AGENT]
    agents, _, errors = router.validate_config(_config({
        AGENT: {**CLOUD_CFG, "lapsed_checkin_min": 360}}))
    assert AGENT in agents and not errors


def test_config_validation_unknown_adapter():
    agents, _, errors = router.validate_config(_config({
        AGENT: {**CLOUD_CFG, "adapter": "spawn-session"}}))
    assert AGENT not in agents and "adapter" in errors[AGENT]


def test_delivered_fold():
    shards = [
        {"agent": "a", "delivered_at": "2026-07-23T10:00:00Z", "source_shard": "s1"},
        {"agent": "a", "delivered_at": "2026-07-23T11:00:00Z", "source_shard": "s2"},
        {"agent": "b", "delivered_at": "2026-07-23T09:00:00Z", "source_shard": "s3"},
    ]
    view = router.fold_delivered(shards)
    assert view["a"] == {"last_delivered_at": "2026-07-23T11:00:00Z", "count": 2,
                         "last_source_shard": "s2"}
    assert view["b"]["count"] == 1


# --- tie-safety + cursor (the required pins) --------------------------------

def test_equal_mtime_tie_is_rescanned_and_ledger_suppressed():
    """Plan §2 REQUIRED: two shards share the watermark minute; one was
    processed pre-checkpoint, the other landed after — the inclusive >= scan
    must surface the unprocessed one, and the ledger must silence the other."""
    t = FakeTransport()
    m = "2026-07-23 11:30AM UTC"
    t.put(TASKP + "item-old.md", _task("item-old", AGENT, "P1"), mtime=m)
    t.put(TASKP + "item-new.md", _task("item-new", AGENT, "P1"), mtime=m)
    _base(t, cursor=_cursor("2026-07-23T11:30:00Z",
                            {f"item-old:{AGENT}": "2026-07-23T11:30:00Z"}))
    assert cli.cmd_router_run(_args(), t) == 0
    entries = _queue_entries(t)
    assert len(entries) == 1
    (entry,) = entries.values()
    assert entry["source_shard"] == "item-new"
    cur = json.loads(t.store[RP + "cursor.json"])
    assert f"item-new:{AGENT}" in cur["processed"]
    assert f"item-old:{AGENT}" in cur["processed"]  # retained, not dropped


def test_watermark_is_monotonic():
    t = FakeTransport()
    t.put(TASKP + "old-item.md", _task("old-item", AGENT), mtime="2026-07-23 10:00AM UTC")
    _base(t, cursor=_cursor("2026-07-23T11:45:00Z"))
    assert cli.cmd_router_run(_args(), t) == 0
    cur = json.loads(t.store[RP + "cursor.json"])
    assert cur["watermark"] == "2026-07-23T11:45:00Z"  # never written backwards


def test_missing_cursor_is_observe_only_then_bootstraps(capsys):
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t, cursor=None)
    assert cli.cmd_router_run(_args(), t) == 0
    assert _queue_entries(t) == {}                      # nothing enqueued
    out = capsys.readouterr()
    assert "observe-only" in (out.out + out.err).lower()
    cur = json.loads(t.store[RP + "cursor.json"])        # bootstrapped
    assert cur["watermark"] == "2026-07-23T11:30:00Z"
    assert f"item-1:{AGENT}" in cur["processed"]


def test_corrupt_cursor_is_observe_only_and_loud(capsys):
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t, cursor="{broken")
    assert cli.cmd_router_run(_args(), t) == 0
    assert _queue_entries(t) == {}
    assert "corrupt" in capsys.readouterr().err.lower()


def test_processed_ledger_suppresses_second_pass():
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    assert cli.cmd_router_run(_args(), t) == 0
    first = set(_queue_entries(t))
    assert cli.cmd_router_run(_args(), t) == 0
    assert set(_queue_entries(t)) == first              # replay is a no-op


# --- policy -----------------------------------------------------------------

def test_interrupt_enqueued_with_cloud_executor():
    t = FakeTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    assert cli.cmd_router_run(_args(), t) == 0
    (entry,) = _queue_entries(t).values()
    assert entry["agent"] == AGENT
    assert entry["priority"] == "P1"
    assert entry["source_shard"] == "urgent-1"
    assert entry["adapter"] == "managed-agents-message"
    assert entry["executor"] == "decision-plane"
    assert entry["queued_at"] == NOW_ISO
    assert entry["not_before"] <= NOW_ISO


def test_host_local_adapter_uses_allowlisted_executor():
    t = FakeTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    cfg = {AGENT: {**CLOUD_CFG, "adapter": "codex-exec-resume",
                   "adapter_args": {"thread_id": "th-1"}, "executor": "mac-1"}}
    _base(t, config=_config(cfg, executors=["mac-1"]))
    assert cli.cmd_router_run(_args(), t) == 0
    (entry,) = _queue_entries(t).values()
    assert entry["executor"] == "mac-1"


def test_host_local_adapter_without_allowlisted_executor_is_unroutable(capsys):
    t = FakeTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    cfg = {AGENT: {**CLOUD_CFG, "adapter": "macos-notify", "adapter_args": {}}}
    _base(t, config=_config(cfg))   # no executors allowlist
    assert cli.cmd_router_run(_args(), t) == 0
    assert _queue_entries(t) == {}
    assert "unroutable" in (capsys.readouterr().out.lower())


def test_below_floor_batches_no_queue_entry():
    t = FakeTransport()
    t.put(TASKP + "fyi-1.md", _task("fyi-1", AGENT, "P3"),
          mtime="2026-07-23 11:30AM UTC")
    cfg = {AGENT: {**CLOUD_CFG, "priority_floor": "P1"}}
    _base(t, config=_config(cfg))
    assert cli.cmd_router_run(_args(), t) == 0
    assert _queue_entries(t) == {}
    cur = json.loads(t.store[RP + "cursor.json"])
    assert f"fyi-1:{AGENT}" in cur["processed"]         # classified, ledgered


def test_debounce_coalesces_same_pass():
    t = FakeTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(TASKP + "urgent-2.md", _task("urgent-2", AGENT, "P1"),
          mtime="2026-07-23 11:31AM UTC")
    _base(t)
    assert cli.cmd_router_run(_args(), t) == 0
    assert len(_queue_entries(t)) == 1                  # one wake per window


def test_debounce_respects_recent_delivery():
    t = FakeTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "delivered/urgent-0-worker-a.json", json.dumps(
        {"agent": AGENT, "delivered_at": "2026-07-23T11:55:00Z",
         "source_shard": "urgent-0"}))
    _base(t)   # debounce_min 15; delivery 5 min ago
    assert cli.cmd_router_run(_args(), t) == 0
    assert _queue_entries(t) == {}
    cur = json.loads(t.store[RP + "cursor.json"])
    assert f"urgent-1:{AGENT}" in cur["processed"]


def test_unconfigured_agent_is_observe_only():
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", "stranger", "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    assert cli.cmd_router_run(_args(), t) == 0
    assert _queue_entries(t) == {}
    cur = json.loads(t.store[RP + "cursor.json"])
    assert "item-1:stranger" in cur["processed"]


def test_lapsed_agent_gets_reduced_cadence_checkin_roles_intact():
    """The shared W3/W4 acceptance case: a W3-marked lapsed fixture receives a
    check-in decision at the reduced cadence — roles intact, no park, and the
    router touches nothing outside its own namespace."""
    t = FakeTransport()
    _put_presence(t, AGENT, "2026-07-23T03:00:00Z",
                  engagement={"mode": "session", "until": "2026-07-23T04:00:00Z",
                              "state": "lapsed", "lapsed_at": "2026-07-23T04:05:00Z"})
    t.put(TASKP + "fyi-1.md", _task("fyi-1", AGENT, "P2"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "delivered/prev-worker-a.json", json.dumps(
        {"agent": AGENT, "delivered_at": "2026-07-23T10:00:00Z",
         "source_shard": "prev"}))
    cfg = {AGENT: {**CLOUD_CFG, "priority_floor": "P1", "lapsed_checkin_min": 120}}
    _base(t, config=_config(cfg))
    before = set(t.store)
    assert cli.cmd_router_run(_args(), t) == 0
    (entry,) = _queue_entries(t).values()
    # cadence: last delivery 10:00 + 120min = 12:00 — the check-in is due at
    # exactly the reduced cadence, not before
    assert entry["not_before"] == "2026-07-23T12:00:00Z"
    # roles intact / no park: every new write is inside the router namespace
    assert all(p.startswith(RP) for p in set(t.store) - before)


def test_router_writes_only_its_own_namespace():
    t = FakeTransport()
    _put_presence(t, AGENT, "2026-07-23T11:58:00Z")
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    before = set(t.store)
    assert cli.cmd_router_run(_args(), t) == 0
    assert all(p.startswith(RP) for p in set(t.store) - before)


def test_busy_agent_defers_below_floor_items():
    t = FakeTransport()
    _put_presence(t, AGENT, "2026-07-23T11:58:00Z")     # beat 2 min ago = busy
    t.put(TASKP + "fyi-1.md", _task("fyi-1", AGENT, "P2"),
          mtime="2026-07-23 11:30AM UTC")
    cfg = {AGENT: {**CLOUD_CFG, "priority_floor": "P1"}}
    _base(t, config=_config(cfg))
    assert cli.cmd_router_run(_args(), t) == 0
    (entry,) = _queue_entries(t).values()
    assert entry["not_before"] > NOW_ISO                # queued to idle boundary


def test_broadcast_and_terminal_items_not_in_population():
    t = FakeTransport()
    t.put(TASKP + "bcast.md", _task("bcast", "*", "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(TASKP + "done-1.md", _task("done-1", AGENT, "P1", status="done"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    assert cli.cmd_router_run(_args(), t) == 0
    assert _queue_entries(t) == {}
    cur = json.loads(t.store[RP + "cursor.json"])
    assert not any(k.startswith(("bcast:", "done-1:")) for k in cur["processed"])


def test_delivered_view_regenerated_from_shards():
    t = FakeTransport()
    t.put(RP + "delivered/a1.json", json.dumps(
        {"agent": AGENT, "delivered_at": "2026-07-23T09:00:00Z",
         "source_shard": "s1"}))
    _base(t)
    assert cli.cmd_router_run(_args(), t) == 0
    view = json.loads(t.store[RP + "delivered.json"])
    assert view[AGENT]["count"] == 1
    assert view[AGENT]["last_source_shard"] == "s1"


class FlakyTransport(FakeTransport):
    """FakeTransport whose writes can be made to fail by path substring."""

    def __init__(self):
        super().__init__()
        self.fail_write_containing: set = set()

    def write(self, path, content):
        if any(s in path for s in self.fail_write_containing):
            return False
        return super().write(path, content)


def test_failed_queue_write_is_not_ledgered_and_retries(capsys):
    """codex P1 (r1): a queue upload that returns False must fail the pass —
    the key stays un-ledgered and the cursor does not advance past the item,
    so the wake is retried and eventually enqueued, never silently lost."""
    t = FlakyTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    t.fail_write_containing.add("queue/")
    assert cli.cmd_router_run(_args(), t) == 1          # fail-visible
    assert _queue_entries(t) == {}
    cur = json.loads(t.store[RP + "cursor.json"])
    assert f"urgent-1:{AGENT}" not in cur["processed"]  # NOT consumed
    assert "queue write failed" in capsys.readouterr().err
    t.fail_write_containing.clear()
    assert cli.cmd_router_run(_args(), t) == 0          # retried next pass
    assert len(_queue_entries(t)) == 1
    cur = json.loads(t.store[RP + "cursor.json"])
    assert f"urgent-1:{AGENT}" in cur["processed"]


def test_failed_cursor_write_fails_the_pass(capsys):
    t = FlakyTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    t.fail_write_containing.add("cursor.json")
    assert cli.cmd_router_run(_args(), t) == 1
    assert "checkpoint write failed" in capsys.readouterr().err


def test_corrupt_config_is_loud_and_enqueues_nothing(capsys):
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t, config="{broken")
    assert cli.cmd_router_run(_args(), t) == 0
    assert _queue_entries(t) == {}
    assert "config" in capsys.readouterr().err.lower()
