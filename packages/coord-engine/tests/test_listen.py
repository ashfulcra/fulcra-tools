"""Tests for the `listen` verb — the await leg of `tell`.

Two id-diff'd event sources (new inbox directives; new responses to directives
the agent owns), a persisted state file, and the fail-visible / no-false-advance
disciplines that this week's incidents made binding.
"""

import argparse
import json

import pytest

from coord_engine import cli, okf, reconcile, tasks
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

TEAM = "r"
NOW = "2026-07-10T00:00:00Z"
TODAY = "2026-07-10"


def _directive_doc(slug, title, *, owner, assignee, status="proposed"):
    fm = {"type": "Task", "id": slug, "title": title, "status": status,
          "priority": "P2", "owner": owner, "assignee": assignee}
    return okf.render_frontmatter(fm) + "\nbody\n"


def _put_directive(t, slug, title, *, owner, assignee, status="proposed"):
    t.put(cli._task_path(TEAM, slug),
          _directive_doc(slug, title, owner=owner, assignee=assignee, status=status))


def _put_response(t, slug, stamp, *, agent, outcome, evidence="done"):
    fm = {"type": "Response", "agent": agent, "outcome": outcome, "timestamp": NOW}
    t.put(cli._response_path(TEAM, slug, stamp),
          okf.render_frontmatter(fm) + f"\n{evidence}\n")


def _reconcile(t):
    reconcile.reconcile(t, TEAM, now=NOW, today=TODAY, host="h")


def _put_review(t, slug, *, requested_by, required=("rev",)):
    fm = {"type": "Review", "schema": "review-request/v1",
          "requested_by": requested_by, "of": "url",
          "required": list(required), "ts": NOW}
    t.put(cli._review_doc_path(TEAM, slug), okf.render_frontmatter(fm) + "\n")


def _put_verdict(t, slug, reviewer, verdict="approve"):
    fm = {"type": "Verdict", "reviewer": reviewer, "verdict": verdict}
    t.put(cli._verdicts_prefix(TEAM, slug) + f"{reviewer}.md",
          okf.render_frontmatter(fm) + "\n")


class ListCountingTransport(FakeTransport):
    """FakeTransport that records every list_dir prefix (for cost assertions)."""

    def __init__(self):
        super().__init__()
        self.lists: list[str] = []

    def list_dir(self, prefix):
        self.lists.append(prefix)
        return super().list_dir(prefix)


def _fresh_state():
    return {"inbox_ids": [], "response_keys": [], "slug_owned": {},
            "verdict_keys": [], "review_requested": {}, "settled_reviews": [],
            "degraded": {"inbox": False, "responses": False, "orphans": False,
                         "verdicts": False}}


# --- Source 1: inbox directives -------------------------------------------

def test_new_directive_fires_then_quiet_and_state_round_trips(capsys):
    t = FakeTransport()
    _put_directive(t, "do-thing-a1", "Do the thing", owner="alice", assignee="bob")
    _reconcile(t)
    state = _fresh_state()

    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    out = capsys.readouterr().out
    assert failures == {}
    assert len(events) == 1
    assert out.strip() == "DIRECTIVE do-thing-a1 (from alice): Do the thing"
    assert state["inbox_ids"] == ["do-thing-a1"]

    # second tick: same directive, no new id -> quiet (nothing to stdout)
    events2, _ = cli._run_listen_tick(t, TEAM, "bob", state,
                                      json_mode=False, verbose=False)
    out2 = capsys.readouterr().out
    assert events2 == []
    assert out2 == ""


def test_directive_json_mode_one_object_per_line(capsys):
    t = FakeTransport()
    _put_directive(t, "slug-b2", "Title B", owner="alice", assignee="bob")
    _reconcile(t)
    cli._run_listen_tick(t, TEAM, "bob", _fresh_state(),
                         json_mode=True, verbose=False)
    out = capsys.readouterr().out.strip()
    obj = json.loads(out)
    assert obj == {"type": "directive", "slug": "slug-b2",
                   "owner": "alice", "title": "Title B"}


