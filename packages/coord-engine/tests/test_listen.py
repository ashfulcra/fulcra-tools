"""Tests for the `listen` verb — the await leg of `tell`.

Two id-diff'd event sources (new inbox directives; new responses to directives
the agent owns), a persisted state file, and the fail-visible / no-false-advance
disciplines that this week's incidents made binding.
"""

import argparse
import json
import time

import pytest

from coord_engine import cli, okf, reconcile, tasks
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

TEAM = "r"
NOW = "2026-07-10T00:00:00Z"

# clock-pin support (see #378):
from datetime import datetime, timezone
PINNED_NOW = datetime(2026, 7, 10, 0, 30, tzinfo=timezone.utc)


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
                         "verdicts": False, "roles": False}}


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
                                 "orphans": True, "verdicts": True, "roles": True}
    assert state["inbox_ids"] == ["x"]

    path.write_text(_j.dumps({"degraded": False}), encoding="utf-8")
    assert cli._load_listen_state(path)["degraded"] == {
        "inbox": False, "responses": False, "orphans": False, "verdicts": False,
        "roles": False}


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


def test_settling_tick_emits_final_verdict_and_settled_then_stops_listings(capsys):
    # THE DOMINANT FLOW: a single approve settles the review and the reviewer
    # settles it themselves (`review status` after filing, per doctrine), so the
    # verdict shard and `.settled` CO-EXIST before the requester's next tick.
    # That tick must emit the settling (often only) verdict + one terminal
    # SETTLED line — dropping the slug first would swallow the final verdict
    # and make `await verdicts:` false in the standard single-reviewer flow.
    t = ListCountingTransport()
    _put_review(t, "pr-s", requested_by="me")
    _put_verdict(t, "pr-s", "rev")
    t.put(cli._settled_marker_path(TEAM, "pr-s"),
          "---\nschema: review-settled/v1\nstate: APPROVED\n---\n")
    state = _fresh_state()
    # tick 1: the settling tick EMITS (verdict first, then the terminal line)
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=False, verbose=False)
    out = capsys.readouterr().out
    assert failures == {}
    assert [e["type"] for e in events] == ["verdict", "settled"]
    assert "VERDICT pr-s by rev: approve" in out
    assert "SETTLED pr-s: APPROVED" in out
    assert state["verdict_keys"] == ["pr-s/rev"]
    assert "pr-s" in state["settled_reviews"]
    # tick 2: quiet, and the settled slug costs ZERO verdicts-dir listings
    t.lists.clear()
    events2, _ = cli._run_listen_tick(t, TEAM, "me", state,
                                      json_mode=False, verbose=False)
    assert events2 == []
    assert capsys.readouterr().out == ""
    assert cli._verdicts_prefix(TEAM, "pr-s") not in t.lists, \
        f"settled slug must not be listed again, got {t.lists}"


def test_settle_after_seen_verdicts_emits_settled_only(capsys):
    # All shards previously seen, THEN the marker lands: the requester still
    # learns the terminal state via one SETTLED event (json contract included).
    t = FakeTransport()
    _put_review(t, "pr-a", requested_by="me")
    _put_verdict(t, "pr-a", "rev")
    state = _fresh_state()
    cli._run_listen_tick(t, TEAM, "me", state, json_mode=False, verbose=False)
    capsys.readouterr()
    assert state["verdict_keys"] == ["pr-a/rev"]

    t.put(cli._settled_marker_path(TEAM, "pr-a"),
          "---\nschema: review-settled/v1\nstate: APPROVED\n---\n")
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=True, verbose=False)
    assert failures == {}
    assert events == [{"type": "settled", "slug": "pr-a", "state": "APPROVED"}]
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj == {"type": "settled", "slug": "pr-a", "state": "APPROVED"}
    assert "pr-a" in state["settled_reviews"]

    # re-tick: quiet (SETTLED fires exactly once per slug lifetime)
    events2, _ = cli._run_listen_tick(t, TEAM, "me", state,
                                      json_mode=True, verbose=False)
    assert events2 == []
    assert capsys.readouterr().out == ""


