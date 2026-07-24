"""Tests for the W5.5 thin host executor — `coord-engine router execute <team>`.

Normative contract: docs/coord/wake-router-PLAN.md W5.5 row + §2 delivery
guarantee. W5.5 is a POLICY-FREE, config-authority-free poller: it fires the
sanctioned host-local adapters for queue entries the decision plane (W4) already
resolved to THIS host's executor id, and nothing else. The load-bearing pins:

- select only (its-executor-id AND not_before-passed) entries; another
  executor's entry is untouched;
- deliver → idempotency-keyed delivery-record shard the `fold_delivered` view
  reads; a delivery-record that already exists SKIPS (never re-invoke);
- §2 at-least-once CONTENT-SAFETY (acceptance): the same entry executed twice
  carries NO per-event command and converges to exactly one bus check;
- bounded retry → dead-letter on exhaustion (attempts/last_error/gave_up_at); a
  transient failure leaves the entry VISIBLY queued, never dropped;
- READ-CONTRACT: a queue/delivered listing that RAISES is UNKNOWN-degraded
  (loud, rc-nonzero, wakes stay queued), never a clean "0 delivered"; a per-entry
  read that is None/unparseable is SKIPPED (never invoke on an UNKNOWN entry);
- policy-free: an entry in the queue executes regardless of priority — the
  decision was already made by W4; W5.5 re-runs no policy;
- the default invoker wires no real adapter, so THIS component wakes nothing.

Every test uses a FAKE adapter invoker — no real wake.
"""

import argparse
import json
from datetime import datetime, timezone

import pytest

from coord_engine import cli, router
from coord_engine_test_helpers import FakeTransport
from coord_engine.transport import TransportError

TEAM = "t"
RP = f"team/{TEAM}/_coord/router/"
HOST = "mac-mini-1"
OTHER = "linux-box-2"
AGENT = "worker-a"

PINNED_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-07-23T12:00:00Z"


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


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
            raise TransportError("boom")
        return super().list_dir(prefix)


