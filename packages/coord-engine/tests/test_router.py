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
    """FakeTransport whose writes/listings can be made to fail by path substring."""

    def __init__(self):
        super().__init__()
        self.fail_write_containing: set = set()
        self.fail_list_containing: set = set()

    def write(self, path, content):
        if any(s in path for s in self.fail_write_containing):
            return False
        return super().write(path, content)

    def list_dir(self, prefix):
        if any(s in prefix for s in self.fail_list_containing):
            from coord_engine.transport import TransportError
            raise TransportError("boom")
        return super().list_dir(prefix)


class FeedTransport(FlakyTransport):
    """FlakyTransport that also serves a data-updates feed (E3). ``_feed`` is
    the raw change list `updates()` returns; set to None to model feed doubt."""

    def __init__(self):
        super().__init__()
        self._feed: object = []

    def set_feed(self, feed):
        self._feed = feed

    def updates(self, since, *, team=None):
        return None if self._feed is None else list(self._feed)


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


# --- W5 opening commit: two codex #460 verdict fixes ------------------------

def test_delivered_listing_failure_preserves_the_populated_view(capsys):
    """codex #460 Fix 1: a delivered/ LISTING error must NOT overwrite the
    populated delivered.json with {} (which would also feed empty
    last_delivered_at into decide() for the whole pass). Skip the refold, stay
    fail-visible, and let the pass otherwise proceed."""
    t = FlakyTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    prior = {"other": {"last_delivered_at": "2026-07-23T09:00:00Z",
                       "count": 5, "last_source_shard": "old"}}
    t.put(RP + "delivered.json", json.dumps(prior))
    t.fail_list_containing.add("delivered/")
    assert cli.cmd_router_run(_args(), t) == 0              # pass proceeds
    assert json.loads(t.store[RP + "delivered.json"]) == prior   # NOT clobbered
    assert "delivered/ listing degraded" in capsys.readouterr().err
    assert len(_queue_entries(t)) == 1                      # P1 still enqueued


def test_degraded_delivered_listing_decides_from_persisted_view(capsys):
    """codex #466: when the delivered/ listing fails, decide() must use the
    last-known-good delivered.json — an agent delivered inside its debounce
    window keeps that debounce instead of being re-woken from an empty fold."""
    t = FlakyTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    # persisted view: AGENT delivered 5 min ago (inside the 15-min debounce)
    t.put(RP + "delivered.json", json.dumps(
        {AGENT: {"last_delivered_at": "2026-07-23T11:55:00Z", "count": 1,
                 "last_source_shard": "prior"}}))
    t.fail_list_containing.add("delivered/")
    assert cli.cmd_router_run(_args(), t) == 0
    assert _queue_entries(t) == {}          # debounced from the persisted view
    assert "delivered/ listing degraded" in capsys.readouterr().err


def test_double_degradation_fails_closed_when_history_unknown(capsys):
    """codex #466 P1: delivered/ listing fails AND delivered.json is absent
    (read None) ⇒ delivery history is UNKNOWN, not empty. Fail the pass CLOSED
    — nonzero, no queue write, no cursor advancement."""
    t = FlakyTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)                                   # cursor watermark 11:00:00Z
    # delivered.json intentionally absent → read returns None
    t.fail_list_containing.add("delivered/")   # authoritative shard listing down
    assert cli.cmd_router_run(_args(), t) == 1
    assert _queue_entries(t) == {}             # nothing enqueued from unknown history
    cur = json.loads(t.store[RP + "cursor.json"])
    assert cur["watermark"] == "2026-07-23T11:00:00Z"   # cursor NOT advanced
    assert f"urgent-1:{AGENT}" not in cur["processed"]  # NOT consumed
    assert "UNKNOWN" in capsys.readouterr().err


def test_double_degradation_fails_closed_on_malformed_persisted_view(capsys):
    """codex #466 P1: listing fails AND delivered.json is malformed (not a valid
    mapping) ⇒ still unknown history ⇒ fail closed."""
    t = FlakyTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    t.put(RP + "delivered.json", "{not valid json")
    t.fail_list_containing.add("delivered/")
    assert cli.cmd_router_run(_args(), t) == 1
    assert _queue_entries(t) == {}
    assert json.loads(t.store[RP + "cursor.json"])["watermark"] == "2026-07-23T11:00:00Z"