def test_unreadable_final_shard_at_settle_not_swallowed(capsys):
    # Settling must not swallow an unreadable final verdict: a None shard read
    # on a settling tick flags `verdicts` degraded and keeps the slug ACTIVE
    # (not dropped) — recovery emits the verdict + SETTLED, then drops.
    class ShardReadFails(FakeTransport):
        def __init__(self):
            super().__init__()
            self.shard_fail = True

        def read(self, path):
            if (self.shard_fail and "/verdicts/" in path
                    and path.endswith(".md")):
                return None  # timeout: content unknown, no exception raised
            return super().read(path)

    t = ShardReadFails()
    _put_review(t, "pr-f", requested_by="me")
    _put_verdict(t, "pr-f", "rev")
    t.put(cli._settled_marker_path(TEAM, "pr-f"),
          "---\nschema: review-settled/v1\nstate: APPROVED\n---\n")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=False, verbose=False)
    err = capsys.readouterr()
    assert events == []
    assert "verdicts" in failures
    assert "LISTEN DEGRADED" in err.err
    assert state["verdict_keys"] == []                 # not advanced
    assert "pr-f" not in state["settled_reviews"], \
        "an unreadable final verdict must keep the slug active (retry)"

    # transport recovers -> the final verdict emits, THEN the slug settles
    t.shard_fail = False
    events2, failures2 = cli._run_listen_tick(t, TEAM, "me", state,
                                              json_mode=False, verbose=False)
    out2 = capsys.readouterr().out
    assert failures2 == {}
    assert [e["type"] for e in events2] == ["verdict", "settled"]
    assert "VERDICT pr-f by rev: approve" in out2
    assert "SETTLED pr-f: APPROVED" in out2
    assert "pr-f" in state["settled_reviews"]


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
                     "orphan_slugs": [],
                     "flagged_orphan_responses": [], "flagged_orphan_verdicts": [],
                     "degraded": {"inbox": False, "responses": False,
                                  "orphans": False, "verdicts": False,
                                  "roles": False}}


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


# --- Task 2: role-routed directives + orphan review dirs ------------------

from coord_engine.transport import TransportError as _TE
from coord_engine.tasks import agent_key as _akey


def _put_role(t, role, *, sla_hours=8760000, policy="shared"):
    t.put(cli._role_doc_path(TEAM, role),
          f"---\ntype: Role\npolicy: {policy}\nsla_hours: {sla_hours}\n---\n")


def _put_lease(t, role, agent, *, ts=NOW):
    t.put(cli._leases_prefix(TEAM, role) + f"{_akey(agent)}.md",
          f"---\ntype: Lease\nagent: {agent}\ntimestamp: {ts}\n---\n")


def test_role_routed_directive_fires_holder_listen_then_quiet(capsys):
    # A directive assigned to a ROLE fires the current fresh-lease holder's listen.
    t = FakeTransport()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")
    _put_directive(t, "role-do-1", "Review the PR", owner="alice", assignee="reviewer")
    _reconcile(t)
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    assert failures == {}
    assert [e["type"] for e in events] == ["directive"]
    assert events[0]["slug"] == "role-do-1"
    assert "role-do-1" in state["inbox_ids"]
    # re-tick: same id -> quiet (id-diff on the directive slug, route-agnostic)
    events2, _ = cli._run_listen_tick(t, TEAM, "bob", state,
                                      json_mode=False, verbose=False)
    assert events2 == []


def test_role_routed_directive_not_fired_for_non_holder(capsys):
    t = FakeTransport()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")
    _put_directive(t, "role-do-2", "Review", owner="alice", assignee="reviewer")
    _reconcile(t)
    # carol does NOT hold the role -> quiet, no false directive
    events, failures = cli._run_listen_tick(t, TEAM, "carol", _fresh_state(),
                                            json_mode=False, verbose=False)
    assert events == [] and failures == {}


def test_role_routed_directive_lease_expiry_stops_it(capsys):
    # An expired (stale) lease means the agent is no longer a holder -> no fire.
    t = FakeTransport()
    _put_role(t, "reviewer", sla_hours=24)
    _put_lease(t, "reviewer", "bob", ts="2020-01-01T00:00:00Z")  # ancient -> stale
    _put_directive(t, "role-do-3", "Review", owner="alice", assignee="reviewer")
    _reconcile(t)
    events, failures = cli._run_listen_tick(t, TEAM, "bob", _fresh_state(),
                                            json_mode=False, verbose=False)
    assert events == [] and failures == {}


