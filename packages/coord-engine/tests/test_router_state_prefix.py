"""Router state-prefix override — parallel live delivery + shadow measurement.

`router run --shadow` (W7 measurement) and `router run --once` (live delivery)
both compose their cursor-tracked state under `router.router_prefix(team)` =
`team/<team>/_coord/router/`. Run on one host on the same cursor they collide:
whichever pass marks a directed item `processed` first starves the other — a
shadow-first pass silently DROPS the live wake, a live-first pass leaves the
measurement blind. The `--state-prefix <name>` override (env
`COORD_ROUTER_STATE_PREFIX` as the launchd-friendly fallback) relocates the
router's OWN state to the sibling `team/<team>/_coord/router-<name>/` so the two
passes never share a cursor.

Pinned here:
- DEFAULT byte-identical: no flag + no env ⇒ exactly `team/<team>/_coord/router/`.
- override ⇒ cursor/decisions/marks/queue land under `router-<name>/` (sibling).
- config.json is SHARED (canonical) — a namespaced run reads the same enablement
  policy the live router uses.
- NO-COLLISION: an interleaved live `--once` (canonical) and shadow
  `--once --state-prefix shadow` over the SAME directed item — the live pass
  enqueues it AND the shadow pass records a decision; neither starves the other.
- flag beats env; a bad-charset name ⇒ rc 2, never an escape from the namespace.
"""

import argparse
import json
from datetime import datetime, timezone

import pytest

from coord_engine import cli, okf, router, tasks
from coord_engine_test_helpers import FakeTransport

TEAM = "t"
RP = f"team/{TEAM}/_coord/router/"                       # canonical
RP_SHADOW = f"team/{TEAM}/_coord/router-shadow/"         # override sibling
TASKP = f"team/{TEAM}/task/"

PINNED_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_clock(monkeypatch):
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("COORD_ROUTER_STATE_PREFIX", raising=False)


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


AGENT = "worker-a"
CLOUD_CFG = {"priority_floor": "P2", "debounce_min": 15,
             "adapter": "managed-agents-message",
             "adapter_args": {"session_ref": "s-1"}}


def _config():
    return json.dumps({AGENT: dict(CLOUD_CFG)})


def _cursor(watermark, processed=None):
    return json.dumps({"watermark": watermark, "processed": processed or {}})


def _paths(t, prefix, sub):
    return {p: c for p, c in t.store.items() if p.startswith(prefix + sub)}


# --- the resolver seam ------------------------------------------------------

def test_default_prefix_is_byte_identical():
    # No override: exactly today's path, and state=None is the same call.
    assert router.router_prefix(TEAM) == "team/t/_coord/router/"
    assert router.router_prefix(TEAM, state=None) == "team/t/_coord/router/"


def test_override_resolves_to_sibling_dir():
    assert router.router_prefix(TEAM, state="shadow") == "team/t/_coord/router-shadow/"
    # sibling of the canonical dir, same team — directed-item paths untouched
    assert router.router_prefix(TEAM, state="shadow").rsplit("/", 2)[0] + "/" \
        == router.router_prefix(TEAM).rsplit("/", 2)[0] + "/"


@pytest.mark.parametrize("bad", ["../evil", "a/b", "with space", "", "x\ty"])
def test_bad_charset_name_is_rejected(bad):
    with pytest.raises(ValueError):
        router.router_prefix(TEAM, state=bad)


# --- override moves the router's own state ----------------------------------

def test_shadow_override_writes_under_sibling_not_canonical():
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "config.json", _config())
    t.put(RP_SHADOW + "cursor.json", _cursor("2026-07-23T11:00:00Z"))

    assert cli.cmd_router_run(
        _args(shadow=True, state_prefix="shadow"), t) == 0

    # decision + cursor + mark landed under the SIBLING, none under canonical
    assert _paths(t, RP_SHADOW, "shadow-decisions/")
    assert RP_SHADOW + "cursor.json" in t.store
    assert _paths(t, RP_SHADOW, "shadow-marks/")
    assert not _paths(t, RP, "shadow-decisions/")
    assert RP + "cursor.json" not in t.store