def test_degraded_listing_with_empty_persisted_view_is_known_history():
    """A valid EMPTY mapping is KNOWN history (router ran, delivered nothing) —
    it must proceed, not fail closed."""
    t = FlakyTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    t.put(RP + "delivered.json", json.dumps({}))   # known-empty
    t.fail_list_containing.add("delivered/")
    assert cli.cmd_router_run(_args(), t) == 0      # proceeds
    assert len(_queue_entries(t)) == 1              # P1 enqueued (no history ⇒ no debounce)


def test_delivered_fold_orders_by_parsed_time_not_lexical_string():
    """codex #460 Fix 2: a later UTC delivery must win over an earlier one whose
    +offset string sorts later lexically ('…T14:00+02:00' = 12:00Z is earlier
    than '…T12:30Z' but sorts after it as a string)."""
    shards = [
        {"agent": "a", "delivered_at": "2026-07-23T14:00:00+02:00",  # 12:00Z
         "source_shard": "early"},
        {"agent": "a", "delivered_at": "2026-07-23T12:30:00Z",       # later
         "source_shard": "late"},
    ]
    view = router.fold_delivered(shards)
    assert view["a"]["last_source_shard"] == "late"
    assert view["a"]["last_delivered_at"] == "2026-07-23T12:30:00Z"  # normalized Z


# --- W5: adapter integration + execution (pure core) ------------------------

def _q_entry(agent=AGENT, adapter="managed-agents-message",
             executor=router.DECISION_PLANE, source="item-1", **extra):
    e = {"agent": agent, "reason": "check your bus", "source_shard": source,
         "priority": "P1", "queued_at": NOW_ISO, "not_before": NOW_ISO,
         "adapter": adapter, "executor": executor}
    e.update(extra)
    return e


def test_decision_plane_owns_only_its_executor():
    """The decision plane executes exactly `executor: decision-plane`; a
    host-local executor id is W5.5's, never fired here (plan §W5)."""
    assert router.is_decision_plane_entry(_q_entry()) is True
    assert router.is_decision_plane_entry(
        _q_entry(executor="mac-mini-1")) is False
    assert router.is_decision_plane_entry({"executor": None}) is False


def test_adapter_invocation_is_a_keyed_nudge_with_no_command():
    """The relay content rule (plan §2): the wake payload carries the
    idempotency key and NO per-event command/session-mutation/raw content, so
    at-least-once delivery converges to one bus check."""
    inv = router.adapter_invocation(
        _q_entry(source="urgent-9"), {"session_ref": "sess-42"})
    key = router.idempotency_key("urgent-9", AGENT)
    assert inv["idempotency_key"] == key
    assert key in inv["message"]
    assert inv["session_ref"] == "sess-42"          # target from adapter_args
    # NO command / session-mutation / raw-content field ever rides the payload
    for banned in ("command", "cmd", "exec", "run", "payload", "session_patch",
                   "permission_mode", "url"):
        assert banned not in inv
    # the message is a fixed nudge — it names no action to take
    assert "no action is encoded" in inv["message"].lower()


def test_adapter_invocation_targets_are_allowlisted_per_adapter():
    assert router.adapter_invocation(
        _q_entry(adapter="codex-exec-resume", executor="host-x"),
        {"thread_id": "th-1"})["thread_id"] == "th-1"
    assert router.adapter_invocation(
        _q_entry(adapter="openclaw-post", executor="host-x"),
        {"endpoint_name": "wake"})["endpoint_name"] == "wake"
    # a no-target adapter carries only the safe nudge fields
    inv = router.adapter_invocation(_q_entry(adapter="routine-align"))
    assert set(inv) == {"adapter", "agent", "idempotency_key", "message"}


def test_adapter_invocation_rejects_unknown_adapter():
    with pytest.raises(ValueError):
        router.adapter_invocation(_q_entry(adapter="curl-the-internet"))