def test_role_holder_change_new_holder_sees_unseen_directive(capsys):
    # Holder-change semantics: state files are PER-AGENT, so the id-diff is per
    # holder. amy (current holder) sees the directive once; when the lease moves to
    # bob, bob's OWN fresh state has never seen the id, so bob fires — while amy,
    # who already recorded the id, stays quiet even before losing the lease.
    t = FakeTransport()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "amy")
    _put_directive(t, "role-do-4", "Review", owner="alice", assignee="reviewer")
    _reconcile(t)
    amy_state = _fresh_state()
    events, _ = cli._run_listen_tick(t, TEAM, "amy", amy_state,
                                     json_mode=False, verbose=False)
    assert [e["slug"] for e in events] == ["role-do-4"]
    assert "role-do-4" in amy_state["inbox_ids"]

    # lease moves to bob (amy released, bob claims)
    del t.store[cli._leases_prefix(TEAM, "reviewer") + f"{_akey('amy')}.md"]
    _put_lease(t, "reviewer", "bob")
    bob_state = _fresh_state()
    bob_events, _ = cli._run_listen_tick(t, TEAM, "bob", bob_state,
                                         json_mode=False, verbose=False)
    assert [e["slug"] for e in bob_events] == ["role-do-4"], \
        "new holder sees the directive iff its id is unseen in THEIR state"
    # amy re-ticks: id already in amy's state -> quiet regardless of lease
    amy_events2, _ = cli._run_listen_tick(t, TEAM, "amy", amy_state,
                                          json_mode=False, verbose=False)
    assert amy_events2 == []


def test_role_lease_read_degraded_is_visible_not_crash(capsys):
    # Fail-closed: a lease LISTING that raises must degrade VISIBLY (the agent may
    # miss role-routed work) — never crash the tick, never falsely fire.
    class LeaseListFails(FakeTransport):
        def list_dir(self, prefix):
            if prefix.endswith("/leases/"):
                raise _TE("boom")
            return super().list_dir(prefix)

    t = LeaseListFails()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")
    _put_directive(t, "role-do-5", "Review", owner="alice", assignee="reviewer")
    _reconcile(t)
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    # no crash; directive not falsely emitted; degradation surfaced on its OWN
    # "roles" source (not inbox — independent streaks, see the masking test below)
    assert not [e for e in events if e.get("slug") == "role-do-5"]
    assert "roles" in failures
    assert state["degraded"]["roles"] is True
    assert state["degraded"]["inbox"] is False


def test_orphan_review_dir_emits_one_listen_event_cached(capsys):
    # A review whose verdicts dir exists but whose <slug>.md doc does NOT is an
    # orphan: listen surfaces it ONCE (cached in state), doc-ful reviews unaffected.
    t = FakeTransport()
    _put_review(t, "pr-doc", requested_by="me")           # a normal, doc-ful review
    # orphan: verdicts dir present, no team/r/review/pr-orphan.md doc
    _put_verdict(t, "pr-orphan", "rev", "approve")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=False, verbose=False)
    out = capsys.readouterr().out
    orphans = [e for e in events if e.get("type") == "orphan"]
    assert [e["slug"] for e in orphans] == ["pr-orphan"]
    assert "ORPHAN pr-orphan" in out
    assert state["orphan_slugs"] == ["pr-orphan"]
    # re-tick: cached -> no repeat
    events2, _ = cli._run_listen_tick(t, TEAM, "me", state,
                                      json_mode=False, verbose=False)
    assert not [e for e in events2 if e.get("type") == "orphan"]


def test_empty_review_dir_is_tombstone_no_listen_orphan_or_degrade(capsys):
    # A `<slug>/` dir with NO verdict shards and no doc is a soft-delete ghost:
    # listen must NOT emit an orphan event, must NOT degrade, and must NOT cache it
    # (it carries zero information). A `.settled`-only dir is the same tombstone.
    t = FakeTransport()
    t.put("team/r/review/pr-empty/", "")                        # empty tombstone
    t.put("team/r/review/pr-stale/verdicts/.settled",          # stale-marker tombstone
          "---\nschema: review-settled/v1\nstate: APPROVED\n---\n")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=False, verbose=False)
    assert not [e for e in events if e.get("type") == "orphan"], "tombstones emit no orphan"
    assert failures == {}, "a tombstone is not a degraded source"
    assert state["orphan_slugs"] == [], "a tombstone must never be cached as an orphan"