def test_config_is_shared_canonical_under_override():
    # config lives ONLY at the canonical prefix; a namespaced run must still read
    # it (enablement is one fleet policy), so the item is decided as configured
    # (interrupt), not routed observe/unroutable for a "missing" config.
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "config.json", _config())                 # canonical only
    t.put(RP_SHADOW + "cursor.json", _cursor("2026-07-23T11:00:00Z"))

    assert cli.cmd_router_run(
        _args(shadow=True, state_prefix="shadow"), t) == 0

    decisions = list(_paths(t, RP_SHADOW, "shadow-decisions/").values())
    assert len(decisions) == 1
    rec = json.loads(decisions[0])
    assert rec["agent"] == AGENT and rec["decision"] == "interrupt"


def test_shadow_pass_reads_canonical_delivery_recency():
    """Blocking (c): a namespaced SHADOW pass reads the delivered view from the
    CANONICAL prefix. The live plane delivered to this agent recently (canonical
    delivered/); the shadow plane never delivers, so its own namespaced delivered/
    is empty by construction. A shadow `--state-prefix` pass over a fresh directed
    item for the same agent must therefore DEBOUNCE — honoring canonical delivery
    recency — not classify `interrupt` off an eternally-empty namespaced history
    (which would inflate policy-divergent forever). It also writes NO delivered.json
    refold (a shadow pass maintains no view it will ever reuse)."""
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "config.json", _config())
    t.put(RP_SHADOW + "cursor.json", _cursor("2026-07-23T11:00:00Z"))
    # LIVE delivery recorded at the CANONICAL delivered/ prefix, 5 min before now
    # — well inside the 15-min debounce window (CLOUD_CFG debounce_min=15).
    prior = {"agent": AGENT, "source_shard": "item-0",
             "adapter": CLOUD_CFG["adapter"], "executor": "decision-plane",
             "delivered_at": "2026-07-23T11:55:00Z"}
    t.put(RP + "delivered/" + router.record_filename(
        router.idempotency_key("item-0", AGENT)), json.dumps(prior))

    assert cli.cmd_router_run(
        _args(shadow=True, state_prefix="shadow"), t) == 0

    decisions = list(_paths(t, RP_SHADOW, "shadow-decisions/").values())
    assert len(decisions) == 1
    rec = json.loads(decisions[0])
    assert rec["decision"] == "debounce", (
        "shadow pass must honor canonical delivery recency, not interrupt")
    # a shadow-under-override pass persists no delivered.json (skips the refold)
    assert RP_SHADOW + "delivered.json" not in t.store


# --- the no-collision property (the deliverable) ----------------------------

def test_live_and_shadow_do_not_starve_each_other():
    """Interleave a live `--once` (canonical) and a shadow `--once
    --state-prefix shadow` over the SAME directed item: the live pass must
    enqueue it AND the shadow pass must record a decision. On the shared cursor
    (pre-fix) whichever runs first marks the key processed and the other skips
    it — this assertion is what fails then."""
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "config.json", _config())
    t.put(RP + "cursor.json", _cursor("2026-07-23T11:00:00Z"))
    t.put(RP_SHADOW + "cursor.json", _cursor("2026-07-23T11:00:00Z"))

    # live first, then shadow — order must not matter
    assert cli.cmd_router_run(_args(), t) == 0
    assert cli.cmd_router_run(
        _args(shadow=True, state_prefix="shadow"), t) == 0

    live_queue = _paths(t, RP, "queue/")
    shadow_decisions = _paths(t, RP_SHADOW, "shadow-decisions/")
    assert len(live_queue) == 1, "live pass must enqueue the wake"
    assert len(shadow_decisions) == 1, "shadow pass must record a decision"
    # shadow enqueued NOTHING (read-only), live wrote NO decisions
    assert not _paths(t, RP_SHADOW, "queue/")
    assert not _paths(t, RP, "shadow-decisions/")