def test_claim_skippable_only_for_a_fresh_foreign_claim():
    fresh = "2026-07-23T11:55:00Z"   # 5 min before PINNED_NOW (< 10)
    stale = "2026-07-23T11:30:00Z"   # 30 min before PINNED_NOW (> 10)
    me = router.DECISION_PLANE
    # foreign + fresh → another executor is mid-flight → skip
    assert router.claim_is_skippable(
        _q_entry(claimed_by="other", claimed_at=fresh), me, PINNED_NOW) is True
    # foreign + stale → retryable (at-least-once, safe by content rule)
    assert router.claim_is_skippable(
        _q_entry(claimed_by="other", claimed_at=stale), me, PINNED_NOW) is False
    # our own claim → retryable
    assert router.claim_is_skippable(
        _q_entry(claimed_by=me, claimed_at=fresh), me, PINNED_NOW) is False
    # unclaimed → claimable
    assert router.claim_is_skippable(_q_entry(), me, PINNED_NOW) is False


def test_delivery_record_is_readable_by_the_delivered_fold():
    """A delivery-record shard must fold cleanly into `delivered.json` — the
    single-writer-per-key contract feeds the decision-plane view."""
    rec = router.delivery_record(_q_entry(source="s-7"), NOW_ISO)
    assert rec["key"] == router.idempotency_key("s-7", AGENT)
    view = router.fold_delivered([rec])
    assert view[AGENT]["last_delivered_at"] == NOW_ISO
    assert view[AGENT]["count"] == 1
    assert view[AGENT]["last_source_shard"] == "s-7"


def test_dead_letter_record_carries_entry_plus_audit():
    e = _q_entry(source="s-3")
    dl = router.dead_letter_record(
        e, attempts=3, last_error="boom", gave_up_at=NOW_ISO)
    assert dl["source_shard"] == "s-3" and dl["adapter"] == e["adapter"]
    assert (dl["attempts"], dl["last_error"], dl["gave_up_at"]) == (
        3, "boom", NOW_ISO)


def test_record_filename_deterministic_and_safe_for_colon_agent_ids():
    key = router.idempotency_key("s-1", "openclaw:discord:fulcra-skills")
    a, b = router.record_filename(key), router.record_filename(key)
    assert a == b                                   # single-writer-per-key
    assert ":" not in a and a.endswith(".json")     # store-safe
    assert router.record_filename(key) != router.record_filename(key + "x")


# --- W7: shadow-mode delivery-probe evidence primitive ----------------------

def test_shadow_evidence_record_is_a_keyed_delivery_probe():
    key = router.idempotency_key("s-1", AGENT)
    rec = router.shadow_evidence_record(
        key=key, agent=AGENT, delivered_at=NOW_ISO, path="listener")
    assert rec == {"key": key, "agent": AGENT,
                   "delivered_at": NOW_ISO, "path": "listener"}


def test_shadow_evidence_record_rejects_unknown_path():
    for good in ("listener", "adapter", "watchdog"):
        router.shadow_evidence_record(
            key="k", agent=AGENT, delivered_at=NOW_ISO, path=good)
    with pytest.raises(ValueError):
        router.shadow_evidence_record(
            key="k", agent=AGENT, delivered_at=NOW_ISO, path="somewhere-else")


def test_shadow_evidence_filename_deterministic_agent_prefixed_colon_safe():
    key = router.idempotency_key("s-1", "openclaw:discord:fulcra-skills")
    a = router.shadow_evidence_filename("openclaw:discord:fulcra-skills", key)
    assert a == router.shadow_evidence_filename(
        "openclaw:discord:fulcra-skills", key)          # deterministic
    assert a.startswith("openclaw-discord-fulcra-skills-") and a.endswith(".json")
    assert ":" not in a                                 # store-safe
    # one shard per (agent, key) — different keys ⇒ different shards
    assert a != router.shadow_evidence_filename(
        "openclaw:discord:fulcra-skills", key + "x")


def test_shadow_decision_record_valid_and_rejects_unknown_decision():
    rec = router.shadow_decision_record(
        key="k", agent=AGENT, decision="interrupt", reason="r",
        priority="P1", decided_at=NOW_ISO)
    assert rec["decision"] == "interrupt" and rec["key"] == "k"
    with pytest.raises(ValueError):
        router.shadow_decision_record(
            key="k", agent=AGENT, decision="frobnicate", reason="r",
            priority="P1", decided_at=NOW_ISO)