def _args(**kw):
    ns = argparse.Namespace(team=TEAM, host=HOST, once=True, dry_run=False,
                            json=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def _host_entry(*, agent=AGENT, adapter="codex-exec-resume", executor=HOST,
                source="item-1", priority="P1", **extra):
    e = {"agent": agent, "reason": "check your bus", "source_shard": source,
         "priority": priority, "queued_at": NOW_ISO, "not_before": NOW_ISO,
         "adapter": adapter, "executor": executor}
    e.update(extra)
    return e


def _seed(t, entry):
    key = router.idempotency_key(entry["source_shard"], entry["agent"])
    name = router.queue_filename(entry["agent"], key)
    path = RP + "queue/" + name
    t.put(path, json.dumps(entry))
    return path


def _config(agents, executors):
    doc = dict(agents)
    doc["executors"] = list(executors)
    return json.dumps(doc)


def _hbase(t, *, config=None):
    if config is None:
        config = _config(
            {AGENT: {"priority_floor": "P1", "debounce_min": 15,
                     "adapter": "codex-exec-resume",
                     "adapter_args": {"thread_id": "th-1"}, "executor": HOST}},
            [HOST])
    t.put(RP + "config.json", config)


def _shards_under(t, sub):
    return {p: json.loads(c) for p, c in t.store.items()
            if p.startswith(RP + sub)}


def _invoke(status, detail=""):
    return lambda inv: (status, detail)


class RecordingInvoker:
    """Fake host-local adapter invoker. Records each invocation payload and
    models the observable EFFECT of a keyed nudge: one bus check per
    idempotency key (N deliveries of the same key converge to one)."""

    def __init__(self, status="delivered", detail=""):
        self.status = status
        self.detail = detail
        self.invocations: list = []
        self.bus_checks: set = set()

    def __call__(self, inv):
        self.invocations.append(inv)
        self.bus_checks.add(inv["idempotency_key"])
        return (self.status, self.detail)


# --- select ------------------------------------------------------------------

def test_selects_only_its_own_executor_and_ready_entries():
    t = FlakyTransport()
    _hbase(t)
    mine = _seed(t, _host_entry(source="mine"))
    theirs = _seed(t, _host_entry(source="theirs", executor=OTHER))
    future = _seed(t, _host_entry(source="later",
                                  not_before="2026-07-23T13:00:00Z"))
    inv = RecordingInvoker("delivered")
    counts = cli._router_execute_host(_args(), t, invoke=inv)
    assert counts["delivered"] == 1
    assert counts["deferred"] == 1
    assert mine not in t.store                 # delivered → queue cleared
    assert theirs in t.store                   # another executor's — untouched
    assert future in t.store                   # not_before in the future
    # exactly the ready-and-ours entry was invoked
    assert [i["idempotency_key"] for i in inv.invocations] == [
        router.idempotency_key("mine", AGENT)]


# --- deliver -----------------------------------------------------------------

def test_delivers_writes_idempotency_keyed_record_and_fold_reflects_it():
    t = FlakyTransport()
    _hbase(t)
    _seed(t, _host_entry(source="s-1"))
    counts = cli._router_execute_host(_args(), t, invoke=_invoke("delivered"))
    assert counts["delivered"] == 1
    recs = list(_shards_under(t, "delivered/").values())
    assert len(recs) == 1
    assert recs[0]["key"] == router.idempotency_key("s-1", AGENT)
    assert recs[0]["executor"] == HOST
    view = router.fold_delivered(recs)
    assert view[AGENT]["last_delivered_at"] == NOW_ISO
    assert view[AGENT]["count"] == 1


# --- idempotency -------------------------------------------------------------

def test_existing_delivery_record_skips_no_reinvoke_no_write():
    t = FlakyTransport()
    _hbase(t)
    entry = _host_entry(source="s-2")
    _seed(t, entry)
    key = router.idempotency_key("s-2", AGENT)
    # a delivery-record already exists for this key
    t.put(RP + "delivered/" + router.record_filename(key),
          json.dumps(router.delivery_record(entry, NOW_ISO)))
    before = dict(t.store)
    inv = RecordingInvoker("delivered")
    counts = cli._router_execute_host(_args(), t, invoke=inv)
    assert counts["already_delivered"] == 1 and counts["delivered"] == 0
    assert inv.invocations == []               # never re-invoked
    assert t.store == before                   # no new write


# --- §2 at-least-once content-safety (ACCEPTANCE) ----------------------------

def test_at_least_once_is_content_safe_and_converges_to_one_bus_check():
    t = FlakyTransport()
    _hbase(t)
    _seed(t, _host_entry(source="s-1"))
    inv = RecordingInvoker("delivered")
    # crash-equivalent: the delivered/ record write fails, so the entry stays
    # queued and the SAME wake is executed again next pass (at-least-once).
    t.fail_write_containing.add("delivered/")
    c1 = cli._router_execute_host(_args(), t, invoke=inv)
    assert c1["delivered"] == 0 and c1["retried"] == 1   # record write failed → stays
    t.fail_write_containing.clear()
    c2 = cli._router_execute_host(_args(), t, invoke=inv)
    assert c2["delivered"] == 1
    # fired TWICE on the same entry — at-least-once
    assert len(inv.invocations) == 2
    # CONTENT-SAFETY: neither invocation carried a per-event command / mutation
    for payload in inv.invocations:
        for banned in ("command", "cmd", "exec", "run", "payload",
                       "session_patch", "permission_mode", "url"):
            assert banned not in payload
        assert "no action is encoded" in payload["message"].lower()
    # CONVERGES: N deliveries of the same keyed nudge = exactly ONE bus check
    assert len(inv.bus_checks) == 1
    # a THIRD execution after the delivery-record exists is a no-op (step 2)
    _seed(t, _host_entry(source="s-1"))        # re-appears (e.g. delete lagged)
    c3 = cli._router_execute_host(_args(), t, invoke=inv)
    assert c3["already_delivered"] == 1
    assert len(inv.invocations) == 2           # NOT re-invoked


# --- bounded retry / dead-letter ---------------------------------------------

def test_bounded_retry_then_dead_letter_transient_stays_queued():
    t = FlakyTransport()
    _hbase(t)
    qpath = _seed(t, _host_entry(source="s-4"))
    fail = _invoke("failed", "adapter boom")
    for expect_attempts in (1, 2):
        counts = cli._router_execute_host(_args(), t, invoke=fail)
        assert counts["retried"] == 1
        assert qpath in t.store                 # transient failure → stays queued
        stamped = json.loads(t.store[qpath])
        assert stamped["attempts"] == expect_attempts
        assert stamped["claimed_by"] == cli._claim_owner(HOST)  # id-match + stamp
    counts = cli._router_execute_host(_args(), t, invoke=fail)
    assert counts["dead_lettered"] == 1
    assert qpath not in t.store
    dl = list(_shards_under(t, "dead-letter/").values())
    assert len(dl) == 1
    assert dl[0]["attempts"] == router.MAX_DELIVERY_ATTEMPTS
    assert dl[0]["last_error"] == "adapter boom"
    assert dl[0]["gave_up_at"] == NOW_ISO


# --- read contract -----------------------------------------------------------

def test_queue_listing_raises_is_degraded_not_clean_zero(capsys):
    t = FlakyTransport()
    _hbase(t)
    _seed(t, _host_entry(source="s-5"))
    t.fail_list_containing.add("queue/")
    inv = RecordingInvoker("delivered")
    counts = cli._router_execute_host(_args(), t, invoke=inv)
    assert counts["degraded"] == 1
    assert inv.invocations == []                # nothing fired while blind
    assert _shards_under(t, "delivered/") == {}
    assert "degraded" in capsys.readouterr().err.lower()
    # command surfaces the degradation as a non-zero exit
    t.fail_list_containing.add("queue/")
    assert cli.cmd_router_execute(_args(), t) == 1


def test_delivered_listing_raises_is_degraded(capsys):
    t = FlakyTransport()
    _hbase(t)
    _seed(t, _host_entry(source="s-6"))
    t.fail_list_containing.add("delivered/")
    inv = RecordingInvoker("delivered")
    counts = cli._router_execute_host(_args(), t, invoke=inv)
    assert counts["degraded"] == 1
    assert inv.invocations == []               # cannot confirm idempotency → blind
    assert "degraded" in capsys.readouterr().err.lower()


def test_unparseable_entry_is_skipped_never_invoked():
    t = FlakyTransport()
    _hbase(t)
    t.put(RP + "queue/junk.json", "{not json")
    good = _seed(t, _host_entry(source="good"))
    inv = RecordingInvoker("delivered")
    counts = cli._router_execute_host(_args(), t, invoke=inv)
    assert counts["skipped"] == 1
    assert counts["delivered"] == 1            # the good one still executes
    assert [i["idempotency_key"] for i in inv.invocations] == [
        router.idempotency_key("good", AGENT)]
    assert good not in t.store


# --- dry-run -----------------------------------------------------------------

def test_dry_run_invokes_nothing_writes_nothing():
    t = FlakyTransport()
    _hbase(t)
    qpath = _seed(t, _host_entry(source="s-7"))
    before = dict(t.store)
    inv = RecordingInvoker("delivered")
    counts = cli._router_execute_host(_args(dry_run=True), t, invoke=inv)
    assert counts["would_execute"] == 1
    assert counts["delivered"] == 0
    assert inv.invocations == []
    assert t.store == before                   # nothing written/deleted
    assert qpath in t.store


# --- policy-free -------------------------------------------------------------

def test_policy_free_executes_regardless_of_priority():
    """W5.5 makes no wake decision — a below-floor P3 entry present in the queue
    executes anyway (W4 already decided). No policy is re-run here."""
    t = FlakyTransport()
    _hbase(t)                                   # config floor is P1
    _seed(t, _host_entry(source="s-8", priority="P3"))
    counts = cli._router_execute_host(_args(), t, invoke=_invoke("delivered"))
    assert counts["delivered"] == 1


def test_executes_with_no_config_present():
    """No config authority: with config.json absent the executor still fires
    (adapter_args just resolve empty) — it needs no policy config to run."""
    t = FlakyTransport()
    _seed(t, _host_entry(source="s-9"))        # no _hbase → no config.json
    counts = cli._router_execute_host(_args(), t, invoke=_invoke("delivered"))
    assert counts["delivered"] == 1


# --- adapter validity --------------------------------------------------------

def test_non_host_local_adapter_is_dead_lettered():
    """Only the four ADAPTERS_HOST_LOCAL are executable on a host executor; a
    cloud adapter mis-resolved to this host is a per-entry error → dead-letter."""
    t = FlakyTransport()
    _hbase(t)
    qpath = _seed(t, _host_entry(source="s-10", adapter="managed-agents-message"))
    inv = RecordingInvoker("delivered")
    counts = cli._router_execute_host(_args(), t, invoke=inv)
    assert counts["dead_lettered"] == 1
    assert inv.invocations == []               # never fired
    assert qpath not in t.store
    dl = list(_shards_under(t, "dead-letter/").values())
    assert len(dl) == 1 and "host-local" in dl[0]["last_error"]


def test_unknown_adapter_is_dead_lettered():
    t = FlakyTransport()
    _hbase(t)
    _seed(t, _host_entry(source="s-11", adapter="curl-the-internet"))
    counts = cli._router_execute_host(_args(), t, invoke=_invoke("delivered"))
    assert counts["dead_lettered"] == 1


# --- fail-visible seam -------------------------------------------------------

def test_default_invoker_leaves_wake_visibly_queued():
    """No real host-local adapter script is wired in this PR — the default
    invoker reports `unconfigured`, so the wake stays VISIBLY queued (never
    dropped, never a burned retry). Proves this component wakes nothing."""
    t = FlakyTransport()
    _hbase(t)
    qpath = _seed(t, _host_entry(source="s-12"))
    counts = cli._router_execute_host(_args(), t)   # default invoker
    assert counts["unconfigured"] == 1
    assert qpath in t.store
    assert json.loads(t.store[qpath]).get("attempts") is None
    assert _shards_under(t, "delivered/") == {}
    assert _shards_under(t, "dead-letter/") == {}


def test_respects_a_fresh_foreign_claim():
    t = FlakyTransport()
    _hbase(t)
    _seed(t, _host_entry(source="s-13", claimed_by="other-proc",
                         claimed_at="2026-07-23T11:55:00Z"))  # 5 min, fresh
    inv = RecordingInvoker("delivered")
    counts = cli._router_execute_host(_args(), t, invoke=inv)
    assert counts["skipped"] == 1 and counts["delivered"] == 0
    assert inv.invocations == []


def test_concurrent_sibling_process_observes_claim_and_does_not_invoke():
    """Claim-then-invoke BOUNDS duplicates: process A persists its claim BEFORE
    invoking; a second process B under the same host executor id then reads the
    entry, sees the fresh claim as foreign (distinct process identity), and does
    NOT invoke the adapter — so the side-effect window is never unclaimed."""
    t = FlakyTransport()
    _hbase(t)
    _seed(t, _host_entry(source="s-c"))
    qpath = RP + "queue/" + router.queue_filename(
        AGENT, router.idempotency_key("s-c", AGENT))
    inv_a = RecordingInvoker("unconfigured")   # A claims + invokes, leaves queued
    inv_b = RecordingInvoker("delivered")
    ca = cli._router_execute_host(_args(), t, invoke=inv_a, claim_owner="HOST#procA")
    assert len(inv_a.invocations) == 1         # A claimed, then invoked
    stamped = json.loads(t.store[qpath])
    assert stamped["claimed_by"] == "HOST#procA"   # claim persisted BEFORE invoke
    assert ca["unconfigured"] == 1
    cb = cli._router_execute_host(_args(), t, invoke=inv_b, claim_owner="HOST#procB")
    assert inv_b.invocations == []             # B saw the fresh claim → did NOT invoke
    assert cb["skipped"] == 1


def test_claim_write_failure_blocks_invoke_and_leaves_entry_queued(capsys):
    """No wake without a persisted claim: if the pre-invoke claim stamp does not
    land, the adapter is NOT invoked, the entry stays VISIBLY queued for the next
    tick, and the failure is loud — never a silent skip and never a wake."""
    t = FlakyTransport()
    _hbase(t)
    qpath = _seed(t, _host_entry(source="s-cf"))
    original = t.store[qpath]
    t.fail_write_containing.add("queue/")       # the claim stamp write fails
    inv = RecordingInvoker("delivered")
    counts = cli._router_execute_host(_args(), t, invoke=inv)
    assert inv.invocations == []                # NEVER invoked without a claim
    assert counts["claim_unpersisted"] == 1
    assert counts["delivered"] == 0
    assert qpath in t.store and t.store[qpath] == original   # unchanged, still queued
    assert _shards_under(t, "delivered/") == {}
    assert "claim" in capsys.readouterr().err.lower()


def test_failed_delivery_record_write_keeps_entry_queued(capsys):
    t = FlakyTransport()
    _hbase(t)
    qpath = _seed(t, _host_entry(source="s-14"))
    t.fail_write_containing.add("delivered/")
    counts = cli._router_execute_host(_args(), t, invoke=_invoke("delivered"))
    assert counts["delivered"] == 0
    assert qpath in t.store                     # NOT lost
    assert _shards_under(t, "delivered/") == {}
    assert "record write failed" in capsys.readouterr().err


def test_default_host_id_used_when_flag_absent(monkeypatch):
    t = FlakyTransport()
    _hbase(t)
    monkeypatch.setattr(cli, "_host", lambda: HOST)
    _seed(t, _host_entry(source="s-15"))
    counts = cli._router_execute_host(_args(host=None), t,
                                      invoke=_invoke("delivered"))
    assert counts["delivered"] == 1


def test_command_once_returns_zero_on_clean_pass():
    t = FlakyTransport()
    _hbase(t)
    _seed(t, _host_entry(source="s-16"))
    # default invoker → unconfigured (wakes nothing) but a clean pass = rc 0
    assert cli.cmd_router_execute(_args(), t) == 0