def test_orphan_dir_listing_raise_degrades_listen_not_tombstone(capsys):
    # Fail-closed outranks tombstone-skip: a verdicts LISTING that RAISES is
    # UNKNOWN — surface a degraded `verdicts` source, never a silent tombstone.
    class OrphanListFails(FakeTransport):
        def list_dir(self, prefix):
            if prefix == "team/r/review/pr-unk/verdicts/":
                raise TransportError("boom")
            return super().list_dir(prefix)

    t = OrphanListFails()
    t.put("team/r/review/pr-unk/", "")  # dir-only, verdicts listing raises
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=False, verbose=False)
    assert not [e for e in events if e.get("type") == "orphan"], "unknown is not an orphan event"
    assert "verdicts" in failures, "a raised listing must degrade VISIBLY"
    assert state["orphan_slugs"] == [], "an unknown dir must not be cached as seen"


def test_listen_dir_classification_bounded_by_budget(capsys, monkeypatch):
    # Codex P1: the dir-only set is PERMANENT and growing (soft deletes), unlike
    # the my-unsettled-slugs set bounding the source's other listings — so the
    # listener's classification pass must run under its own small time budget
    # (COORD_LISTEN_CLASSIFY_BUDGET). Adversarial: several tombstones + slow
    # listings + a tiny budget -> the tick returns BEFORE visiting them all, the
    # `verdicts` source degrades (existing streak), NO classification knowledge is
    # cached for the unvisited (no ORPHAN events, no orphan_slugs entries — unknown
    # is not classified), and a recovery tick classifies correctly.
    monkeypatch.setenv("COORD_LISTEN_CLASSIFY_BUDGET", "0.05")

    class SlowGhostListings(FakeTransport):
        def __init__(self):
            super().__init__()
            self.slow = True

        def list_dir(self, prefix):
            if self.slow and "/verdicts/" in prefix and "ghost-" in prefix:
                time.sleep(0.03)  # degraded transport: each dir listing crawls
            return super().list_dir(prefix)

    t = SlowGhostListings()
    for i in range(5):
        t.put(f"team/r/review/ghost-{i}/", "")  # permanent soft-delete ghosts
    # the LAST dir (sorted) is a real orphan — only classifiable after recovery
    t.put("team/r/review/ghost-5/verdicts/x.md",
          "---\ntype: Verdict\nreviewer: x\nverdict: approve\n---\n")
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "me", state,
                                            json_mode=False, verbose=False)
    assert "verdicts" in failures, "budget exhaustion must degrade the verdicts source"
    assert not [e for e in events if e.get("type") == "orphan"], \
        "unvisited dirs must emit no ORPHAN events"
    assert state["orphan_slugs"] == [], \
        "no classification knowledge may persist for unvisited dirs"
    # recovery: fast transport -> next tick classifies ALL, finds the real orphan
    t.slow = False
    events2, failures2 = cli._run_listen_tick(t, TEAM, "me", state,
                                              json_mode=False, verbose=False)
    assert [e["slug"] for e in events2 if e.get("type") == "orphan"] == ["ghost-5"]
    assert "verdicts" not in failures2, "recovered tick must not stay degraded"
    assert state["orphan_slugs"] == ["ghost-5"]