def test_id_diff_not_count_ack_plus_new_still_fires(capsys):
    """An ack removes A and a new B arrives — open count stays 1, but B is a NEW
    id and must fire (the id-diff-not-count lesson)."""
    t = FakeTransport()
    _put_directive(t, "aaa-1", "Alpha", owner="alice", assignee="bob")
    _reconcile(t)
    state = _fresh_state()
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    capsys.readouterr()
    assert state["inbox_ids"] == ["aaa-1"]

    # ack A (hides it) + add B — count is still 1 open for bob
    t.put(cli._ack_path(TEAM, "aaa-1", "bob"), "acked")
    _put_directive(t, "bbb-2", "Bravo", owner="alice", assignee="bob")
    _reconcile(t)
    events, _ = cli._run_listen_tick(t, TEAM, "bob", state,
                                     json_mode=False, verbose=False)
    out = capsys.readouterr().out
    assert [e["slug"] for e in events] == ["bbb-2"]
    assert "DIRECTIVE bbb-2" in out
    assert set(state["inbox_ids"]) == {"aaa-1", "bbb-2"}


# --- Source 2: responses to owned directives ------------------------------

def test_new_response_on_owned_slug_fires(capsys):
    t = FakeTransport()
    # bob owns the directive (owner == the listening agent)
    _put_directive(t, "owned-1", "Owned", owner="bob", assignee="carol")
    _put_response(t, "owned-1", "20260710T0000-carol", agent="carol", outcome="ACK")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    out = capsys.readouterr().out
    assert failures == {}
    assert [e["type"] for e in events] == ["response"]
    assert out.strip() == "RESPONSE owned-1 by carol: ACK"
    assert state["response_keys"] == ["owned-1/20260710T0000-carol"]
    assert state["slug_owned"] == {"owned-1": True}

    # second tick: same response, quiet
    events2, _ = cli._run_listen_tick(t, TEAM, "bob", state,
                                      json_mode=False, verbose=False)
    assert events2 == []
    assert capsys.readouterr().out == ""


def test_response_on_unowned_slug_is_quiet(capsys):
    t = FakeTransport()
    # directive owned by someone else; bob is listening
    _put_directive(t, "other-1", "Other", owner="alice", assignee="carol")
    _put_response(t, "other-1", "20260710T0000-carol", agent="carol", outcome="DONE")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    assert events == []
    assert failures == {}
    assert capsys.readouterr().out == ""
    # ownership decided once and cached as not-owned (skips listing forever)
    assert state["slug_owned"] == {"other-1": False}
    assert state["response_keys"] == []


def test_broadcast_owner_response_is_noise(capsys):
    t = FakeTransport()
    _put_directive(t, "bcast-1", "Broadcast", owner="*", assignee="*")
    _put_response(t, "bcast-1", "20260710T0000-carol", agent="carol", outcome="ACK")
    state = _fresh_state()
    events, _ = cli._run_listen_tick(t, TEAM, "bob", state,
                                     json_mode=False, verbose=False)
    assert events == []
    assert capsys.readouterr().out == ""
    assert state["slug_owned"] == {"bcast-1": False}


def test_second_response_to_owned_slug_fires(capsys):
    t = FakeTransport()
    _put_directive(t, "owned-2", "Owned2", owner="bob", assignee="carol")
    _put_response(t, "owned-2", "20260710T0000-carol", agent="carol", outcome="ACK")
    state = _fresh_state()
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    capsys.readouterr()
    # a SECOND response lands in the already-known slug dir
    _put_response(t, "owned-2", "20260710T0100-dave", agent="dave", outcome="DONE")
    events, _ = cli._run_listen_tick(t, TEAM, "bob", state,
                                     json_mode=False, verbose=False)
    out = capsys.readouterr().out
    assert [e["agent"] for e in events] == ["dave"]
    assert "RESPONSE owned-2 by dave: DONE" in out


# --- Fail-visible / no-false-advance --------------------------------------

