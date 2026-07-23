"""W1.5 — activity-implies-liveness write-path refresh.

Every engine bus WRITE verb bumps the ACTOR's presence timestamp so a *working*
agent is provably live (distinct from a dead session whose launchd beat still
ticks). Two hard constraints, each pinned here:

1. THROTTLE — at most one presence write per interval per process (module memo).
2. FAILURE ISOLATION — a refresh failure never fails the bus write.

Plus preservation (timestamp bump only), the exact write-verb set, and
actor = the WRITER (not a target assignee).
"""

import pytest

from coord_engine import cli, okf, presence, tasks
from coord_engine_test_helpers import FakeTransport


@pytest.fixture(autouse=True)
def _reset_activity_memo():
    """The throttle memo is process-global module state. Reset it around every
    test so throttle behavior is deterministic. Defensive getattr so the suite
    still fails on each test's OWN assertion before the feature exists."""
    getattr(cli, "_ACTIVITY_BEAT_MEMO", {}).clear()
    yield
    getattr(cli, "_ACTIVITY_BEAT_MEMO", {}).clear()


def _presence_path(team, actor):
    return f"team/{team}/presence/{tasks.agent_key(actor)}.md"


class _PresenceWriteRaises(FakeTransport):
    """Writes to the presence shard raise; every other write succeeds. Models a
    store that can accept the directive but not the presence bump."""

    def write(self, path, content):
        if "/presence/" in path:
            raise RuntimeError("presence store unavailable")
        return super().write(path, content)


# --- 1. THROTTLE -------------------------------------------------------------

def test_throttle_collapses_writes_within_one_interval():
    t = FakeTransport()
    actor, team = "worker-1", "r"
    path = _presence_path(team, actor)

    cli._refresh_activity_presence(
        t, team, actor, now_monotonic=100.0, now_iso="2026-07-02T12:00:00Z")
    # Second call, still inside the interval -> throttled (no write, shard keeps
    # the first timestamp).
    cli._refresh_activity_presence(
        t, team, actor,
        now_monotonic=100.0 + presence.ACTIVITY_REFRESH_INTERVAL - 1,
        now_iso="2026-07-02T12:30:00Z")
    assert "timestamp: 2026-07-02T12:00:00Z" in t.store[path]
    assert "2026-07-02T12:30:00Z" not in t.store[path]

    # A call AFTER the interval elapses -> a second write.
    cli._refresh_activity_presence(
        t, team, actor,
        now_monotonic=100.0 + presence.ACTIVITY_REFRESH_INTERVAL,
        now_iso="2026-07-02T13:00:00Z")
    assert "timestamp: 2026-07-02T13:00:00Z" in t.store[path]


def test_throttle_counts_exactly_one_write_for_a_burst():
    writes = []
    t = FakeTransport()
    real_write = t.write

    def counting_write(path, content):
        if "/presence/" in path:
            writes.append(path)
        return real_write(path, content)

    t.write = counting_write
    for i in range(5):
        cli._refresh_activity_presence(
            t, "r", "worker-b",
            now_monotonic=1.0 + i,  # all inside a 60s interval
            now_iso=f"2026-07-02T12:00:0{i}Z")
    assert len(writes) == 1


# --- 2. FAILURE ISOLATION ----------------------------------------------------

def test_refresh_failure_never_fails_the_bus_write(monkeypatch, capsys):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "worker-2")
    t = _PresenceWriteRaises()
    rc = cli.main(["tell", "r", "amy", "Do the thing"], transport=t)
    assert rc == 0  # the bus write still succeeds
    # the directive itself was written (a non-presence doc under team/r)
    assert any(p.startswith("team/r/") and "/presence/" not in p for p in t.store)
    err = capsys.readouterr().err
    assert "presence activity-refresh failed" in err


def test_refresh_failure_helper_swallows_and_notes(capsys):
    t = _PresenceWriteRaises()
    # Must not raise.
    cli._refresh_activity_presence(
        t, "r", "worker-2b", now_monotonic=1.0, now_iso="2026-07-02T12:00:00Z")
    assert "presence activity-refresh failed" in capsys.readouterr().err


# --- 3. PRESERVATION (timestamp bump only) -----------------------------------

def test_refresh_preserves_everything_but_the_timestamp():
    actor, team = "worker-3", "r"
    fm = {
        "type": "Presence", "title": f"presence — {actor}", "agent": actor,
        "workstreams": ["w1.5"], "summary": "wiring the hook",
        "timestamp": "2026-07-02T04:00:00Z",
        "engagement": {"mode": "session", "until": "2026-07-02T09:00:00Z",
                       "state": "lapsed", "lapsed_at": "2026-07-02T08:00:00Z"},
    }
    original = okf.render_frontmatter(fm) + f"\n# Presence: {actor}\n"
    t = FakeTransport()
    path = _presence_path(team, actor)
    t.put(path, original)

    cli._refresh_activity_presence(
        t, team, actor, now_monotonic=1.0, now_iso="2026-07-02T12:00:00Z")
    updated = t.store[path]

    o, u = original.split("\n"), updated.split("\n")
    assert len(o) == len(u)
    diff = [(a, b) for a, b in zip(o, u) if a != b]
    # EXACTLY one line changed, and it is the timestamp line.
    assert diff == [("timestamp: 2026-07-02T04:00:00Z",
                     "timestamp: 2026-07-02T12:00:00Z")]
    # Engagement (incl. state/lapsed_at) and until are byte-identical.
    assert "until: 2026-07-02T09:00:00Z" in updated
    assert "state: lapsed" in updated
    assert "lapsed_at: 2026-07-02T08:00:00Z" in updated
    # And re-parses to the identical engagement object.
    assert (presence.parse_engagement(okf.parse_frontmatter(updated))
            == presence.parse_engagement(okf.parse_frontmatter(original)))