def test_pinned_roles_streak_does_not_mask_fresh_inbox_outage(capsys):
    # Review fix (MEDIUM): role-lease-unknown is its OWN degraded source ("roles"),
    # not folded into `inbox` — a chronic role degradation must not pin the inbox
    # streak and mask a fresh summaries outage (the independent-streak invariant).
    class LeaseListFails(FakeTransport):
        def __init__(self):
            super().__init__()
            self.fail_summaries = False

        def read(self, path):
            if self.fail_summaries and path.endswith("summaries.json"):
                raise TransportError("summaries boom")
            return super().read(path)

        def list_dir(self, prefix):
            if prefix.endswith("/leases/"):
                raise TransportError("lease boom")
            return super().list_dir(prefix)

    t = LeaseListFails()
    t.put(cli._role_doc_path(TEAM, "reviewer"),
          "---\ntype: Role\npolicy: shared\nsla_hours: 8760000\n---\n")
    _put_directive(t, "role-do-m", "Review", owner="alice", assignee="reviewer")
    _reconcile(t)
    state = _fresh_state()
    # tick 1: chronic role degradation alarms once, on ITS OWN "roles" streak
    _, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                       json_mode=False, verbose=False)
    err1 = capsys.readouterr().err
    assert "roles" in failures and "inbox" not in failures
    assert state["degraded"]["roles"] is True
    assert state["degraded"]["inbox"] is False
    assert "LISTEN DEGRADED" in err1
    # tick 2: roles STILL degraded (suppressed) AND a fresh summaries outage —
    # the inbox source must still alarm.
    t.fail_summaries = True
    _, failures2 = cli._run_listen_tick(t, TEAM, "bob", state,
                                        json_mode=False, verbose=False)
    err2 = capsys.readouterr().err
    assert "inbox" in failures2
    assert state["degraded"]["inbox"] is True
    assert "LISTEN DEGRADED" in err2 and "summaries index unreadable" in err2
    assert "lease unknown" not in err2, "pinned roles streak must stay suppressed"


# --- Task 2.5: live-freshness overlay in the listen inbox source -----------

def test_listen_overlay_fires_fresh_directive_before_reconcile(capsys):
    """A directive delivered BETWEEN reconciles (task doc present, summaries index
    stale) fires the holder's listen immediately via the freshness overlay — no
    heartbeat rebuild required. Re-tick is quiet (id-diff seen)."""
    t = FakeTransport()
    _put_directive(t, "anchor-0", "Anchor", owner="alice", assignee="bob")
    _reconcile(t)                                  # summaries present, has anchor-0
    state = _fresh_state()
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    capsys.readouterr()                            # anchor consumed
    # fresh directive: task doc written, index NOT rebuilt
    _put_directive(t, "fresh-1", "Fresh work", owner="alice", assignee="bob")
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    out = capsys.readouterr().out
    assert failures == {}
    assert [e["slug"] for e in events] == ["fresh-1"]
    assert "DIRECTIVE fresh-1" in out
    # re-tick: same id, no reconcile -> quiet
    events2, _ = cli._run_listen_tick(t, TEAM, "bob", state,
                                      json_mode=False, verbose=False)
    assert events2 == []


def test_listen_overlay_no_duplicate_after_reconcile(capsys):
    """Once reconcile folds the fresh doc into the index, it lives in BOTH the index
    and the task dir — the overlay skips it (index row wins), so no second fire."""
    t = FakeTransport()
    _reconcile(t)                                  # summaries present (empty)
    state = _fresh_state()
    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    capsys.readouterr()
    # fresh directive surfaced by the overlay
    _put_directive(t, "once-1", "Once", owner="alice", assignee="bob")
    ev1, _ = cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    capsys.readouterr()
    assert [e["slug"] for e in ev1] == ["once-1"]
    # now reconcile folds it into the index; it must NOT re-fire (id seen + no dup row)
    _reconcile(t)
    ev2, _ = cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    assert ev2 == []


def test_listen_overlay_listing_failure_degraded_not_silent(capsys):
    """The overlay task-dir listing raises while the summaries read succeeds: the
    inbox source degrades VISIBLY (never silent) AND the index rows are still served
    — a directive already in the index still fires despite the overlay outage."""
    t = FakeTransport()
    _put_directive(t, "anchor-0", "Anchor", owner="alice", assignee="bob")
    _reconcile(t)                                  # anchor-0 in the index
    orig_list = t.list_dir
    def boom_on_task(prefix):
        if prefix == reconcile.task_prefix(TEAM):
            raise TransportError("overlay boom")
        return orig_list(prefix)
    t.list_dir = boom_on_task
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    err = capsys.readouterr().err
    assert "inbox" in failures                     # degraded, attributed to inbox source
    assert "LISTEN DEGRADED" in err                # not silent
    # honest attribution: the OVERLAY failed, not the summaries index
    assert "task-dir overlay" in err
    assert "summaries index unreadable" not in err
    assert state["degraded"]["inbox"] is True
    assert [e["slug"] for e in events] == ["anchor-0"]   # index rows still served