def test_transport_failure_degraded_once_no_advance_then_recovers(capsys):
    t = FakeTransport()
    _put_directive(t, "owned-3", "Owned3", owner="bob", assignee="carol")
    _put_response(t, "owned-3", "20260710T0000-carol", agent="carol", outcome="ACK")
    state = _fresh_state()

    # tick 1: responses listing fails -> one DEGRADED line to stderr, no advance
    t.fail_list = True
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    err = capsys.readouterr()
    assert events == []
    assert failures  # a failure was recorded
    assert err.out == ""  # nothing to stdout on a degraded/quiet tick
    assert err.err.count("LISTEN DEGRADED") == 1
    assert state["degraded"]["responses"] is True
    assert state["response_keys"] == []  # NOT advanced over unknown data
    assert state["slug_owned"] == {}     # ownership not cached from a failed pass

    # tick 2: still failing -> suppressed (once per streak, no flooding)
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    err2 = capsys.readouterr()
    assert "LISTEN DEGRADED" not in err2.err
    assert state["response_keys"] == []

    # tick 3: recovery -> the pending response is emitted, streak resets
    t.fail_list = False
    events3, failures3 = cli._run_listen_tick(t, TEAM, "bob", state,
                                              json_mode=False, verbose=False)
    out3 = capsys.readouterr().out
    assert failures3 == {}
    assert [e["slug"] for e in events3] == ["owned-3"]
    assert "RESPONSE owned-3 by carol: ACK" in out3
    assert not any(state["degraded"].values())  # every source streak reset
    assert state["response_keys"] == ["owned-3/20260710T0000-carol"]


def test_owner_unresolved_does_not_cache_or_advance(capsys):
    """A response whose directive doc can't be read is UNKNOWN ownership: don't
    cache, don't advance, flag degraded — then resolve once the doc appears."""
    t = FakeTransport()
    # response exists but NO directive doc yet (owner unresolvable)
    _put_response(t, "ghost-1", "20260710T0000-carol", agent="carol", outcome="ACK")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    err = capsys.readouterr()
    assert events == []
    assert failures
    assert "LISTEN DEGRADED" in err.err
    assert state["slug_owned"] == {}       # not classified
    assert state["response_keys"] == []    # not advanced

    # directive doc appears (owned by bob) -> next tick resolves + fires
    _put_directive(t, "ghost-1", "Ghost", owner="bob", assignee="carol")
    events2, failures2 = cli._run_listen_tick(t, TEAM, "bob", state,
                                              json_mode=False, verbose=False)
    out2 = capsys.readouterr().out
    assert failures2 == {}
    assert [e["slug"] for e in events2] == ["ghost-1"]
    assert "RESPONSE ghost-1 by carol: ACK" in out2
    assert state["slug_owned"] == {"ghost-1": True}


def test_inbox_index_unreadable_is_degraded_not_silent(capsys):
    """M1: an unreadable summaries index must surface as degraded, not fold to a
    silent empty inbox indistinguishable from 'no directives'. Red at HEAD: a
    corrupt index was swallowed to [] with no LISTEN DEGRADED line."""
    t = FakeTransport()
    # index present but corrupt -> json parse fails (was silently folded to [])
    t.put(reconcile.summaries_path(TEAM), "{ not json")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    err = capsys.readouterr()
    assert events == []
    assert "LISTEN DEGRADED" in err.err       # surfaced, not silent
    assert "inbox" in failures                # attributed to the inbox source
    assert state["degraded"]["inbox"] is True


def test_inbox_transport_down_is_degraded_not_empty(capsys):
    """A summaries read of None under a down transport (list_dir raises) is unknown,
    not a confirmed-empty inbox — the inbox source goes degraded."""
    t = FakeTransport()
    t.fail_list = True  # no summaries doc + list_dir raises -> cannot confirm empty
    state = _fresh_state()
    _, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                       json_mode=False, verbose=False)
    err = capsys.readouterr()
    assert "inbox" in failures
    assert "LISTEN DEGRADED" in err.err
    assert state["degraded"]["inbox"] is True


def test_absent_index_is_readable_empty_not_degraded(capsys):
    """A genuinely-absent index (fresh team, no reconcile) is empty-and-readable —
    it must NOT alarm (do not conflate empty-and-readable with failed)."""
    t = FakeTransport()
    state = _fresh_state()
    _, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                       json_mode=False, verbose=False)
    err = capsys.readouterr()
    assert "inbox" not in failures
    assert "LISTEN DEGRADED" not in err.err


