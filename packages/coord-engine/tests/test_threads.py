"""Dropped-threads fold + `threads` verb (2026-07-11-dropped-threads-design).

Two layers, per the plan's Task 1:
  * PURE ``threads.classify`` — exhaustively unit-testable over the neutral row
    shape (no transport). Mode precedence is mutually exclusive, first-match-wins:
    intent-carve-out (mode 3) -> blocked-on-principal (mode 2) -> aged-silence
    (mode 1). Overlap + follow-up-suppression contracts are pinned here.
  * The bus ADAPTER + `threads` verb in cli.py — summaries + freshness overlay
    filtered to principal items, per-candidate reads for the signals summaries
    lack (intent_by window, ash-activity attribution), budget + `threads-degraded`
    on any failure. Never crash, never silence.
"""

import json
from datetime import datetime, timedelta, timezone

from coord_engine import cli, threads
from coord_engine_test_helpers import FakeTransport

NOW = "2026-07-11T12:00:00Z"


# --------------------------------------------------------------------------
# neutral-row builder for the PURE classify tests
# --------------------------------------------------------------------------

def _row(**over):
    """A neutral adapter row with inert defaults; override the signals a test
    exercises. Defaults describe an item that classifies to NOTHING (a fresh,
    ash-owned, non-intent, non-blocked item), so every mode a test asserts comes
    from the override, not the scaffolding."""
    row = {
        "id": over.get("id", "t1"),
        "title": over.get("title", "Thread one"),
        "status": "active",
        "tags": [],
        "intent": False,
        "blocked_on_principal": False,
        "blocked_signal": "",
        "parked": False,
        "not_before": None,
        "ash_activity_ts": NOW,          # fresh by default
        "ash_activity_attributed": True,
        "ash_activity_source": "ack shard",
        "declared_window": None,
        "captured_ts": NOW,
        "followup": {"status": "proposed", "responded": False, "followup_ref": None},
    }
    row.update(over)
    return row


def _classify(rows, *, silence_days=3, intent_grace_hours=48, now=NOW):
    return threads.classify(rows, now=now, silence_days=silence_days,
                            intent_grace_hours=intent_grace_hours)