# --- Codex P1: fail-open holes in _role_fresh_holders ----------------------

def test_role_doc_none_but_listed_degrades_roles_then_recovers(capsys):
    # A role-doc read that returns None for a name PRESENT in the roles/ listing
    # is a transport failure, NOT a non-role: the directive must not be silently
    # unrouted with zero degradation. Roles source degrades; the id must NOT enter
    # state (no false advance); the recovery tick fires the directive.
    class RoleDocReadFails(FakeTransport):
        def __init__(self):
            super().__init__()
            self.fail_doc = True

        def read(self, path):
            if self.fail_doc and path == cli._role_doc_path(TEAM, "reviewer"):
                return None
            return super().read(path)

    t = RoleDocReadFails()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")
    _put_directive(t, "role-do-t", "Review", owner="alice", assignee="reviewer")
    _reconcile(t)
    state = _fresh_state()
    events, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                            json_mode=False, verbose=False)
    capsys.readouterr()
    assert "roles" in failures, "doc-None on a LISTED role must degrade, not non-role"
    assert not [e for e in events if e.get("slug") == "role-do-t"]
    assert "role-do-t" not in state["inbox_ids"], \
        "id must never enter state while holders were unknown"
    # recovery: doc readable again -> the deferred directive fires
    t.fail_doc = False
    events2, failures2 = cli._run_listen_tick(t, TEAM, "bob", state,
                                              json_mode=False, verbose=False)
    assert [e["slug"] for e in events2 if e["type"] == "directive"] == ["role-do-t"]
    assert "role-do-t" in state["inbox_ids"]
    assert "roles" not in failures2


def test_listed_lease_read_none_is_unknown_not_stale():
    # A JUST-LISTED lease shard whose read returns None must not parse as {} and
    # get silently folded out as stale (fail-open vacancy inside the fold).
    class LeaseReadFails(FakeTransport):
        def read(self, path):
            if "/leases/" in path:
                return None
            return super().read(path)

    t = LeaseReadFails()
    _put_role(t, "reviewer")
    _put_lease(t, "reviewer", "bob")  # fresh lease; only its shard READ fails
    holders, ok = cli._role_fresh_holders(t, TEAM, "reviewer", now=NOW)
    assert ok is False, "listed-lease read-None is UNKNOWN, never dropped-as-stale"
    assert holders == []


def test_absent_role_doc_unchanged_nonrole():
    # Genuinely-absent role doc (name not in the roles/ listing): still a plain
    # non-role — the literal-agent-id case must not degrade.
    t = FakeTransport()
    holders, ok = cli._role_fresh_holders(t, TEAM, "just-an-agent", now=NOW)
    assert (holders, ok) == ([], True)


def test_invalid_sla_hours_is_unknown_not_default_sla():
    # Reviewer's exact reproduction. A role doc that PARSES but whose `sla_hours`
    # the operator explicitly got wrong: the window is unknowable, so bob's 36h-old
    # lease can be neither fresh nor stale. Before the fix this folded to
    # ([], True) — a clean, confident "bob is not a holder" with no degradation,
    # off a 24h default nobody asked for.
    t = FakeTransport()
    t.put(cli._role_doc_path(TEAM, "reviewer"),
          "---\ntype: Role\npolicy: shared\nsla_hours: abc\n---\n")
    _put_lease(t, "reviewer", "bob", ts="2026-07-15T12:00:00Z")
    holders, ok = cli._role_fresh_holders(t, TEAM, "reviewer",
                                          now="2026-07-17T00:00:00Z")
    assert (holders, ok) == ([], False), \
        "explicitly-invalid sla_hours is UNKNOWN, never the default"


