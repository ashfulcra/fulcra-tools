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
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

NOW = "2026-07-11T12:00:00Z"


class _FailReadTransport(FakeTransport):
    """FakeTransport whose ``read`` RAISES for paths in ``fail_reads`` — the
    transport-exception leg of the unreadable-intent-window contract."""

    def __init__(self):
        super().__init__()
        self.fail_reads: set[str] = set()

    def read(self, path):
        if path in self.fail_reads:
            raise TransportError("boom")
        return super().read(path)


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


# --------------------------------------------------------------------------
# BLOCKING (review r1): an unreadable intent window must NEVER silently fall
# back to capture+grace — that manufactures a false mode-3 drop with
# degraded:False (the nagging-sensitive failure the spec forbids verbatim).
# Contract: read MISSES (raise OR None) -> ripeness UNKNOWN -> the intent is
# excluded from this pass AND a threads-degraded row surfaces; it returns,
# correctly windowed, when readable again. A doc that reads fine but genuinely
# lacks intent_by stays the legitimate capture+grace fallback.
# --------------------------------------------------------------------------

def test_verb_intent_window_read_raises_degraded_not_surfaced(capsys):
    # Reviewer repro: intent_by 2999 (unripe) + doc read RAISES. Without the fix
    # the missing window silently falls back to capture+grace and the ancient
    # capture ts surfaces it as mode 3 with no degraded row.
    t = _FailReadTransport()
    _put_task(t, "future", assignee="ash", tags=["intent:ash"],
              status="proposed", timestamp=_ancient(),
              intent_by="2999-01-01T00:00:00Z")
    _reconcile(t)
    t.fail_reads.add("team/x/task/future.md")
    rc, out = _run_threads(t, capsys)
    assert rc == 0  # never crash
    objs = _json_objs(out)
    assert [o for o in objs if o.get("type") != "threads-degraded"] == []
    degraded = [o for o in objs if o.get("type") == "threads-degraded"]
    assert degraded and "intent window unreadable" in degraded[0]["reason"]
    assert "future" in degraded[0]["reason"]
    # RECOVERY: doc readable again -> real window honored (2999 unripe -> absent,
    # and the degraded row is gone).
    t.fail_reads.clear()
    rc, out = _run_threads(t, capsys)
    assert _json_objs(out) == []


def test_verb_intent_window_read_none_degraded_then_recovers(capsys):
    # The None leg: doc listed in summaries but read returns None. Ripeness is
    # UNKNOWN -> excluded + degraded. On recovery the REAL (past) window makes
    # it mode 3.
    t = FakeTransport()
    _put_task(t, "later", assignee="ash", tags=["intent:ash"],
              status="proposed", timestamp=_ancient(), intent_by=_ancient())
    _reconcile(t)
    doc = t.store.pop("team/x/task/later.md")
    rc, out = _run_threads(t, capsys)
    assert rc == 0
    objs = _json_objs(out)
    assert [o for o in objs if o.get("type") != "threads-degraded"] == []
    assert any(o.get("type") == "threads-degraded"
               and "intent window unreadable" in o.get("reason", "")
               for o in objs)
    # RECOVERY: readable again -> surfaces with its real, ripe window.
    t.store["team/x/task/later.md"] = doc
    rc, out = _run_threads(t, capsys)
    objs = _json_objs(out)
    assert [o["mode"] for o in objs if o.get("type") != "threads-degraded"] == [3]
    assert not any(o.get("type") == "threads-degraded" for o in objs)


def test_verb_intent_genuinely_undeclared_window_grace_fallback_stands(capsys):
    # A doc that READS FINE but lacks intent_by is legitimately undeclared:
    # capture+grace fallback stands (ancient capture -> ripe -> mode 3, clean).
    t = FakeTransport()
    _put_task(t, "nodate", assignee="ash", tags=["intent:ash"],
              status="proposed", timestamp=_ancient())
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = _json_objs(out)
    assert [o["mode"] for o in objs if o.get("type") != "threads-degraded"] == [3]
    assert not any(o.get("type") == "threads-degraded" for o in objs)


# --------------------------------------------------------------------------
# review r1 should-fix test gaps
# --------------------------------------------------------------------------

def test_classify_terminal_status_excluded():
    # (1a) pure layer: a closed item is never a dropped thread — not mode 1
    # (aged silence) and not mode 2 (blocked signals on a done/abandoned item).
    aged_done = _row(id="d", status="done", ash_activity_ts=_iso_days_ago(30))
    blocked_abandoned = _row(id="a", status="abandoned", blocked_on_principal=True,
                             blocked_signal="assignee: ash")
    assert _classify([aged_done, blocked_abandoned]) == []


def test_verb_terminal_status_excluded(capsys):
    # (1b) adapter layer: same exclusion end-to-end.
    t = FakeTransport()
    _put_task(t, "shipped", owner="ash", status="done", timestamp=_ancient())
    _put_task(t, "dropped-for-good", assignee="ash", status="abandoned",
              timestamp=_ancient())
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    assert [o for o in _json_objs(out) if o.get("type") != "threads-degraded"] == []