def test_pinned_orphan_does_not_silence_a_new_distinct_failure(capsys):
    """M2: per-source streaks. A permanent orphan (owner unresolved every tick)
    pins the `orphans` streak, but must NOT silence a NEW, distinct outage. Red at
    HEAD: one shared `degraded` bool stayed True from the orphan and swallowed the
    later transport failure — no second LISTEN DEGRADED ever fired."""
    t = FakeTransport()
    # a response whose directive doc never resolves -> owner unresolved every tick
    _put_response(t, "ghost-x", "20260710T0000-carol", agent="carol", outcome="ACK")
    state = _fresh_state()

    # tick 1: the orphan alarms once and pins its own streak
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    e1 = capsys.readouterr().err
    assert e1.count("LISTEN DEGRADED") == 1
    assert state["degraded"]["orphans"] is True

    # tick 2: orphan STILL unresolved AND a NEW distinct failure — the responses
    # subtree goes unreadable. A single shared flag would stay pinned and swallow
    # this; per-source streaks must re-alarm on the new source.
    t.fail_list = True
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    e2 = capsys.readouterr().err
    assert "LISTEN DEGRADED" in e2                 # the new outage is NOT silenced
    assert state["degraded"]["responses"] is True


def test_degraded_alarms_once_per_source_streak_then_suppresses(capsys):
    """A source that stays degraded alarms only once for its streak (no flooding),
    independent of other sources' streaks."""
    t = FakeTransport()
    _put_response(t, "ghost-y", "20260710T0000-carol", agent="carol", outcome="ACK")
    state = _fresh_state()
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    assert capsys.readouterr().err.count("LISTEN DEGRADED") == 1
    # same orphan next tick -> suppressed (once per streak)
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    assert "LISTEN DEGRADED" not in capsys.readouterr().err


def test_legacy_single_bool_degraded_migrates_to_per_source(tmp_path):
    """Backward-compat: a state file with the old single ``degraded`` bool migrates
    to the per-source dict (same value on each source), so an upgrade neither loses
    the streak nor invents one."""
    import json as _j
    path = tmp_path / "listen-r-bob.json"
    path.write_text(_j.dumps({"inbox_ids": ["x"], "response_keys": [],
                              "slug_owned": {}, "degraded": True}), encoding="utf-8")
    state = cli._load_listen_state(path)
    assert state["degraded"] == {"inbox": True, "responses": True,
                                 "orphans": True, "verdicts": True}
    assert state["inbox_ids"] == ["x"]

    path.write_text(_j.dumps({"degraded": False}), encoding="utf-8")
    assert cli._load_listen_state(path)["degraded"] == {
        "inbox": False, "responses": False, "orphans": False, "verdicts": False}


def test_verbose_heartbeat_only_on_quiet_tick_and_only_stderr(capsys):
    t = FakeTransport()
    state = _fresh_state()
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=True)
    err = capsys.readouterr()
    assert err.out == ""  # quiet: nothing to stdout even with --verbose
    assert "listen: quiet" in err.err


# --- Source 3: verdicts on reviews the agent REQUESTED --------------------

def test_verdict_on_requested_review_fires_then_quiet(capsys):
    t = FakeTransport()
    _put_review(t, "pr-1", requested_by="me")
    _put_verdict(t, "pr-1", "rev", "approve")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=False, verbose=False)
    out = capsys.readouterr().out
    assert failures == {}
    assert [e["type"] for e in events] == ["verdict"]
    assert out.strip() == "VERDICT pr-1 by rev: approve"
    assert state["verdict_keys"] == ["pr-1/rev"]
    assert state["review_requested"] == {"pr-1": True}

    # second tick: same verdict, no new id -> quiet
    events2, _ = cli._run_listen_tick(t, TEAM, "me", state,
                                      json_mode=False, verbose=False)
    assert events2 == []
    assert capsys.readouterr().out == ""