def test_absent_sla_hours_still_uses_the_default_undegraded():
    # The other half of the distinction, and the one a careless fix breaks: a doc
    # that simply OMITS the optional `sla_hours` is well-formed. It must resolve
    # off the default, undegraded — "unset" is not "unknown".
    t = FakeTransport()
    t.put(cli._role_doc_path(TEAM, "reviewer"), "---\ntype: Role\npolicy: shared\n---\n")
    _put_lease(t, "reviewer", "bob", ts="2026-07-16T23:00:00Z")  # 1h old, fresh at 24h
    assert cli._role_fresh_holders(t, TEAM, "reviewer",
                                   now="2026-07-17T00:00:00Z") == (["bob"], True)
    # blank (`sla_hours:` with no value) is the same statement
    t.put(cli._role_doc_path(TEAM, "reviewer"),
          "---\ntype: Role\npolicy: shared\nsla_hours:\n---\n")
    assert cli._role_fresh_holders(t, TEAM, "reviewer",
                                   now="2026-07-17T00:00:00Z") == (["bob"], True)


def test_invalid_sla_hours_surfaces_role_degraded_in_the_fold(capsys):
    # And the UNKNOWN must reach the READER: `_held_roles_for_rows` puts the role in
    # `unresolved`, so the fold emits a `role-degraded` row instead of serving bob a
    # role-blind queue that looks exactly like "nothing for you".
    t = FakeTransport()
    t.put(cli._role_doc_path(TEAM, "reviewer"),
          "---\ntype: Role\npolicy: shared\nsla_hours: abc\n---\n")
    _put_lease(t, "reviewer", "bob")
    _put_directive(t, "role-do-t", "Review", owner="alice", assignee="reviewer")
    _reconcile(t)
    rows = [{"name": "role-do-t", "status": "proposed", "assignee": "reviewer"}]
    held, unresolved = cli._held_roles_for_rows(t, TEAM, "bob", rows, now=NOW)
    assert (held, unresolved) == (set(), {"reviewer"})
    assert cli._role_degraded_row(unresolved) == {"type": "role-degraded",
                                                  "roles": ["reviewer"]}


def test_foreign_literal_assignee_no_roles_degradation(capsys):
    # A directive assigned to another literal agent (no role doc anywhere) must
    # not produce a roles failure while listening as a third party.
    t = FakeTransport()
    _put_directive(t, "for-carol", "Carol's job", owner="alice", assignee="carol")
    _reconcile(t)
    events, failures = cli._run_listen_tick(t, TEAM, "bob", _fresh_state(),
                                            json_mode=False, verbose=False)
    assert events == [] and failures == {}


# --- v1.6.4 Fix B: orphan/requester-unresolved degrade is emit-once, not per-tick
# A response/verdict dir whose directive|review doc is PERMANENTLY absent (a
# settled/archived/tombstoned directive) must degrade exactly ONCE, then stay
# silent — a fail-closed watcher treats persistent DEGRADED stderr as fatal, so a
# per-tick re-degrade murders it. Recovery (doc reappears) re-arms fail-loud.


def test_owner_unresolved_degrade_emits_once_not_per_tick(capsys):
    # Red before the fix: `orphans` re-entered `failures` (and re-pinned the source)
    # on every tick for a permanently-missing directive doc.
    t = FakeTransport()
    _put_response(t, "gone-1", "20260710T0000-carol", agent="carol", outcome="ACK")
    state = _fresh_state()

    # tick 1: the orphan degrades once and is cached in the flagged set
    _, failures1 = cli._run_listen_tick(t, TEAM, "bob", state,
                                        json_mode=False, verbose=False)
    err1 = capsys.readouterr().err
    assert "orphans" in failures1
    assert err1.count("LISTEN DEGRADED") == 1
    assert state["flagged_orphan_responses"] == ["gone-1"]

    # tick 2: SAME permanently-absent doc -> silent; the source is NOT re-flagged
    # and NOT pinned (so it stays free to alarm a genuinely fresh outage)
    _, failures2 = cli._run_listen_tick(t, TEAM, "bob", state,
                                        json_mode=False, verbose=False)
    err2 = capsys.readouterr().err
    assert "orphans" not in failures2, "a permanent orphan must not re-degrade every tick"
    assert "LISTEN DEGRADED" not in err2
    assert state["degraded"]["orphans"] is False
    assert state["response_keys"] == []  # still no false advance


class _ReviewDocReadNone(FakeTransport):
    """A review `<slug>.md` doc that is LISTED (so the Source-3 loop enters) but
    whose read returns None — models a settled/archived review whose doc is gone
    while its verdicts subtree still lists. ``doc_fail`` toggles for recovery."""

    def __init__(self):
        super().__init__()
        self.doc_fail = True

    def read(self, path):
        if (self.doc_fail and path.startswith("team/r/review/")
                and path.endswith(".md") and "/verdicts/" not in path):
            return None
        return super().read(path)