def test_verb_text_degraded_on_stderr_list_on_stdout(capsys):
    # (2) text-mode degraded output pinned: the notice goes to STDERR, stdout
    # stays the clean thread list (here the empty-state line).
    t = FakeTransport()
    _put_task(t, "old", owner="ash", timestamp=_ancient())
    t.store["team/x/_coord/summaries.json"] = "{ this is not json"
    t.fail_list = True
    capsys.readouterr()
    rc = cli.main(["threads", "x", "--for", "ash"], transport=t)
    cap = capsys.readouterr()
    assert rc == 0
    assert "threads degraded" in cap.err
    assert "degraded" not in cap.out
    assert "nothing dropped" in cap.out


def test_verb_fresh_intent_visible_via_overlay(capsys):
    # (3) an intent written AFTER the last reconcile (absent from summaries) is
    # visible through the freshness overlay — no reconcile needed.
    t = FakeTransport()
    _put_task(t, "seed", owner="bob", assignee="bob")
    _reconcile(t)
    _put_task(t, "fresh-intent", assignee="ash", tags=["intent:ash"],
              status="proposed", timestamp=_ancient(), intent_by=_ancient())
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert [(o["mode"], o["id"]) for o in objs] == [(3, "fresh-intent")]


def test_verb_needs_human_block_is_mode2(capsys):
    # (4) the needs:human + principal signal (spec mode 2, third signal) pinned.
    t = FakeTransport()
    _put_task(t, "ask", owner="ash", status="blocked", tags=["needs:human"],
              timestamp=NOW)
    _reconcile(t)
    rc, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert [o["mode"] for o in objs] == [2]
    assert "needs:human" in objs[0]["evidence"]


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


# --------------------------------------------------------------------------
# `intent` capture verb (Task 2) — FakeTransport, real directive-delivery path.
#   Identity deviation: text + assignee ONLY (intent_by EXCLUDED from identity,
#   given a VERIFIED in-place update path). Restatement never forks a 2nd item.
# --------------------------------------------------------------------------

PAST = "2020-01-01T00:00:00Z"       # a ripe intent window
FUTURE = "2999-01-01T00:00:00Z"     # an unripe intent window
FUTURE2 = "2998-06-01T00:00:00Z"    # a DIFFERENT unripe window (for update tests)


class _LosingWriteTransport(FakeTransport):
    """``write`` claims success but stores nothing (silent write loss) — proves
    the update path's read-back verification catches a write that did not land
    and refuses to claim it, leaving the original window intact."""

    def write(self, path, content):
        return True  # lie: nothing persisted


class _BlindReadTransport(FakeTransport):
    """``read`` always returns None while ``list_dir`` still lists the slot —
    the present-but-unreadable case (I1): the verb must NOT overwrite a slot it
    cannot read."""

    def read(self, path):
        return None


def _intent(t, text, *extra, principal="ash"):
    return cli.main(["intent", "x", text, "--for", principal, *extra], transport=t)


def _task_docs(t):
    # Only real task docs — exclude reconcile's engine-owned index.md / log.md.
    return sorted(p for p in t.store
                  if p.startswith("team/x/task/") and p.endswith(".md")
                  and p.rsplit("/", 1)[1] not in ("index.md", "log.md"))


def test_intent_capture_round_trip(capsys):
    # Capture through the real delivery path; a ripe (past-window) intent then
    # surfaces as mode 3 through Task 1's fold. Doc carries the capture doctrine:
    # intent:<principal> tag + assignee + intent_by.
    t = FakeTransport()
    rc = _intent(t, "enumerate the tools list", "--by", PAST)
    assert rc == 0
    docs = _task_docs(t)
    assert len(docs) == 1
    doc = t.store[docs[0]]
    assert "intent:ash" in doc
    assert "assignee: ash" in doc
    assert f"intent_by: {PAST}" in doc
    capsys.readouterr()
    _reconcile(t)
    rc2, out = _run_threads(t, capsys)
    assert rc2 == 0
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert [o["mode"] for o in objs] == [3]


def test_intent_identical_restatement_dedupes(capsys):
    # Same text + same window -> pure dedup: rc 0, "already captured", NO fork.
    t = FakeTransport()
    _intent(t, "ship the demo", "--by", FUTURE)
    before = _task_docs(t)
    capsys.readouterr()
    rc = _intent(t, "ship the demo", "--by", FUTURE)
    out = capsys.readouterr().out
    assert rc == 0
    assert "already captured" in out
    assert _task_docs(t) == before  # one item, never forked


def test_intent_restatement_without_by_dedupes(capsys):
    # Restating with NO --by is dedup, not a window wipe: rc 0, window preserved.
    t = FakeTransport()
    _intent(t, "ship the demo", "--by", FUTURE)
    before = _task_docs(t)
    capsys.readouterr()
    rc = _intent(t, "ship the demo")
    out = capsys.readouterr().out
    assert rc == 0
    assert "already captured" in out
    assert _task_docs(t) == before
    assert f"intent_by: {FUTURE}" in t.store[before[0]]  # window intact