def _shadow_decisions(t):
    return {p: json.loads(c) for p, c in t.store.items()
            if p.startswith(RP + router.SHADOW_DECISIONS_SUBPATH)}


def _shadow_evidence(t):
    return {p: json.loads(c) for p, c in t.store.items()
            if p.startswith(RP + router.SHADOW_EVIDENCE_SUBPATH)}


def test_router_shadow_mode_persists_decisions_enqueues_nothing():
    """W7: `router run --shadow` logs + persists a decision per directed item,
    enqueues and executes nothing, but stays cursor-tracked (decides once)."""
    t = FakeTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    assert cli.cmd_router_run(_args(shadow=True), t) == 0
    assert _queue_entries(t) == {}                       # nothing enqueued
    dec = _shadow_decisions(t)
    assert len(dec) == 1
    (d,) = dec.values()
    assert (d["agent"], d["decision"], d["key"]) == (
        AGENT, "interrupt", f"urgent-1:{AGENT}")
    cur = json.loads(t.store[RP + "cursor.json"])
    assert f"urgent-1:{AGENT}" in cur["processed"]       # cursor still advances


def test_router_shadow_arm_writes_marker_and_is_idempotent(capsys):
    t = FakeTransport()
    assert cli.cmd_router_shadow_arm(_args(min_hours=48), t) == 0
    marker = json.loads(t.store[RP + "shadow-window.json"])
    assert marker["started_at"] == NOW_ISO and marker["min_hours"] == 48
    capsys.readouterr()
    assert cli.cmd_router_shadow_arm(_args(min_hours=48), t) == 0  # re-arm
    assert "already armed" in capsys.readouterr().out
    assert json.loads(t.store[RP + "shadow-window.json"])["started_at"] == NOW_ISO


def test_router_shadow_status_reports_elapsed(capsys):
    t = FakeTransport()
    cli.cmd_router_shadow_arm(_args(), t)
    capsys.readouterr()
    assert cli.cmd_router_shadow_status(_args(), t) == 0
    assert "ARMED" in capsys.readouterr().out


def _listen_state():
    return {"inbox_ids": set(), "response_keys": set(),
            "verdict_keys": set(), "degraded": {}}


def test_listener_probe_records_evidence_when_window_armed(monkeypatch):
    """W7: a DIRECTIVE surfaced by a listen tick, while a window is armed, writes
    a listener-path evidence shard keyed by (agent, source-shard:agent)."""
    t = FakeTransport()
    t.put(RP + "shadow-window.json",
          json.dumps({"started_at": NOW_ISO, "min_hours": 48}))
    ev = {"type": "directive", "slug": "urgent-1", "owner": "boss", "title": "x"}
    monkeypatch.setattr(cli, "_listen_tick", lambda *a, **k: ([ev], {}))
    cli._run_listen_tick(t, TEAM, AGENT, _listen_state(),
                         json_mode=False, verbose=False)
    shards = _shadow_evidence(t)
    assert len(shards) == 1
    (s,) = shards.values()
    assert (s["path"], s["agent"], s["key"]) == (
        "listener", AGENT, f"urgent-1:{AGENT}")


def test_listener_probe_silent_when_window_not_armed(monkeypatch):
    t = FakeTransport()                                  # no marker
    ev = {"type": "directive", "slug": "urgent-1", "owner": "boss", "title": "x"}
    monkeypatch.setattr(cli, "_listen_tick", lambda *a, **k: ([ev], {}))
    cli._run_listen_tick(t, TEAM, AGENT, _listen_state(),
                         json_mode=False, verbose=False)
    assert _shadow_evidence(t) == {}


def test_adapter_probe_records_evidence_on_delivery_when_armed():
    """codex #470 P1: a genuine cloud-adapter delivery, while a window is armed,
    writes path=adapter evidence."""
    t = FakeTransport()
    _base(t)
    t.put(RP + "shadow-window.json",
          json.dumps({"started_at": NOW_ISO, "min_hours": 48}))
    _seed_queue(t, _q_entry(source="s-1"))
    cli._router_execute_cloud(_args(), t, invoke=_invoke("delivered"))
    ev = _shadow_evidence(t)
    assert len(ev) == 1
    (s,) = ev.values()
    assert (s["path"], s["key"]) == ("adapter", f"s-1:{AGENT}")


