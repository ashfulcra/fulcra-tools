# packages/coord-engine/tests/test_continuity_audit.py
from datetime import datetime, timedelta, timezone
from coord_engine.continuity_audit import stale_agents

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)

# clock-pin support (see #378):
import pytest
PINNED_NOW = datetime(2026, 7, 7, 12, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_module_clock(monkeypatch):
    """Pin cli._now to PINNED_NOW (just after the module NOW).

    Fixtures stamp data relative to NOW, but folds/verbs compute windows and
    staleness off cli._now() against the REAL clock — so once wall-clock time
    crossed NOW + a window this suite flipped RED for good (the repo's
    date-boundary CI-flake class; template: #378 test_threads). Remedy: pin the
    clock, never weaken assertions. Tests that MOVE time monkeypatch cli._now
    themselves, overriding this."""
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW)


def _p(agent, hours_ago):
    return {"agent": agent, "ts": NOW - timedelta(hours=hours_ago)}

def test_fresh_presence_no_snapshot_is_stale():
    out = stale_agents(presence=[_p("a", 1)], snapshots=[], now=NOW)
    assert [r["agent"] for r in out] == ["a"]
    assert out[0]["snapshot_age_h"] is None

def test_fresh_presence_old_snapshot_is_stale():
    out = stale_agents(presence=[_p("a", 1)], snapshots=[_p("a", 30)], now=NOW)
    assert [r["agent"] for r in out] == ["a"]
    assert out[0]["snapshot_age_h"] == 30.0

def test_fresh_presence_fresh_snapshot_is_clean():
    assert stale_agents(presence=[_p("a", 1)], snapshots=[_p("a", 2)], now=NOW) == []

def test_stale_presence_is_ignored_not_flagged():
    # a dead agent is a presence problem, not a continuity problem
    assert stale_agents(presence=[_p("a", 48)], snapshots=[], now=NOW) == []

def test_latest_snapshot_wins():
    out = stale_agents(presence=[_p("a", 1)], snapshots=[_p("a", 40), _p("a", 3)], now=NOW)
    assert out == []

def test_thresholds_are_parameters():
    out = stale_agents(presence=[_p("a", 1)], snapshots=[_p("a", 5)], now=NOW,
                       snapshot_stale_hours=4)
    assert [r["agent"] for r in out] == ["a"]


# --- cmd_health wiring (text path) -------------------------------------------

import json

from coord_engine import cli, continuity, okf
from coord_engine_test_helpers import FakeTransport


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _beat(t, team, agent, hours_ago):
    # Stamp relative to PINNED_NOW (the pinned verb clock), not the real clock —
    # health computes freshness off cli._now() == PINNED_NOW.
    fm = {"type": "Presence", "agent": agent,
          "timestamp": _iso(PINNED_NOW - timedelta(hours=hours_ago))}
    t.put(f"team/{team}/presence/{agent}.md", okf.render_frontmatter(fm) + f"\n# Presence: {agent}\n")


def _snap(t, team, agent, task, hours_ago, *, pointer=True):
    """A snapshot AND its LATEST pointer, which is what a real save writes.

    The audit reads the pointer, not the task documents — a directory listing
    carries no mtime, so finding the newest snapshot otherwise costs one read
    per task (upstream register U8). `pointer=False` models the pre-pointer
    world, or a pointer write that failed: the agent has snapshots the audit
    cannot date."""
    snap = continuity.build_snapshot(
        agent=agent, task=task, objective="o",
        now=_iso(PINNED_NOW - timedelta(hours=hours_ago)))
    t.put(cli._continuity_path(team, agent, task), json.dumps(snap))
    if pointer:
        t.put(cli._continuity_latest_path(team, agent), json.dumps({
            "schema": "coord.continuity-latest.v1", "agent": agent, "task": task,
            "checkpoint_id": snap["checkpoint_id"],
            "created_at": snap.get("created_at"),
            "path": cli._continuity_path(team, agent, task),
        }))


def test_health_flags_fresh_presence_missing_snapshot(capsys):
    t = FakeTransport()
    _beat(t, "r", "bob", 1)
    assert cli.main(["health", "r"], transport=t) in (0, 1)
    out = capsys.readouterr().out
    assert any("continuity-stale" in ln and "bob" in ln for ln in out.splitlines())


def test_health_clean_when_snapshot_fresh(capsys):
    t = FakeTransport()
    _beat(t, "r", "alice", 1)
    _snap(t, "r", "alice", "t1", 2)
    assert cli.main(["health", "r"], transport=t) in (0, 1)
    assert "continuity-stale" not in capsys.readouterr().out