def test_second_verdict_on_requested_review_fires(capsys):
    t = FakeTransport()
    _put_review(t, "pr-2", requested_by="me", required=("alice", "bob"))
    _put_verdict(t, "pr-2", "alice", "approve")
    state = _fresh_state()
    cli._run_listen_tick(t, TEAM, "me", state, json_mode=False, verbose=False)
    capsys.readouterr()
    # a SECOND reviewer files a verdict on the same, still-unsettled slug
    _put_verdict(t, "pr-2", "bob", "changes")
    events, _ = cli._run_listen_tick(t, TEAM, "me", state,
                                     json_mode=False, verbose=False)
    out = capsys.readouterr().out
    assert [e["reviewer"] for e in events] == ["bob"]
    assert "VERDICT pr-2 by bob: changes" in out


def test_verdict_on_others_review_is_quiet(capsys):
    t = FakeTransport()
    _put_review(t, "pr-3", requested_by="alice")
    _put_verdict(t, "pr-3", "rev")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=False, verbose=False)
    assert events == []
    assert failures == {}
    assert capsys.readouterr().out == ""
    # requester decided once and cached as not-mine (skips listing forever)
    assert state["review_requested"] == {"pr-3": False}
    assert state["verdict_keys"] == []


def test_verdict_json_mode_one_object_per_line(capsys):
    t = FakeTransport()
    _put_review(t, "pr-j", requested_by="me")
    _put_verdict(t, "pr-j", "rev", "changes")
    cli._run_listen_tick(t, TEAM, "me", _fresh_state(),
                         json_mode=True, verbose=False)
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj == {"type": "verdict", "slug": "pr-j",
                   "reviewer": "rev", "verdict": "changes"}


def test_settled_review_stops_verdict_listings(capsys):
    # Bounded cost: once a review is `.settled`, the listener drops it from the
    # active set and never lists its verdicts dir again.
    t = ListCountingTransport()
    _put_review(t, "pr-s", requested_by="me")
    _put_verdict(t, "pr-s", "rev")
    t.put(cli._settled_marker_path(TEAM, "pr-s"),
          "---\nschema: review-settled/v1\nstate: APPROVED\n---\n")
    state = _fresh_state()
    # tick 1: lists review root + the slug's verdicts dir (sees .settled -> drop)
    cli._run_listen_tick(t, TEAM, "me", state, json_mode=False, verbose=False)
    capsys.readouterr()
    assert "pr-s" in state["settled_reviews"]
    # tick 2: the settled slug costs ZERO verdicts-dir listings
    t.lists.clear()
    cli._run_listen_tick(t, TEAM, "me", state, json_mode=False, verbose=False)
    assert cli._verdicts_prefix(TEAM, "pr-s") not in t.lists, \
        f"settled slug must not be listed again, got {t.lists}"


def test_requester_unresolved_no_false_advance_then_recovers(capsys):
    """A review doc that can't be read is UNKNOWN requester: don't cache, don't
    advance, flag the `verdicts` source degraded — then resolve on recovery."""
    class ReviewDocReadFails(FakeTransport):
        def __init__(self):
            super().__init__()
            self.doc_fail = True

        def read(self, path):
            if (self.doc_fail and path.startswith("team/r/review/")
                    and path.endswith(".md") and "/verdicts/" not in path):
                return None  # timeout: content unknown, no exception raised
            return super().read(path)

    t = ReviewDocReadFails()
    _put_review(t, "pr-u", requested_by="me")
    _put_verdict(t, "pr-u", "rev")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=False, verbose=False)
    err = capsys.readouterr()
    assert events == []
    assert "verdicts" in failures
    assert "LISTEN DEGRADED" in err.err
    assert state["review_requested"] == {}   # not classified
    assert state["verdict_keys"] == []        # not advanced
    assert state["degraded"]["verdicts"] is True

    # review doc becomes readable -> next tick resolves + fires the verdict
    t.doc_fail = False
    events2, failures2 = cli._run_listen_tick(t, TEAM, "me", state,
                                              json_mode=False, verbose=False)
    out2 = capsys.readouterr().out
    assert failures2 == {}
    assert [e["slug"] for e in events2] == ["pr-u"]
    assert "VERDICT pr-u by rev: approve" in out2
    assert state["review_requested"] == {"pr-u": True}