def test_adapter_probe_silent_when_window_not_armed():
    t = FakeTransport()
    _base(t)
    _seed_queue(t, _q_entry(source="s-1"))
    cli._router_execute_cloud(_args(), t, invoke=_invoke("delivered"))
    assert _shadow_evidence(t) == {}


def test_shadow_arm_rejects_sub_48_hour_windows(capsys):
    """codex #470 P1: the normative window is >=48h — 47/0/negative are refused,
    48 is the accepted boundary."""
    t = FakeTransport()
    for bad in (47, 0, -1):
        assert cli.cmd_router_shadow_arm(_args(min_hours=bad), t) == 1
    assert RP + "shadow-window.json" not in t.store       # nothing armed
    assert cli.cmd_router_shadow_arm(_args(min_hours=48), t) == 0
    assert json.loads(t.store[RP + "shadow-window.json"])["min_hours"] == 48


def test_malformed_started_at_does_not_activate_probes(monkeypatch):
    """codex #470 P2: a marker with an unparseable started_at is doubt ⇒ off."""
    t = FakeTransport()
    t.put(RP + "shadow-window.json", json.dumps({"started_at": "bogus"}))
    ev = {"type": "directive", "slug": "urgent-1", "owner": "b", "title": "x"}
    monkeypatch.setattr(cli, "_listen_tick", lambda *a, **k: ([ev], {}))
    cli._run_listen_tick(t, TEAM, AGENT, _listen_state(),
                         json_mode=False, verbose=False)
    assert _shadow_evidence(t) == {}
    assert cli.cmd_router_shadow_status(_args(), t) == 1   # status agrees: invalid


# --- W7: acceptance-report fold ---------------------------------------------

WS, WE = "2026-07-23T00:00:00Z", "2026-07-25T00:00:00Z"


def _dec(key, decision="interrupt", decided_at="2026-07-23T12:01:00Z"):
    return {"key": key, "agent": key.split(":", 1)[1], "decision": decision,
            "reason": "r", "priority": "P1", "decided_at": decided_at}


def _ev(key, delivered_at="2026-07-23T12:00:00Z"):
    return {"key": key, "agent": key.split(":", 1)[1],
            "delivered_at": delivered_at, "path": "listener"}


def _marks(step_s=60, hours=48):
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc)
    n = int(hours * 3600 / step_s)
    return [router.iso(t0 + timedelta(seconds=i * step_s)) for i in range(n + 1)]


def test_shadow_report_matched_lagged_policy_divergent_missed():
    k1, k2, k3, k4 = (f"s{i}:{AGENT}" for i in range(1, 5))
    rep = router.shadow_report(
        [_dec(k1, "interrupt", "2026-07-23T12:02:00Z"),      # +2m  -> matched
         _dec(k2, "interrupt", "2026-07-23T12:30:00Z"),      # +30m -> lagged
         _dec(k3, "batch")],                                  # delivered -> divergent
        [_ev(k1), _ev(k2), _ev(k3), _ev(k4)],                # k4: no decision -> missed
        window_start=WS, window_end=WE)
    assert rep["classes"] == {k1: "matched", k2: "lagged",
                              k3: "policy-divergent", k4: "missed"}
    assert rep["gates"]["missed_zero"] is False
    assert rep["gates"]["lagged_zero"] is False
    assert rep["pass"] is False


def test_shadow_report_phantom_only_with_store_keys():
    k = f"ghost:{AGENT}"
    # without store_keys: unverifiable -> no-probe-evidence, not phantom
    rep = router.shadow_report([_dec(k)], [], window_start=WS, window_end=WE)
    assert rep["classes"][k] == "no-probe-evidence"
    # with store_keys and the item absent -> phantom (zero-tolerance)
    rep = router.shadow_report([_dec(k)], [], store_keys=set(),
                               window_start=WS, window_end=WE)
    assert rep["classes"][k] == "phantom"
    assert rep["gates"]["phantom_zero"] is False and rep["pass"] is False
    # present in the store -> not phantom
    rep = router.shadow_report([_dec(k)], [], store_keys={k},
                               window_start=WS, window_end=WE)
    assert rep["classes"][k] == "no-probe-evidence"