def test_health_survives_malformed_snapshot_and_still_flags_from_valid_data(capsys):
    t = FakeTransport()
    # carol: fresh presence, only a corrupt snapshot shard -> flagged, no crash
    _beat(t, "r", "carol", 1)
    t.put(cli._continuity_path("r", "carol", "bad"), "{not json")
    # dan: fresh presence, one bad-timestamp shard + one genuinely stale one ->
    # the corrupt shard must not shadow the valid stale evidence
    _beat(t, "r", "dan", 1)
    t.put(cli._continuity_path("r", "dan", "badts"),
          json.dumps({"agent": "dan", "task": "badts", "created_at": "not-a-time"}))
    _snap(t, "r", "dan", "old", 30)
    # erin: fresh presence, corrupt shard + fresh valid one -> clean
    _beat(t, "r", "erin", 1)
    t.put(cli._continuity_path("r", "erin", "bad"), "{not json")
    _snap(t, "r", "erin", "t1", 2)
    # SEMANTICS CHANGED WITH THE POINTER, and in the honest direction. carol has
    # a snapshot shard — it is merely unreadable — so the old "snapshot missing"
    # was the absent-vs-unreadable conflation this codebase keeps paying for.
    # She is now UNKNOWN. dan is the CONTRAST that keeps this honest: he has a
    # valid pointer at a 30h-old snapshot, so he is still proven stale, and a
    # corrupt shard beside it does not shadow that evidence — which was this
    # test's original point and still holds.
    #
    # The transitional cost is explicit elsewhere: a PRE-pointer agent reads
    # UNKNOWN until their next snapshot writes one (see
    # test_a_missing_pointer_is_UNKNOWN_not_stale). Losing a finding is the
    # right trade against manufacturing one — and an agent with NO snapshots at
    # all is still flagged (test_health_flags_fresh_presence_missing_snapshot).
    assert cli.main(["health", "r", "--json"], transport=t) in (0, 1)
    view = json.loads(capsys.readouterr().out)
    stale = {row.get("agent") for row in view.get("continuity_stale", [])}
    unknown = set(view.get("continuity_unknown", []))
    assert "carol" in unknown and "carol" not in stale, "a shard exists; it is not missing"
    assert "dan" in stale and "dan" not in unknown, \
        "a valid pointer at an old snapshot is still proven staleness"
    assert "erin" not in stale and "erin" not in unknown, "fresh pointer -> clean"


# --- cmd_health wiring (JSON path) -------------------------------------------

def test_health_json_includes_flagged_agent_under_continuity_stale(capsys):
    t = FakeTransport()
    _beat(t, "r", "bob", 1)  # fresh presence, no snapshot -> flagged
    assert cli.main(["health", "r", "--json"], transport=t) in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert "continuity_stale" in payload
    agents = [row["agent"] for row in payload["continuity_stale"]]
    assert agents == ["bob"]
    row = payload["continuity_stale"][0]
    # same fields stale_agents returns
    assert set(row) == {"agent", "presence_age_h", "snapshot_age_h"}
    assert row["snapshot_age_h"] is None


def test_health_json_continuity_stale_empty_when_clean(capsys):
    t = FakeTransport()
    _beat(t, "r", "alice", 1)
    _snap(t, "r", "alice", "t1", 2)  # fresh snapshot -> clean
    assert cli.main(["health", "r", "--json"], transport=t) in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["continuity_stale"] == []


# --- the pointer, and what a MISSING one must not be mistaken for -----------

def test_a_missing_pointer_is_UNKNOWN_not_stale(capsys):
    """The load-bearing refusal. An agent with snapshots but no LATEST pointer
    (pre-pointer history, or a pointer write that failed) must NOT be flagged
    stale: the audit cannot date them, and "we could not tell" is not evidence
    that they stopped checkpointing.

    I built the zero-read version of this audit first and it declared every
    agent in the fleet stale, because it read mtimes off directory entries that
    carry none. That is the shape this test exists to keep out."""
    t = FakeTransport()
    _beat(t, "r", "erin", 1)
    _snap(t, "r", "erin", "t1", 2, pointer=False)   # fresh work, no pointer
    capsys.readouterr()
    assert cli.main(["health", "r", "--json"], transport=t) in (0, 1)
    view = json.loads(capsys.readouterr().out)
    assert "erin" not in [row.get("agent") for row in view.get("continuity_stale", [])], \
        "an undatable agent must not be accused of going stale"
    assert "erin" in view.get("continuity_unknown", []), \
        "and it must be REPORTED as unknown, not silently dropped"


def test_the_audit_costs_one_read_per_agent(capsys):
    """The whole point of the pointer. The previous shape read every snapshot
    document of every agent — 203 reads / ~149s for three real agents — and the
    verb was killed at 240s and again at 590s on a live store."""
    class Counting(FakeTransport):
        def __init__(self):
            super().__init__()
            self.snapshot_reads = 0

        def read(self, path):
            if "/continuity/" in path:
                self.snapshot_reads += 1
            return super().read(path)

    t = Counting()
    _beat(t, "r", "frank", 1)
    for i in range(12):                      # twelve tasks, one agent
        _snap(t, "r", "frank", f"t{i}", 2)
    t.snapshot_reads = 0
    capsys.readouterr()
    cli.main(["health", "r"], transport=t)
    assert t.snapshot_reads == 1, (
        f"one read per agent regardless of task count; got {t.snapshot_reads}")