def test_requester_unresolved_degrade_emits_once_not_per_tick(capsys):
    t = _ReviewDocReadNone()
    _put_review(t, "pr-gone", requested_by="me")   # doc present in the listing
    _put_verdict(t, "pr-gone", "rev", "approve")
    state = _fresh_state()

    _, failures1 = cli._run_listen_tick(t, TEAM, "me", state,
                                        json_mode=False, verbose=False)
    err1 = capsys.readouterr().err
    assert "verdicts" in failures1
    assert err1.count("LISTEN DEGRADED") == 1
    assert state["flagged_orphan_verdicts"] == ["pr-gone"]

    _, failures2 = cli._run_listen_tick(t, TEAM, "me", state,
                                        json_mode=False, verbose=False)
    err2 = capsys.readouterr().err
    assert "verdicts" not in failures2, "a permanent requester-orphan must not re-degrade every tick"
    assert "LISTEN DEGRADED" not in err2
    assert state["degraded"]["verdicts"] is False
    assert state["verdict_keys"] == []   # still no false advance


def test_owner_unresolved_flag_cleared_on_recovery(capsys):
    # Recovery re-arms: a slug flagged on tick 1 whose directive doc becomes
    # readable on tick 2 is processed normally AND discarded from the flagged set,
    # so a genuine future orphaning re-degrades once rather than being suppressed
    # forever by a stale flag.
    t = FakeTransport()
    _put_response(t, "flap-1", "20260710T0000-carol", agent="carol", outcome="ACK")
    state = _fresh_state()

    cli._run_listen_tick(t, TEAM, "bob", state, json_mode=False, verbose=False)
    capsys.readouterr()
    assert state["flagged_orphan_responses"] == ["flap-1"]

    # directive doc appears (owned by bob) -> processed + flag discarded
    _put_directive(t, "flap-1", "Flap", owner="bob", assignee="carol")
    events2, failures2 = cli._run_listen_tick(t, TEAM, "bob", state,
                                              json_mode=False, verbose=False)
    out2 = capsys.readouterr().out
    assert failures2 == {}
    assert [e["slug"] for e in events2] == ["flap-1"]
    assert "RESPONSE flap-1 by carol: ACK" in out2
    assert state["flagged_orphan_responses"] == [], "recovered slug must clear the flagged set"
    assert state["slug_owned"] == {"flap-1": True}


def test_requester_unresolved_flag_cleared_on_recovery(capsys):
    t = _ReviewDocReadNone()
    _put_review(t, "pr-flap", requested_by="me")
    _put_verdict(t, "pr-flap", "rev", "approve")
    state = _fresh_state()

    cli._run_listen_tick(t, TEAM, "me", state, json_mode=False, verbose=False)
    capsys.readouterr()
    assert state["flagged_orphan_verdicts"] == ["pr-flap"]

    # review doc becomes readable -> the verdict fires and the flag is discarded
    t.doc_fail = False
    events2, failures2 = cli._run_listen_tick(t, TEAM, "me", state,
                                              json_mode=False, verbose=False)
    out2 = capsys.readouterr().out
    assert failures2 == {}
    assert [e["type"] for e in events2] == ["verdict"]
    assert "VERDICT pr-flap by rev: approve" in out2
    assert state["flagged_orphan_verdicts"] == [], "recovered requester must clear the flagged set"


def test_response_listing_raise_still_fails_loud_not_emit_once(capsys):
    # Emit-once is ONLY for the None-doc/missing-doc case. A transport ERROR
    # (list_dir raises) on the responses root must still fail loud on its streak —
    # never silenced by the orphan-flag mechanism, and never cached as an orphan.
    t = FakeTransport()
    t.fail_list = True
    state = _fresh_state()
    _, failures = cli._run_listen_tick(t, TEAM, "bob", state,
                                       json_mode=False, verbose=False)
    err = capsys.readouterr().err
    assert "responses" in failures
    assert "LISTEN DEGRADED" in err
    assert state["flagged_orphan_responses"] == [], "a raised listing is not an orphan-flag case"