def test_shadow_report_duty_cycle_unknown_fails_closed():
    k = f"s1:{AGENT}"
    rep = router.shadow_report(
        [_dec(k, "interrupt", "2026-07-23T12:02:00Z")], [_ev(k)],
        window_start=WS, window_end=WE)          # no pass_marks
    assert rep["classes"][k] == "matched"
    assert rep["duty_cycle"]["known"] is False
    assert rep["gates"]["duty_uptime"] is False   # unknown never passes
    assert rep["pass"] is False


def test_shadow_report_passes_with_healthy_window():
    k = f"s1:{AGENT}"
    rep = router.shadow_report(
        [_dec(k, "interrupt", "2026-07-23T12:02:00Z")], [_ev(k)],
        store_keys={k}, pass_marks=_marks(),
        window_start=WS, window_end=WE)
    assert rep["duty_cycle"]["known"] is True
    assert rep["duty_cycle"]["uptime"] >= 0.95
    assert rep["duty_cycle"]["max_gap_s"] <= 90 * 60
    assert rep["gates"] == {g: True for g in rep["gates"]}
    assert rep["pass"] is True


def test_shadow_report_duty_gate_fails_on_a_two_hour_gap():
    from datetime import datetime, timedelta, timezone
    t0 = datetime(2026, 7, 23, 0, 0, tzinfo=timezone.utc)
    marks = _marks(step_s=60, hours=24)          # healthy first day...
    late_start = t0 + timedelta(hours=26)        # ...then a 2h outage
    marks += [router.iso(late_start + timedelta(seconds=i * 60))
              for i in range(22 * 60)]
    k = f"s1:{AGENT}"
    rep = router.shadow_report(
        [_dec(k, "interrupt", "2026-07-23T12:02:00Z")], [_ev(k)],
        store_keys={k}, pass_marks=marks, window_start=WS, window_end=WE)
    assert rep["duty_cycle"]["max_gap_s"] >= 2 * 3600
    assert rep["gates"]["duty_max_gap"] is False
    assert rep["pass"] is False


def test_shadow_report_p95_bound():
    # 20 interrupts: 19 fast (30s), 1 slow-but-within-window (300s) -> p95 over bound(180s)? p95 index = int(.95*20)=19 -> the 300s one
    decs, evs = [], []
    for i in range(20):
        k = f"m{i}:{AGENT}"
        lat = 30 if i < 19 else 300
        decs.append(_dec(k, "interrupt",
                         router.iso(router.parse_iso("2026-07-23T12:00:00Z")
                                    + __import__('datetime').timedelta(seconds=lat))))
        evs.append(_ev(k))
    rep = router.shadow_report(decs, evs, store_keys={d['key'] for d in decs},
                               pass_marks=_marks(), window_start=WS, window_end=WE)
    assert all(c == "matched" for c in rep["classes"].values())  # 300s < 360s window
    assert rep["p95_interrupt_latency_s"] == 300
    assert rep["gates"]["p95_within_bound"] is False              # 300 > 180
    assert rep["pass"] is False


# --- E3: router feed-first candidate source (addendum §3.3) -----------------

def _uploaded(name, at):
    return {"path": TASKP + name, "state": "uploaded", "uploaded_at": at}


def test_router_feed_first_bypasses_a_failed_task_listing():
    """E3: with the feed available, candidates come from it — a failing
    task-directory LISTING no longer degrades the pass (feed is the source)."""
    t = FeedTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    t.set_feed([_uploaded("urgent-1.md", "2026-07-23T11:30:05Z")])
    t.fail_list_containing.add("task/")          # listing would fail — feed bypasses it
    assert cli.cmd_router_run(_args(), t) == 0
    q = _queue_entries(t)
    assert len(q) == 1
    (entry,) = q.values()
    assert entry["source_shard"] == "urgent-1"


def test_router_feed_wins_over_listing_divergence():
    """E3: the feed is authoritative — a shard present in the listing but NOT
    reported by the feed is not a candidate this pass."""
    t = FeedTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(TASKP + "listing-only.md", _task("listing-only", AGENT, "P1"),
          mtime="2026-07-23 11:40AM UTC")
    _base(t)
    t.set_feed([_uploaded("urgent-1.md", "2026-07-23T11:30:05Z")])  # not listing-only
    assert cli.cmd_router_run(_args(), t) == 0
    q = _queue_entries(t)
    assert len(q) == 1
    (entry,) = q.values()
    assert entry["source_shard"] == "urgent-1"   # NOT listing-only