def test_the_pointer_is_written_only_after_the_snapshot_persists(capsys):
    """A pointer to a checkpoint that does not exist is worse than no pointer:
    a reader takes it as evidence of a save that never happened."""
    class Dark(FakeTransport):
        def write(self, path, content):
            return False

    t = Dark()
    capsys.readouterr()
    rc = cli.main(["continuity", "snapshot", "r", "gina", "slug",
                   "--objective", "o", "--next", "n"], transport=t)
    assert rc == 3, "the snapshot itself failed, so this is degraded"
    assert t.read(cli._continuity_latest_path("r", "gina")) is None, \
        "no pointer may survive a snapshot that did not persist"


def test_a_successful_snapshot_WRITES_the_pointer(capsys):
    """The actuator test. Without it the whole pointer scheme is unpinned:
    every other test here builds the pointer in its own fixture, so deleting
    the writer entirely leaves them all green — I checked, by deleting it.

    The audit is only cheap because this write happens."""
    t = FakeTransport()
    capsys.readouterr()
    rc = cli.main(["continuity", "snapshot", "r", "hank", "some-task",
                   "--objective", "o", "--next", "n"], transport=t)
    assert rc == 0
    raw = t.read(cli._continuity_latest_path("r", "hank"))
    assert raw is not None, "a successful snapshot must leave a LATEST pointer"
    ptr = json.loads(raw)
    assert ptr["task"] == "some-task"
    assert ptr["schema"] == "coord.continuity-latest.v1"
    assert ptr["path"] == cli._continuity_path("r", "hank", "some-task")
    assert ptr.get("created_at"), "the pointer must carry the timestamp the audit reads"


def test_a_failed_pointer_update_removes_the_STALE_one(capsys):
    """codex-reviewer, 585 r1, reproduced exactly: an OLD pointer plus a failed
    pointer update is not 'pointer missing'. The old file survives, the audit
    reads its timestamp as authoritative, and an agent who JUST checkpointed is
    reported stale — the false finding this design claims to prevent, and it
    made my own failure message ('will report UNKNOWN') a lie.

    With no conditional write, the only way to stop a stale cache being believed
    is to remove it. Deleting loses nothing recoverable: the pointer is a cache,
    the snapshots behind it are untouched, and missing means UNKNOWN."""
    class PointerWriteFails(FakeTransport):
        def write(self, path, content):
            if path.endswith("/continuity/LATEST.json"):
                return False
            return super().write(path, content)

    t = PointerWriteFails()
    _beat(t, "r", "iris", 1)
    _snap(t, "r", "iris", "old", 30)            # an OLD snapshot AND pointer
    capsys.readouterr()
    # ...now a fresh snapshot saves, but its pointer update fails.
    cli.main(["continuity", "snapshot", "r", "iris", "fresh",
              "--objective", "o", "--next", "n"], transport=t)
    assert t.read(cli._continuity_latest_path("r", "iris")) is None, \
        "the stale pointer must be REMOVED, not left to be read as current"

    capsys.readouterr()
    cli.main(["health", "r", "--json"], transport=t)
    view = json.loads(capsys.readouterr().out)
    stale = {row.get("agent") for row in view.get("continuity_stale", [])}
    assert "iris" not in stale, "an agent who just checkpointed is not stale"
    assert "iris" in view.get("continuity_unknown", [])


def test_an_unremovable_stale_pointer_is_rc3_and_says_so(capsys):
    """If the stale pointer can be neither updated nor removed, a wrong answer
    is queued up and this process cannot stop it. That is not a degradation to
    mention in passing — it is the loudest thing the verb says."""
    class Stuck(FakeTransport):
        def write(self, path, content):
            if path.endswith("/continuity/LATEST.json"):
                return False
            return super().write(path, content)

        def delete(self, path):
            return False

    t = Stuck()
    _snap(t, "r", "jane", "old", 30)
    capsys.readouterr()
    rc = cli.main(["continuity", "snapshot", "r", "jane", "fresh",
                   "--objective", "o", "--next", "n"], transport=t)
    err = capsys.readouterr().err
    assert rc == 3
    assert "STALE pointer is still in place" in err
    assert "may report this agent stale while they are actively checkpointing" in err


def test_an_older_snapshot_does_not_move_the_pointer_backwards(capsys, monkeypatch):
    """Monotonicity, best-effort. Two snapshots racing can land out of order and
    an OLDER invocation can clobber a newer pointer, reporting an active agent
    as older than they are. Read-then-write closes the common case; with no
    conditional write it cannot close the race, and the comment says so."""
    t = FakeTransport()
    _snap(t, "r", "kip", "recent", 1)           # pointer at 1h ago
    before = t.read(cli._continuity_latest_path("r", "kip"))
    capsys.readouterr()
    # An older invocation finishes late and tries to publish its own pointer.
    monkeypatch.setattr(cli, "_now", lambda: PINNED_NOW - timedelta(hours=20))
    cli.main(["continuity", "snapshot", "r", "kip", "older",
              "--objective", "o", "--next", "n"], transport=t)
    after = t.read(cli._continuity_latest_path("r", "kip"))
    assert after == before, "an older snapshot must not move the pointer backwards"
    assert "BACKWARDS" in capsys.readouterr().err