def test_review_root_listing_failure_is_verdicts_degraded(capsys):
    # A verdicts-source outage is attributed to its OWN source, not inbox/responses.
    class ReviewListFails(FakeTransport):
        def list_dir(self, prefix):
            if prefix == "team/r/review/":
                raise TransportError("boom")
            return super().list_dir(prefix)

    t = ReviewListFails()
    state = _fresh_state()
    _, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                       json_mode=False, verbose=False)
    err = capsys.readouterr()
    assert "verdicts" in failures
    assert "inbox" not in failures and "responses" not in failures
    assert "LISTEN DEGRADED" in err.err
    assert state["degraded"]["verdicts"] is True


# --- State persistence + driver -------------------------------------------

def test_state_round_trips_through_disk(tmp_path):
    t = FakeTransport()
    _put_directive(t, "persist-1", "Persist", owner="alice", assignee="bob")
    _reconcile(t)
    path = tmp_path / "listen-r-bob.json"
    state = _fresh_state()
    cli._listen_tick(t, TEAM, "bob", state)
    cli._save_listen_state(path, state)
    reloaded = cli._load_listen_state(path)
    assert reloaded["inbox_ids"] == ["persist-1"]


def test_load_state_tolerates_corrupt_file(tmp_path):
    path = tmp_path / "corrupt.json"
    path.write_text("{not json", encoding="utf-8")
    state = cli._load_listen_state(path)
    assert state == {"inbox_ids": [], "response_keys": [], "slug_owned": {},
                     "verdict_keys": [], "review_requested": {}, "settled_reviews": [],
                     "degraded": {"inbox": False, "responses": False,
                                  "orphans": False, "verdicts": False}}


def test_cmd_listen_once_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORD_LISTENER_STATE", str(tmp_path))
    t = FakeTransport()
    _put_directive(t, "once-1", "Once", owner="alice", assignee="bob")
    _reconcile(t)
    args = argparse.Namespace(team=TEAM, agent="bob", interval=60,
                              once=True, verbose=False, json=False)
    rc = cli.cmd_listen(args, t)
    assert rc == 0
    assert "DIRECTIVE once-1" in capsys.readouterr().out
    # state persisted to disk under the env dir
    saved = cli._load_listen_state(cli._listen_state_path(TEAM, "bob"))
    assert saved["inbox_ids"] == ["once-1"]


def test_cmd_listen_loop_sigint_clean_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("COORD_LISTENER_STATE", str(tmp_path))
    t = FakeTransport()

    def boom(_secs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", boom)
    args = argparse.Namespace(team=TEAM, agent="bob", interval=1,
                              once=False, verbose=False, json=False)
    rc = cli.cmd_listen(args, t)  # one tick, then sleep raises SIGINT -> clean exit
    assert rc == 0


def test_main_wires_listen_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COORD_LISTENER_STATE", str(tmp_path))
    t = FakeTransport()
    _put_directive(t, "wired-1", "Wired", owner="alice", assignee="bob")
    _reconcile(t)
    rc = cli.main(["listen", TEAM, "--agent", "bob", "--once"], transport=t)
    assert rc == 0
    assert "DIRECTIVE wired-1" in capsys.readouterr().out


def test_listen_state_path_prints_and_does_not_tick(tmp_path, monkeypatch, capsys):
    """The hidden `--state-path` resolver (listener-tick.sh's migration uses it)
    prints the engine-resolved state file path and exits 0 WITHOUT ticking or
    writing — the slug/agent_key naming stays owned by the engine, not the shell."""
    monkeypatch.setenv("COORD_LISTENER_STATE", str(tmp_path))
    t = FakeTransport()
    _put_directive(t, "np-1", "NoTick", owner="alice", assignee="bob")
    _reconcile(t)
    rc = cli.main(["listen", TEAM, "--agent", "bob", "--state-path"], transport=t)
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out == str(cli._listen_state_path(TEAM, "bob"))
    assert not cli._listen_state_path(TEAM, "bob").exists()  # resolver never writes