def test_intent_different_by_updates_window_in_place(capsys):
    # A revised deadline updates the SAME doc in place (no fork), read-back verified.
    t = FakeTransport()
    _intent(t, "ship the demo", "--by", FUTURE)
    before = _task_docs(t)
    capsys.readouterr()
    rc = _intent(t, "ship the demo", "--by", FUTURE2)
    out = capsys.readouterr().out
    assert rc == 0
    assert "window updated" in out
    after = _task_docs(t)
    assert after == before  # same single doc
    doc = t.store[after[0]]
    assert f"intent_by: {FUTURE2}" in doc
    assert FUTURE not in doc  # stale deadline gone (the false-drop case)
    # identity fields untouched -> a later identical restatement STILL dedupes.
    capsys.readouterr()
    rc2 = _intent(t, "ship the demo", "--by", FUTURE2)
    assert rc2 == 0
    assert "already captured" in capsys.readouterr().out
    assert _task_docs(t) == before


def test_intent_window_update_read_back_verified(capsys):
    # Update path writes, then reads back and compares intent_by. A silently-lost
    # write (read-back still shows the OLD window) -> rc 1 unverifiable, retry;
    # the original window is NEVER left in a claimed-but-false state.
    t = FakeTransport()
    _intent(t, "ship the demo", "--by", FUTURE)
    lt = _LosingWriteTransport()
    lt.store = dict(t.store)
    lt.mtimes = dict(t.mtimes)
    capsys.readouterr()
    rc = _intent(lt, "ship the demo", "--by", FUTURE2)
    err = capsys.readouterr().err
    assert rc == 1
    assert "retry" in err.lower()
    # No overwrite claimed: the persisted doc still holds the ORIGINAL window.
    doc = lt.store[_task_docs(lt)[0]]
    assert f"intent_by: {FUTURE}" in doc
    assert FUTURE2 not in doc


def test_intent_unreadable_slot_no_overwrite(capsys):
    # A present-but-unreadable slot (read None, listing shows it) must NOT be
    # overwritten: rc 1 cannot-verify, store unchanged (I1 never-write-blind).
    t = FakeTransport()
    _intent(t, "ship the demo", "--by", FUTURE)
    bt = _BlindReadTransport()
    bt.store = dict(t.store)
    bt.mtimes = dict(t.mtimes)
    snapshot = dict(bt.store)
    capsys.readouterr()
    rc = _intent(bt, "ship the demo", "--by", FUTURE2)
    err = capsys.readouterr().err
    assert rc == 1
    assert "retry" in err.lower()
    assert bt.store == snapshot  # never overwrote the slot it could not read


def test_intent_ripe_unripe_interplay_with_fold(capsys):
    # Integration through the REAL fold: a future-window intent is invisible;
    # updating the window into the past makes the SAME item surface as mode 3.
    t = FakeTransport()
    _intent(t, "wire up the adapter", "--by", FUTURE)
    _reconcile(t)
    _, out = _run_threads(t, capsys)
    assert [o for o in _json_objs(out) if o.get("type") != "threads-degraded"] == []
    # revise the deadline into the past -> in-place update -> now ripe.
    _intent(t, "wire up the adapter", "--by", PAST)
    _reconcile(t)
    _, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert [o["mode"] for o in objs] == [3]
    assert len(_task_docs(t)) == 1  # updated in place, never forked


def test_intent_undeclared_window_ripens_via_capture_grace(capsys, monkeypatch):
    # No --by -> NO intent_by written -> the fold windows it by capture+grace.
    # Time-travel proves the ripening: unripe within the grace, mode 3 past it.
    t = FakeTransport()
    captured_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(cli, "_now", lambda: captured_at)
    rc = _intent(t, "follow up on that list")
    assert rc == 0
    assert "intent_by" not in t.store[_task_docs(t)[0]]  # genuinely undeclared
    _reconcile(t)
    # +24h, default 48h grace -> still unripe -> absent.
    monkeypatch.setattr(cli, "_now", lambda: captured_at + timedelta(hours=24))
    _, out = _run_threads(t, capsys)
    assert [o for o in _json_objs(out) if o.get("type") != "threads-degraded"] == []
    # +72h, past the 48h grace -> ripe -> mode 3.
    monkeypatch.setattr(cli, "_now", lambda: captured_at + timedelta(hours=72))
    _, out = _run_threads(t, capsys)
    objs = [o for o in _json_objs(out) if o.get("type") != "threads-degraded"]
    assert [o["mode"] for o in objs] == [3]


def test_intent_unparseable_by_rc1(capsys):
    # An unparseable --by fails loud (rc 1), never captures a windowless-but-meant-
    # -to-be-windowed intent.
    t = FakeTransport()
    capsys.readouterr()
    rc = _intent(t, "do the thing", "--by", "whenever-ish")
    err = capsys.readouterr().err
    assert rc == 1
    assert "--by" in err
    assert _task_docs(t) == []  # nothing written