def test_router_falls_back_to_listing_when_feed_unavailable():
    """E3: feed doubt (updates returns None) ⇒ the full task-listing scan, the
    unchanged W4 source — same enqueue outcome."""
    t = FeedTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    t.set_feed(None)                              # feed unavailable -> listing
    assert cli.cmd_router_run(_args(), t) == 0
    assert len(_queue_entries(t)) == 1


def test_router_malformed_feed_timestamp_is_doubt_not_skip():
    """codex + Tycho E3 blocking (addendum principle 2): an unparseable
    uploaded_at on a task upload is feed DOUBT, never a silent skip — dropping
    it while other candidates advance the watermark loses that wake forever. The
    pass abandons the partial feed and takes the healthy listing, surfacing BOTH
    shards; nothing is ledgered from the doubtful feed read."""
    t = FeedTransport()
    t.put(TASKP + "good-1.md", _task("good-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(TASKP + "bad-1.md", _task("bad-1", AGENT, "P1"),
          mtime="2026-07-23 11:31AM UTC")
    _base(t)
    t.set_feed([
        _uploaded("good-1.md", "2026-07-23T11:30:05Z"),
        _uploaded("bad-1.md", "not-a-timestamp"),      # malformed ⇒ feed doubt
    ])
    assert cli.cmd_router_run(_args(), t) == 0
    cur = json.loads(t.store[RP + "cursor.json"])
    # BOTH shards surfaced via the listing fallback and were ledgered — neither
    # lost to the doubtful feed. (bad-1 coalesces into good-1's wake by debounce
    # for the same agent, but it IS ledgered, not dropped. Under the buggy skip
    # the feed advances the watermark past bad-1 and it never enters `processed`.)
    assert f"good-1:{AGENT}" in cur["processed"]
    assert f"bad-1:{AGENT}" in cur["processed"]        # the at-risk wake is NOT lost
    assert len(_queue_entries(t)) >= 1                 # good-1 enqueued
    # watermark came from the LISTING (bad-1 mtime 11:31), NOT the partial feed
    # (good-1 ts 11:30:05) — the doubtful feed advanced nothing independently.
    assert cur["watermark"] == "2026-07-23T11:31:00Z"


def test_router_feed_second_granularity_advances_watermark():
    """E3: the watermark advances to the feed's second-granular uploaded_at
    (subsuming the minute tie), and the processed ledger records the key."""
    t = FeedTransport()
    t.put(TASKP + "urgent-1.md", _task("urgent-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    _base(t)
    t.set_feed([_uploaded("urgent-1.md", "2026-07-23T11:30:05Z")])
    assert cli.cmd_router_run(_args(), t) == 0
    cur = json.loads(t.store[RP + "cursor.json"])
    assert cur["watermark"] == "2026-07-23T11:30:05Z"      # second-granular
    assert f"urgent-1:{AGENT}" in cur["processed"]


# --- W5: cloud-execution loop (claim → invoke → delivered/dead-letter) -------

def _seed_queue(t, entry):
    key = router.idempotency_key(entry["source_shard"], entry["agent"])
    name = router.queue_filename(entry["agent"], key)
    t.put(RP + "queue/" + name, json.dumps(entry))
    return RP + "queue/" + name


def _shards_under(t, sub):
    return {p: json.loads(c) for p, c in t.store.items()
            if p.startswith(RP + sub)}


def _invoke(status, detail=""):
    return lambda inv: (status, detail)


def test_execute_delivers_cloud_entry_writes_record_and_clears_queue():
    t = FakeTransport()
    _base(t)
    qpath = _seed_queue(t, _q_entry(source="s-1"))
    counts = cli._router_execute_cloud(_args(), t, invoke=_invoke("delivered"))
    assert counts["delivered"] == 1
    assert qpath not in t.store                          # queue entry cleared
    recs = list(_shards_under(t, "delivered/").values())
    assert len(recs) == 1 and recs[0]["agent"] == AGENT
    assert router.fold_delivered(recs)[AGENT]["last_delivered_at"] == NOW_ISO


def test_execute_leaves_host_local_entries_for_w55():
    t = FakeTransport()
    _base(t)
    qpath = _seed_queue(t, _q_entry(adapter="codex-exec-resume",
                                    executor="mac-mini-1", source="s-2"))
    counts = cli._router_execute_cloud(_args(), t, invoke=_invoke("delivered"))
    assert counts["delivered"] == 0
    assert qpath in t.store                              # untouched — W5.5's
    assert _shards_under(t, "delivered/") == {}


def test_execute_defers_entry_until_not_before():
    t = FakeTransport()
    _base(t)
    qpath = _seed_queue(t, _q_entry(source="s-3",
                                    not_before="2026-07-23T13:00:00Z"))  # future
    counts = cli._router_execute_cloud(_args(), t, invoke=_invoke("delivered"))
    assert counts["deferred"] == 1 and counts["delivered"] == 0
    assert qpath in t.store and _shards_under(t, "delivered/") == {}


def test_execute_bounded_retry_then_dead_letter():
    t = FakeTransport()
    _base(t)
    qpath = _seed_queue(t, _q_entry(source="s-4"))
    fail = _invoke("failed", "adapter boom")
    # attempts 1,2 retry (entry stays); attempt 3 == MAX → dead-letter + clear
    for expect_attempts in (1, 2):
        counts = cli._router_execute_cloud(_args(), t, invoke=fail)
        assert counts["retried"] == 1
        assert json.loads(t.store[qpath])["attempts"] == expect_attempts
    counts = cli._router_execute_cloud(_args(), t, invoke=fail)
    assert counts["dead_lettered"] == 1
    assert qpath not in t.store
    dl = list(_shards_under(t, "dead-letter/").values())
    assert len(dl) == 1
    assert dl[0]["attempts"] == router.MAX_DELIVERY_ATTEMPTS
    assert dl[0]["last_error"] == "adapter boom"


def test_execute_respects_a_fresh_foreign_claim():
    t = FakeTransport()
    _base(t)
    _seed_queue(t, _q_entry(source="s-5", claimed_by="other-plane",
                            claimed_at="2026-07-23T11:55:00Z"))  # 5 min, fresh
    counts = cli._router_execute_cloud(_args(), t, invoke=_invoke("delivered"))
    assert counts["skipped"] == 1 and counts["delivered"] == 0


def test_execute_default_invoker_leaves_wake_visibly_queued():
    """No host-side client wired ⇒ `unconfigured`: the wake stays queued (never
    dropped, never a burned retry) — the plan's fail-visible degradation."""
    t = FakeTransport()
    _base(t)
    qpath = _seed_queue(t, _q_entry(source="s-6"))
    counts = cli._router_execute_cloud(_args(), t)      # default invoker
    assert counts["unconfigured"] == 1
    assert qpath in t.store and json.loads(t.store[qpath]).get("attempts") is None
    assert _shards_under(t, "delivered/") == {} and _shards_under(t, "dead-letter/") == {}


def test_execute_delivery_record_is_single_writer_per_key():
    t = FakeTransport()
    _base(t)
    _seed_queue(t, _q_entry(source="s-7"))
    cli._router_execute_cloud(_args(), t, invoke=_invoke("delivered"))
    _seed_queue(t, _q_entry(source="s-7"))              # same key, re-delivered
    cli._router_execute_cloud(_args(), t, invoke=_invoke("delivered"))
    assert len(_shards_under(t, "delivered/")) == 1     # self-overwrite, one shard


def test_execute_failed_record_write_keeps_entry_queued(capsys):
    """Same class as the W4 P1: a failed delivered-record write must NOT be
    followed by the queue delete — the wake stays queued to retry, never lost."""
    t = FlakyTransport()
    _base(t)
    qpath = _seed_queue(t, _q_entry(source="s-9"))
    t.fail_write_containing.add("delivered/")
    counts = cli._router_execute_cloud(_args(), t, invoke=_invoke("delivered"))
    assert counts["delivered"] == 0
    assert qpath in t.store                              # NOT lost
    assert _shards_under(t, "delivered/") == {}
    assert "record write failed" in capsys.readouterr().err