def _iso_days_ago(days, base=NOW):
    b = datetime.fromisoformat(base.replace("Z", "+00:00"))
    return (b - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _iso_hours_ago(hours, base=NOW):
    b = datetime.fromisoformat(base.replace("Z", "+00:00"))
    return (b - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# PURE classify — the three modes
# --------------------------------------------------------------------------

def test_mode1_silent_owned_ages_in():
    # ash-owned, non-intent, last activity 5d ago, silence window 3d -> dropped.
    out = _classify([_row(ash_activity_ts=_iso_days_ago(5))])
    assert len(out) == 1
    assert out[0]["mode"] == 1
    assert out[0]["age"] >= 5.0 - 0.01


def test_mode1_fresh_owned_absent():
    # last activity 1d ago, silence 3d -> not yet dropped.
    assert _classify([_row(ash_activity_ts=_iso_days_ago(1))]) == []


def test_mode2_blocked_on_ash_immediate_no_aging():
    # blocked-on-ash surfaces immediately, regardless of a fresh activity ts.
    out = _classify([_row(blocked_on_principal=True, blocked_signal="assignee: ash",
                          ash_activity_ts=NOW)])
    assert len(out) == 1 and out[0]["mode"] == 2


def test_mode3_intent_past_window():
    # ripe intent (declared window in the past), un-followed-up -> mode 3.
    out = _classify([_row(intent=True, status="proposed",
                          declared_window=_iso_days_ago(1))])
    assert len(out) == 1 and out[0]["mode"] == 3


def test_parked_backlog_and_not_before_excluded():
    parked = _row(id="p", parked=True, ash_activity_ts=_iso_days_ago(9))
    gated = _row(id="g", not_before="2999-01-01T00:00:00Z",
                 ash_activity_ts=_iso_days_ago(9))
    assert _classify([parked, gated]) == []


# --------------------------------------------------------------------------
# PURE classify — the three OVERLAP tests (mandatory, mutually exclusive)
# --------------------------------------------------------------------------

def test_overlap_fresh_intent_absent_entirely():
    # An UNRIPE intent must not surface at all — not mode 1/2 either (the nag bug).
    # Give it every non-mode-3 trigger: aged activity AND blocked-on signal.
    r = _row(intent=True, status="proposed",
             declared_window="2999-01-01T00:00:00Z",  # window not yet reached
             blocked_on_principal=True,
             ash_activity_ts=_iso_days_ago(30))
    assert _classify([r]) == []


def test_overlap_ripe_intent_mode3_only():
    r = _row(intent=True, status="proposed",
             declared_window=_iso_days_ago(2),
             blocked_on_principal=True,           # would be mode 2 if not carved out
             ash_activity_ts=_iso_days_ago(30))   # would be mode 1 if not carved out
    out = _classify([r])
    assert len(out) == 1 and out[0]["mode"] == 3


def test_overlap_ash_assigned_nonintent_aged_is_mode2_with_age():
    # blocked-on-ash dominates aged silence; the age is reported in evidence.
    r = _row(blocked_on_principal=True, blocked_signal="assignee: ash",
             ash_activity_ts=_iso_days_ago(10))
    out = _classify([r])
    assert len(out) == 1 and out[0]["mode"] == 2
    assert "10.0d" in out[0]["evidence"] or "10d" in out[0]["evidence"]
    assert "window" in out[0]["evidence"]  # notes it also exceeds silence


# --------------------------------------------------------------------------
# PURE classify — the three FOLLOW-UP suppression signals (each red-first)
# --------------------------------------------------------------------------

def _ripe_intent(**over):
    base = dict(intent=True, status="proposed", declared_window=_iso_days_ago(2))
    base.update(over)
    return _row(**base)


def test_followup_suppressed_by_status_advanced():
    # (a) status moved past proposed -> suppressed.
    r = _ripe_intent(followup={"status": "active", "responded": False,
                               "followup_ref": None})
    assert _classify([r]) == []


def test_followup_suppressed_by_response_shard():
    # (b) a response shard exists -> suppressed.
    r = _ripe_intent(followup={"status": "proposed", "responded": True,
                               "followup_ref": None})
    assert _classify([r]) == []


def test_followup_suppressed_by_followed_up_by_tag():
    # (c) followed-up-by:<slug> tag -> suppressed.
    r = _ripe_intent(followup={"status": "proposed", "responded": False,
                               "followup_ref": "the-artifact-slug"})
    assert _classify([r]) == []


def test_followup_none_present_stays_mode3():
    r = _ripe_intent(followup={"status": "proposed", "responded": False,
                               "followup_ref": None})
    out = _classify([r])
    assert len(out) == 1 and out[0]["mode"] == 3


# --------------------------------------------------------------------------
# PURE classify — windows (silence-days + intent-grace-hours arithmetic)
# --------------------------------------------------------------------------

def test_silence_days_window_boundary():
    # 2d-old owned item: dropped at silence=1, fresh at silence=3.
    r = _row(ash_activity_ts=_iso_days_ago(2))
    assert [o["mode"] for o in _classify([r], silence_days=1)] == [1]
    assert _classify([r], silence_days=3) == []


def test_intent_grace_hours_when_window_undeclared():
    # Undeclared window -> ripeness = capture + grace. Captured 24h ago:
    # ripe at grace=12h, unripe at grace=48h.
    r = _row(intent=True, status="proposed", declared_window=None,
             captured_ts=_iso_hours_ago(24))
    assert [o["mode"] for o in _classify([r], intent_grace_hours=12)] == [3]
    assert _classify([r], intent_grace_hours=48) == []


def test_mode1_timestamp_fallback_flagged_in_evidence():
    r = _row(ash_activity_ts=_iso_days_ago(6), ash_activity_attributed=False,
             ash_activity_source="item timestamp")
    out = _classify([r])
    assert out[0]["mode"] == 1
    assert "timestamp" in out[0]["evidence"].lower()


def test_classify_grouped_and_oldest_first():
    rows = [
        _row(id="m2", blocked_on_principal=True, blocked_signal="assignee: ash",
             ash_activity_ts=NOW),
        _row(id="m1old", ash_activity_ts=_iso_days_ago(9)),
        _row(id="m1new", ash_activity_ts=_iso_days_ago(4)),
        _row(id="m3", intent=True, status="proposed",
             declared_window=_iso_days_ago(1)),
    ]
    out = _classify(rows)
    modes = [o["mode"] for o in out]
    assert modes == sorted(modes)  # grouped by mode ascending
    m1 = [o for o in out if o["mode"] == 1]
    assert [o["id"] for o in m1] == ["m1old", "m1new"]  # oldest first within a mode


# --------------------------------------------------------------------------
# ADAPTER + `threads` verb (FakeTransport, real path through _load_rows_status)
# --------------------------------------------------------------------------

def _put_task(t, slug, *, title="T", status="active", owner=None, assignee=None,
              tags=None, timestamp=NOW, intent_by=None, not_before=None):
    lines = ["---", "type: Task", f"id: {slug}", f"title: {title}",
             f"status: {status}", "priority: P2", f"timestamp: {timestamp}"]
    if owner is not None:
        lines.append(f"owner: {owner}")
    if assignee is not None:
        lines.append(f"assignee: {assignee}")
    if not_before is not None:
        lines.append(f"not_before: {not_before}")
    if intent_by is not None:
        lines.append(f"intent_by: {intent_by}")
    if tags:
        lines.append("tags:")
        for tag in tags:
            lines.append(f"  - {tag}")
    else:
        lines.append("tags: []")
    lines += ["---", "", f"# {title}", ""]
    t.put(f"team/x/task/{slug}.md", "\n".join(lines), mtime="2026-07-01 04:00PM UTC")


def _reconcile(t):
    assert cli.main(["reconcile", "x"], transport=t) == 0


def _run_threads(t, capsys, *extra):
    capsys.readouterr()  # drain any prior reconcile output
    rc = cli.main(["threads", "x", "--for", "ash", "--json", *extra], transport=t)
    out = capsys.readouterr().out
    return rc, out


def _ancient():
    return "2020-01-01T00:00:00Z"


def _json_objs(out):
    out = out.strip()
    if not out:
        return []
    if out.startswith("["):
        return json.loads(out)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_verb_mode1_owned_aged(capsys):
    t = FakeTransport()
    _put_task(t, "old", owner="ash", timestamp=_ancient())
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    assert rc == 0
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert [o["mode"] for o in objs] == [1]
    assert objs[0]["id"] == "old"


def test_verb_mode2_blocked_on_ash(capsys):
    t = FakeTransport()
    _put_task(t, "blk", assignee="ash", timestamp=NOW)
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert [o["mode"] for o in objs] == [2]


def test_verb_mode3_ripe_intent(capsys):
    t = FakeTransport()
    _put_task(t, "later", assignee="ash", tags=["intent:ash"],
              status="proposed", intent_by=_ancient())
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert [o["mode"] for o in objs] == [3]
    assert objs[0]["id"] == "later"


def test_verb_unripe_intent_absent(capsys):
    t = FakeTransport()
    _put_task(t, "future", assignee="ash", tags=["intent:ash"],
              status="proposed", intent_by="2999-01-01T00:00:00Z")
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert objs == []


def test_verb_intent_suppressed_by_status_advanced(capsys):
    t = FakeTransport()
    _put_task(t, "started", assignee="ash", tags=["intent:ash"],
              status="active", intent_by=_ancient())
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert objs == []


def test_verb_intent_suppressed_by_response_shard(capsys):
    t = FakeTransport()
    _put_task(t, "spoke", assignee="ash", tags=["intent:ash"],
              status="proposed", intent_by=_ancient())
    t.put("team/x/_coord/responses/spoke/20260710-x.md",
          "---\ntype: Response\nagent: helper\noutcome: done\n"
          "timestamp: 2026-07-10T00:00:00Z\n---\nfollowed up\n")
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert objs == []


def test_verb_intent_suppressed_by_followed_up_by_tag(capsys):
    t = FakeTransport()
    _put_task(t, "tagged", assignee="ash",
              tags=["intent:ash", "followed-up-by:the-pr"],
              status="proposed", intent_by=_ancient())
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert objs == []


def test_verb_activity_from_shard_overrides_stale_doc_ts(capsys):
    # An ancient doc ts but a RECENT ash ack shard -> attributed activity is
    # fresh -> NOT dropped. Proves ash-activity attribution reads the shards.
    t = FakeTransport()
    _put_task(t, "touched", owner="ash", timestamp=_ancient())
    ack_key = cli.tasks.agent_key("ash")
    t.put(f"team/x/_coord/acks/touched/{ack_key}.md",
          f"---\ntype: Ack\nagent: ash\ntimestamp: {NOW}\n---\nacked\n")
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert objs == []  # fresh attributed activity -> not a dropped thread


def test_verb_mode1_timestamp_fallback_flagged(capsys):
    t = FakeTransport()
    _put_task(t, "nostamp", owner="ash", timestamp=_ancient())
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert objs[0]["mode"] == 1
    assert "timestamp" in objs[0]["evidence"].lower()


def test_verb_non_principal_items_ignored(capsys):
    t = FakeTransport()
    _put_task(t, "bob", owner="bob", assignee="bob", timestamp=_ancient())
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert objs == []


def test_verb_json_shape_pinned(capsys):
    t = FakeTransport()
    _put_task(t, "old", owner="ash", timestamp=_ancient())
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert objs, "expected at least one thread"
    for o in objs:
        assert set(o.keys()) == {"mode", "id", "title", "age", "window", "evidence"}


def test_verb_degraded_on_summaries_failure(capsys):
    # No reconcile: summaries.json absent AND the parent listing raises -> the
    # index is UNKNOWN (not confirmed-empty). A `threads-degraded` row surfaces;
    # never a silent empty, never a crash.
    t = FakeTransport()
    _put_task(t, "old", owner="ash", timestamp=_ancient())
    t.store["team/x/_coord/summaries.json"] = "{ this is not json"
    t.fail_list = True
    rc = cli.main(["threads", "x", "--for", "ash", "--json"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0  # never crash
    assert any(o.get("type") == "threads-degraded" for o in _json_objs(out))


def test_verb_text_render_grouped(capsys):
    t = FakeTransport()
    _put_task(t, "old", owner="ash", timestamp=_ancient())
    _put_task(t, "blk", assignee="ash", timestamp=NOW)
    _reconcile(t)
    rc = cli.main(["threads", "x", "--for", "ash"], transport=t)
    out = capsys.readouterr().out
    assert rc == 0
    assert "old" in out and "blk" in out


def test_verb_silence_days_flag_and_env(capsys, monkeypatch):
    t = FakeTransport()
    two_days = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    _put_task(t, "recent", owner="ash", timestamp=two_days)
    _reconcile(t)
    # default silence 3d -> a 2d-old item is fresh -> absent.
    _, out = _run_threads(t, capsys)
    assert [o for o in _json_objs(out) if o.get("type") != "threads-degraded"] == []
    # --silence-days 1 -> now dropped.
    _, out = _run_threads(t, capsys, "--silence-days", "1")
    assert [o["mode"] for o in _json_objs(out) if o.get("type") != "threads-degraded"] == [1]
    # env override behaves the same.
    monkeypatch.setenv("COORD_THREADS_SILENCE_DAYS", "1")
    _, out = _run_threads(t, capsys)
    assert [o["mode"] for o in _json_objs(out) if o.get("type") != "threads-degraded"] == [1]