def test_refresh_does_not_clobber_a_present_shard_lacking_a_timestamp(capsys):
    # A PRESENT-but-malformed shard (engagement + workstreams intact, but the
    # top-level ``timestamp:`` line missing) must NOT be overwritten with a
    # minimal beat — that would erase live engagement/workstreams, exactly the
    # clobber this build exists to prevent. The refresh skips; the next real
    # ``presence beat`` repairs it.
    actor, team = "worker-3c", "r"
    fm = {
        "type": "Presence", "title": f"presence — {actor}", "agent": actor,
        "workstreams": ["w1.5"], "summary": "wiring the hook",
        "engagement": {"mode": "session", "until": "2026-07-02T09:00:00Z",
                       "state": "lapsed", "lapsed_at": "2026-07-02T08:00:00Z"},
    }
    # Render, then strip the top-level timestamp line to simulate a malformed
    # present shard.
    rendered = okf.render_frontmatter(fm) + f"\n# Presence: {actor}\n"
    original = "\n".join(ln for ln in rendered.split("\n")
                         if not ln.startswith("timestamp:"))
    t = FakeTransport()
    path = _presence_path(team, actor)
    t.put(path, original)

    cli._refresh_activity_presence(
        t, team, actor, now_monotonic=1.0, now_iso="2026-07-02T12:00:00Z")

    # Byte-identical: no minimal-beat clobber, engagement + workstreams intact.
    assert t.store[path] == original
    assert "state: lapsed" in t.store[path]
    assert "w1.5" in t.store[path]


def test_refresh_writes_minimal_beat_when_no_shard_exists():
    actor, team = "worker-3b", "r"
    t = FakeTransport()
    path = _presence_path(team, actor)
    assert path not in t.store
    cli._refresh_activity_presence(
        t, team, actor, now_monotonic=1.0, now_iso="2026-07-02T12:00:00Z")
    shard = t.store[path]
    assert "type: Presence" in shard
    assert f"agent: {actor}" in shard
    assert "timestamp: 2026-07-02T12:00:00Z" in shard
    # A bare activity beat must NOT invent an engagement object.
    assert "engagement:" not in shard


# --- 4. THE WRITE-VERB SET ---------------------------------------------------

def test_write_verb_set_is_exactly_the_bus_writers():
    expected = {
        cli.cmd_tell, cli.cmd_respond,
        cli.cmd_task_start, cli.cmd_task_update, cli.cmd_task_block,
        cli.cmd_task_pause, cli.cmd_task_abandon, cli.cmd_task_assign,
        cli.cmd_task_restore, cli.cmd_task_done,
        cli.cmd_review_request, cli.cmd_review_restore,
        cli.cmd_reconcile,
    }
    assert set(cli._ACTIVITY_WRITE_FUNCS) == expected


def test_read_verbs_are_not_in_the_write_set():
    for read_fn in (cli.cmd_status, cli.cmd_board, cli.cmd_search,
                    cli.cmd_needs_me, cli.cmd_briefing,
                    cli.cmd_presence_show, cli.cmd_review_status,
                    cli.cmd_presence_beat):
        assert read_fn not in cli._ACTIVITY_WRITE_FUNCS


# --- dispatch-seam behavior (verb-agnostic: keyed on the set) ----------------

def test_dispatch_hook_fires_for_tell(monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "worker-4")
    t = FakeTransport()
    path = _presence_path("r", "worker-4")
    assert path not in t.store
    assert cli.main(["tell", "r", "amy", "ship it"], transport=t) == 0
    assert path in t.store


def test_dispatch_hook_fires_for_reconcile(monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "worker-6")
    t = FakeTransport()
    assert cli.main(["reconcile", "r"], transport=t) == 0
    assert _presence_path("r", "worker-6") in t.store


def test_dispatch_hook_skips_read_verbs(monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "worker-5")
    t = FakeTransport()
    assert cli.main(["presence", "show", "r"], transport=t) == 0
    assert _presence_path("r", "worker-5") not in t.store


def test_dispatch_hook_skips_when_actor_unresolved(monkeypatch):
    # No --from and no FULCRA_COORD_AGENT: the writer identity is only the
    # anonymous host fallback, which is not a presence identity -> skip.
    monkeypatch.delenv("FULCRA_COORD_AGENT", raising=False)
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "anon"], transport=t) == 0
    assert not any("/presence/" in p for p in t.store)


# --- 5. ACTOR = THE WRITER, NOT THE TARGET -----------------------------------

def test_refresh_targets_the_writer_not_the_assignee(monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "the-writer")
    t = FakeTransport()
    assert cli.main(["tell", "r", "the-assignee", "do X"], transport=t) == 0
    assert _presence_path("r", "the-writer") in t.store
    assert _presence_path("r", "the-assignee") not in t.store


def test_refresh_honors_from_override_as_the_writer(monkeypatch):
    # --from names the writer explicitly; it wins over the env identity.
    monkeypatch.setenv("FULCRA_COORD_AGENT", "env-identity")
    t = FakeTransport()
    assert cli.main(["tell", "r", "amy", "x", "--from", "flag-writer"],
                    transport=t) == 0
    assert _presence_path("r", "flag-writer") in t.store
    assert _presence_path("r", "env-identity") not in t.store