def test_no_collision_holds_shadow_first():
    # the symmetric order — shadow first, then live
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "config.json", _config())
    t.put(RP + "cursor.json", _cursor("2026-07-23T11:00:00Z"))
    t.put(RP_SHADOW + "cursor.json", _cursor("2026-07-23T11:00:00Z"))

    assert cli.cmd_router_run(
        _args(shadow=True, state_prefix="shadow"), t) == 0
    assert cli.cmd_router_run(_args(), t) == 0

    assert len(_paths(t, RP, "queue/")) == 1
    assert len(_paths(t, RP_SHADOW, "shadow-decisions/")) == 1


# --- override source precedence + validation --------------------------------

def test_env_fallback_moves_state(monkeypatch):
    monkeypatch.setenv("COORD_ROUTER_STATE_PREFIX", "shadow")
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "config.json", _config())
    t.put(RP_SHADOW + "cursor.json", _cursor("2026-07-23T11:00:00Z"))

    assert cli.cmd_router_run(_args(shadow=True), t) == 0
    assert _paths(t, RP_SHADOW, "shadow-decisions/")


def test_flag_beats_env(monkeypatch):
    monkeypatch.setenv("COORD_ROUTER_STATE_PREFIX", "fromenv")
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "config.json", _config())
    t.put(RP_SHADOW + "cursor.json", _cursor("2026-07-23T11:00:00Z"))

    assert cli.cmd_router_run(
        _args(shadow=True, state_prefix="shadow"), t) == 0
    # flag "shadow" wins; the env "fromenv" namespace is never touched
    assert _paths(t, RP_SHADOW, "shadow-decisions/")
    assert not _paths(t, f"team/{TEAM}/_coord/router-fromenv/", "shadow-decisions/")


def test_default_run_is_untouched_by_absent_override():
    # regression pin: no flag, no env ⇒ everything at the canonical prefix
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "config.json", _config())
    t.put(RP + "cursor.json", _cursor("2026-07-23T11:00:00Z"))

    assert cli.cmd_router_run(_args(), t) == 0
    assert len(_paths(t, RP, "queue/")) == 1
    # nothing leaked into any sibling
    assert not [p for p in t.store if "/_coord/router-" in p]


def test_bad_flag_charset_rc2(capsys):
    t = FakeTransport()
    assert cli.cmd_router_run(_args(state_prefix="../escape"), t) == 2
    assert "state" in capsys.readouterr().err.lower()


def test_bad_env_charset_rc2(monkeypatch, capsys):
    monkeypatch.setenv("COORD_ROUTER_STATE_PREFIX", "a/b")
    t = FakeTransport()
    assert cli.cmd_router_run(_args(), t) == 2
    assert "COORD_ROUTER_STATE_PREFIX" in capsys.readouterr().err


def test_execute_honors_override():
    # a namespaced live run enqueues to the sibling queue; a namespaced execute
    # must drain THAT queue (round-trip consistency for the run/execute pair).
    t = FakeTransport()
    t.put(TASKP + "item-1.md", _task("item-1", AGENT, "P1"),
          mtime="2026-07-23 11:30AM UTC")
    t.put(RP + "config.json", _config())
    t.put(RP_SHADOW + "cursor.json", _cursor("2026-07-23T11:00:00Z"))

    # namespaced LIVE run (not shadow): enqueues a cloud wake under the sibling
    assert cli.cmd_router_run(_args(state_prefix="shadow"), t) == 0
    assert len(_paths(t, RP_SHADOW, "queue/")) == 1

    delivered = {}

    def _invoke(inv):
        delivered["hit"] = True
        return ("delivered", "ok")

    counts = cli._router_execute_cloud(
        _args(state_prefix="shadow"), t, invoke=_invoke)
    assert counts["delivered"] == 1
    assert delivered.get("hit")
    # the wake left the sibling queue, a delivered record landed there
    assert not _paths(t, RP_SHADOW, "queue/")
    assert _paths(t, RP_SHADOW, "delivered/")
    # canonical queue was never touched
    assert not _paths(t, RP, "queue/")
