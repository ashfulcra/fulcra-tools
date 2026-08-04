"""The annotation read side: folds consume the summaries.json projection.

Reconcile (the write side) folds review verdict state and forge
responsibility/feedback into ``_coord/summaries.json`` (``projection.py``); the
review/forge wake folds then answer from ONE summaries read when the projection
is FRESH, and fall back to the raw scan — LOUDLY — when it is stale, incomplete,
or unrecognized. A team whose aggregate was never projected behaves exactly as
before. These tests pin all four legs plus the end-to-end state agreement
between the projection and the raw per-slug tally.
"""

import json
from datetime import datetime, timedelta, timezone

from coord_engine import budget, cli, projection, reconcile
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

TEAM = "r"


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _reconcile(t, now=None):
    now = now or _now_iso()
    return reconcile.reconcile(t, TEAM, now=now, today=now[:10], host="h")


def _agg(t):
    return json.loads(t.store[f"team/{TEAM}/_coord/summaries.json"])


def _put_review(t, slug, required, verdicts=(), requested_by=None, of=None):
    fm = [f"type: Review", f"required: {required}"]
    if requested_by:
        fm.append(f"requested_by: {requested_by}")
    if of:
        fm.append(f"of: {of}")
    t.put(f"team/{TEAM}/review/{slug}.md", "---\n" + "\n".join(fm) + "\n---\n")
    for who, v in verdicts:
        t.put(f"team/{TEAM}/review/{slug}/verdicts/{who}.md",
              f"---\ntype: Verdict\nreviewer: {who}\nverdict: {v}\n---\n")


class CountingTransport(FakeTransport):
    """Counts list/read ops per path so 'zero raw review scans' is a fact."""

    def __init__(self):
        super().__init__()
        self.listed: list[str] = []
        self.reads: list[str] = []
        self.updates_result = []
        self.updates_calls: list[tuple[str, str | None]] = []

    def list_dir(self, prefix):
        self.listed.append(prefix)
        return super().list_dir(prefix)

    def read(self, path):
        self.reads.append(path)
        return super().read(path)

    def updates(self, since, *, team=None):
        self.updates_calls.append((since, team))
        return self.updates_result

    def reset_counts(self):
        self.listed, self.reads, self.updates_calls = [], [], []


# --- write side: reconcile builds the sections -------------------------------

def test_reconcile_writes_review_projection():
    t = FakeTransport()
    _put_review(t, "open-one", "alice", requested_by="bob",
                of="https://github.com/o/r/pull/7")
    _put_review(t, "settled-one", "alice", verdicts=[("alice", "approve")])
    _put_review(t, "changed-one", "alice", verdicts=[("alice", "changes")])
    _reconcile(t)
    sec = _agg(t)[projection.REVIEWS_KEY]
    assert sec["schema"] == projection.REVIEWS_SCHEMA
    assert sec["complete"] is True
    by_name = {r["name"]: r for r in sec["rows"]}
    assert by_name["open-one"]["state"] == "PENDING"
    assert by_name["open-one"]["pending_required"] == ["alice"]
    assert by_name["open-one"]["requested_by"] == "bob"
    assert by_name["open-one"]["artifact"] == "o-r-7"
    assert by_name["settled-one"]["state"] == "APPROVED"
    assert by_name["settled-one"]["settled"] is True
    assert by_name["changed-one"]["state"] == "CHANGES"
    # a proven settle also drops the read fold's settled-cache marker
    assert f"team/{TEAM}/review/settled-one/verdicts/.settled" in t.store


def test_reconcile_writes_forge_projection():
    t = FakeTransport()
    url = "https://github.com/o/r/pull/9"
    t.put(f"team/{TEAM}/_coord/forge/watch/o-r-9.md",
          f"---\ntype: Watch\nurl: {url}\nagent: bob\n---\n")
    t.put(f"team/{TEAM}/_coord/forge/feedback/o-r-9/review-1.md",
          "---\ntype: ForgeFeedback\nauthor: rev\n---\nfix\n")
    _put_review(t, "pr-review", "alice", requested_by="carol", of=url)
    _reconcile(t)
    sec = _agg(t)[projection.FORGE_KEY]
    assert sec["schema"] == projection.FORGE_SCHEMA
    assert sec["complete"] is True
    # watch agent UNION the review's requested_by, keyed by PR slug
    assert sec["responsible"]["o-r-9"] == ["bob", "carol"]
    assert sec["feedback"]["o-r-9"] == [{"id": "review-1", "author": "rev"}]


def test_reconcile_review_projection_classifies_orphans_and_tombstones():
    t = FakeTransport()
    t.put(f"team/{TEAM}/review/ghost/verdicts/{projection.SETTLED_MARKER}", "x")
    t.put(f"team/{TEAM}/review/orphaned/verdicts/alice.md",
          "---\ntype: Verdict\nreviewer: alice\nverdict: approve\n---\n")
    _reconcile(t)
    sec = _agg(t)[projection.REVIEWS_KEY]
    assert sec["orphans"] == ["orphaned"]
    assert sec["tombstones"] == ["ghost"]
    # tombstones are cached: the next pass pays no listing for the ghost dir
    ct = CountingTransport()
    ct.store, ct.mtimes, ct.sizes = t.store, t.mtimes, t.sizes
    _reconcile(ct)
    assert f"team/{TEAM}/review/ghost/verdicts/" not in ct.listed
    assert json.loads(ct.store[f"team/{TEAM}/_coord/summaries.json"])[
        projection.REVIEWS_KEY]["tombstones"] == ["ghost"]


def test_reconcile_settled_rows_carry_without_rereads():
    t = CountingTransport()
    _put_review(t, "done-a", "alice", verdicts=[("alice", "approve")])
    _reconcile(t)
    t.reset_counts()
    _reconcile(t)
    # the settled row carries on the root listing's mtime+size alone: no verdicts
    # listing, no doc read, no shard read for the settled slug
    assert f"team/{TEAM}/review/done-a/verdicts/" not in t.listed
    assert f"team/{TEAM}/review/done-a.md" not in t.reads
    sec = _agg(t)[projection.REVIEWS_KEY]
    assert sec["complete"] is True
    assert sec["rows"][0]["settled"] is True


def test_reconcile_budget_cut_marks_projection_incomplete():
    t = FakeTransport()
    for i in range(3):
        _put_review(t, f"open-{i}", "alice")
    dead = budget.Deadline(0.0)  # already expired
    sec = projection.build_review_projection(
        t, TEAM, now=_now_iso(), prior=None, settled_index=set(),
        deadline=dead)
    assert sec["complete"] is False
    assert sec["scanned"] == 0 and sec["total"] == 3
    forge_sec = projection.build_forge_projection(
        t, TEAM, now=_now_iso(), review_rows=sec["rows"],
        reviews_complete=False, prior=None, deadline=dead)
    assert forge_sec["complete"] is False  # a review floor floors forge too


def test_reconcile_unreadable_verdict_shard_is_unknown_not_frozen():
    class ShardHidingTransport(FakeTransport):
        def read(self, path):
            if path.endswith("/verdicts/bob.md"):
                return None  # listed but unreadable: a floor, never projected
            return super().read(path)

    t = ShardHidingTransport()
    _put_review(t, "flaky", "bob", verdicts=[("bob", "changes")])
    _reconcile(t)
    sec = _agg(t)[projection.REVIEWS_KEY]
    assert sec["complete"] is False
    assert sec["rows"] == []  # no row is better than a false APPROVED/PENDING


def test_fast_path_declines_on_review_change():
    # a verdict written between passes must decline the fast path, or the
    # projection would keep its stamp while claiming to cover moved state
    t = FakeTransport()
    t.put(f"team/{TEAM}/task/a.md",
          "---\ntype: Task\ntitle: A\nstatus: active\n---\n")
    now = _now_iso()
    _reconcile(t, now=now)
    changes = [{"path": f"team/{TEAM}/review/pr-1/verdicts/alice.md",
                "state": "uploaded", "uploaded_at": now}]
    t.updates = lambda period, team=None: changes
    res = _reconcile(t, now=_iso(datetime.now(timezone.utc) + timedelta(minutes=5)))
    assert not res.get("fast_path")


# --- freshness gate ----------------------------------------------------------

def _fresh_reviews_section(**over):
    sec = {"schema": projection.REVIEWS_SCHEMA, "generated_at": _now_iso(),
           "complete": True, "scanned": 1, "total": 1, "rows": [],
           "orphans": [], "orphans_unknown": [], "tombstones": []}
    sec.update(over)
    return sec


def test_fresh_section_absent_is_silent():
    got, reason = projection.fresh_section(
        {"rows": []}, projection.REVIEWS_KEY, projection.REVIEWS_SCHEMA,
        now=_now_iso())
    assert got is None and reason == ""


def test_fresh_section_serves_fresh_complete():
    doc = {projection.REVIEWS_KEY: _fresh_reviews_section()}
    got, reason = projection.fresh_section(
        doc, projection.REVIEWS_KEY, projection.REVIEWS_SCHEMA, now=_now_iso())
    assert got is doc[projection.REVIEWS_KEY] and reason == ""


def test_fresh_section_stale_is_loud():
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
    doc = {projection.REVIEWS_KEY: _fresh_reviews_section(generated_at=old)}
    got, reason = projection.fresh_section(
        doc, projection.REVIEWS_KEY, projection.REVIEWS_SCHEMA, now=_now_iso())
    assert got is None and "stale" in reason and "48" in reason


def test_fresh_section_incomplete_unrecognized_unstamped_are_loud():
    for over, needle in (
        ({"complete": False, "scanned": 2, "total": 5}, "incomplete (scanned 2/5)"),
        ({"schema": "coord.reviews.projection.v999"}, "unrecognized"),
        ({"generated_at": "not-a-time"}, "stamp unreadable"),
    ):
        doc = {projection.REVIEWS_KEY: _fresh_reviews_section(**over)}
        got, reason = projection.fresh_section(
            doc, projection.REVIEWS_KEY, projection.REVIEWS_SCHEMA,
            now=_now_iso())
        assert got is None and needle in reason, (over, reason)


def test_fresh_section_threshold_env(monkeypatch):
    monkeypatch.setenv("COORD_PROJECTION_MAX_AGE_HOURS", "72")
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
    doc = {projection.REVIEWS_KEY: _fresh_reviews_section(generated_at=old)}
    got, _ = projection.fresh_section(
        doc, projection.REVIEWS_KEY, projection.REVIEWS_SCHEMA, now=_now_iso())
    assert got is not None  # 48h old is fresh under a 72h threshold


# --- read side: the review fold ---------------------------------------------

def test_review_fold_consumes_fresh_projection_without_scanning():
    t = CountingTransport()
    _put_review(t, "pr-1", "alice")
    _put_review(t, "pr-2", "alice", verdicts=[("alice", "approve")])
    _reconcile(t)
    agg = _agg(t)
    t.reset_counts()
    out = cli._pending_reviews_for(t, TEAM, "alice", rows=[], aggregate_doc=agg)
    pend = [r for r in out if r.get("type") == "review-pending"]
    assert [r["name"] for r in pend] == ["pr-1"]
    assert pend[0]["pending_required"] == ["alice"]
    src = [r for r in out if r.get("type") == "review-source"]
    assert src == [{"type": "review-source", "source": "projection",
                    "as_of": agg[projection.REVIEWS_KEY]["generated_at"]}]
    # THE point: zero raw review-namespace transport ops on a projection hit
    assert not [p for p in t.listed if "/review/" in p]
    assert not [p for p in t.reads if "/review/" in p]


def test_review_fold_stale_projection_falls_back_loudly():
    t = CountingTransport()
    _put_review(t, "pr-1", "alice")
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
    _reconcile(t, now=old)  # projection stamped two days ago
    agg = _agg(t)
    t.reset_counts()
    out = cli._pending_reviews_for(t, TEAM, "alice", rows=[], aggregate_doc=agg)
    # the raw scan really ran (the review root was listed) ...
    assert f"team/{TEAM}/review/" in t.listed
    assert [r["name"] for r in out if r.get("type") == "review-pending"] == ["pr-1"]
    # ... and said so, naming staleness — never silently serving the old stamp
    src = [r for r in out if r.get("type") == "review-source"]
    assert len(src) == 1 and src[0]["source"] == "raw-scan"
    assert "stale" in src[0]["reason"]


def test_review_fold_missing_projection_behaves_exactly_as_today():
    t = FakeTransport()
    _put_review(t, "pr-1", "alice")
    # aggregate exists but carries no projection (an un-upgraded reconcile)
    agg = {"schema": "coord.teams.summaries.v1", "rows": []}
    out = cli._pending_reviews_for(t, TEAM, "alice", rows=[], aggregate_doc=agg)
    legacy = cli._pending_reviews_for(t, TEAM, "alice", rows=[])
    assert out == legacy  # no source row, no behavior change
    assert not any(r.get("type") == "review-source" for r in out)


def test_review_fold_incomplete_projection_falls_back_loudly():
    t = FakeTransport()
    _put_review(t, "pr-1", "alice")
    agg = {projection.REVIEWS_KEY: _fresh_reviews_section(
        complete=False, scanned=0, total=1)}
    out = cli._pending_reviews_for(t, TEAM, "alice", rows=[], aggregate_doc=agg)
    src = [r for r in out if r.get("type") == "review-source"]
    assert len(src) == 1 and src[0]["source"] == "raw-scan"
    assert "incomplete" in src[0]["reason"]
    assert [r["name"] for r in out if r.get("type") == "review-pending"] == ["pr-1"]


def test_review_fold_projection_surfaces_orphans_and_role_routing():
    from coord_engine.tasks import agent_key
    t = FakeTransport()
    _put_review(t, "pr-role", "codex-reviewer")
    t.put(f"team/{TEAM}/review/orphaned/verdicts/alice.md",
          "---\ntype: Verdict\nreviewer: alice\nverdict: approve\n---\n")
    t.put(f"team/{TEAM}/roles/codex-reviewer.md",
          "---\ntype: Role\npolicy: shared\n---\n")
    t.put(f"team/{TEAM}/roles/codex-reviewer/leases/{agent_key('holder')}.md",
          f"---\ntype: Lease\nagent: holder\ntimestamp: {_now_iso()}\n---\n")
    _reconcile(t)
    out = cli._pending_reviews_for(t, TEAM, "holder", rows=[],
                                   aggregate_doc=_agg(t))
    assert [r["name"] for r in out if r.get("type") == "review-pending"] == ["pr-role"]
    assert [r["name"] for r in out if r.get("type") == "review-orphan"] == ["orphaned"]
    assert any(r.get("type") == "review-source" and r["source"] == "projection"
               for r in out)


def test_review_fold_head_slug_newer_than_projection_is_raw_scanned():
    # a review requested AFTER the projection stamp is not in it; the caller's
    # own head obligation must not wait for the next reconcile
    t = FakeTransport()
    _reconcile(t)  # projection over an empty review tree
    agg = _agg(t)
    _put_review(t, "fresh-pr", "alice")  # arrives after the stamp
    rows = [{"id": "rr-fresh-pr", "name": "rr-fresh-pr",
             "title": "REVIEW REQUEST: fresh-pr", "status": "proposed",
             "assignee": "alice", "priority": "P2", "tags": []}]
    out = cli._pending_reviews_for(t, TEAM, "alice", rows=rows,
                                   aggregate_doc=agg)
    assert [r["name"] for r in out if r.get("type") == "review-pending"] == ["fresh-pr"]


def test_review_fold_head_slug_covered_by_stale_settled_row_still_surfaces():
    # Round-2 P0 regression (reconcile/write race): reconcile carried a settled
    # row, then — after the stamp — the head advanced (.settled cleared, the
    # review doc rewritten head-keyed) and the caller's open review-request
    # directive landed. The slug IS in the fresh complete:true section, but the
    # caller-owned open directive is authoritative head coverage: the fold must
    # raw-tally it and surface the pending review, never serve the stale
    # "settled" row for the caller's own obligation.
    t = FakeTransport()
    _put_review(t, "pr-race", "alice", verdicts=[("alice", "approve")])
    _reconcile(t)
    agg = _agg(t)  # fresh, complete:true, pr-race carried as settled
    assert [r["settled"] for r in agg[projection.REVIEWS_KEY]["rows"]] == [True]
    head = "a" * 40
    t.delete(f"team/{TEAM}/review/pr-race/verdicts/{projection.SETTLED_MARKER}")
    t.put(f"team/{TEAM}/review/pr-race.md",
          f"---\ntype: Review\nrequired: alice\nhead: {head}\n---\n")
    rows = [{"id": "rr-race", "name": "rr-race",
             "title": "REVIEW REQUEST: pr-race", "status": "proposed",
             "assignee": "alice", "priority": "P2", "tags": []}]
    out = cli._pending_reviews_for(t, TEAM, "alice", rows=rows,
                                   aggregate_doc=agg)
    assert [r["name"] for r in out
            if r.get("type") == "review-pending"] == ["pr-race"]
    # still a projection hit for the tail — only the head went to the raw tally
    assert any(r.get("type") == "review-source"
               and r["source"] == "projection" for r in out)


def test_review_fold_head_slug_in_projection_not_double_emitted():
    # a head slug the projection also carries as PENDING must surface exactly
    # once (the raw head tally answers it; the projected row is skipped)
    t = FakeTransport()
    _put_review(t, "pr-mine", "alice")
    _reconcile(t)
    rows = [{"id": "rr-mine", "name": "rr-mine",
             "title": "REVIEW REQUEST: pr-mine", "status": "proposed",
             "assignee": "alice", "priority": "P2", "tags": []}]
    out = cli._pending_reviews_for(t, TEAM, "alice", rows=rows,
                                   aggregate_doc=_agg(t))
    assert [r["name"] for r in out
            if r.get("type") == "review-pending"] == ["pr-mine"]


def test_review_fold_malformed_nested_row_falls_back_loud():
    # Round-2 P1: nested values must be POSITIVELY validated before anything is
    # served — e.g. settled:"false" is truthy and, served silently, would
    # suppress a genuinely pending row while still claiming source:projection.
    t = FakeTransport()
    _put_review(t, "pr-1", "alice")
    good = {"name": "pr-1", "state": "PENDING",
            "pending_required": ["alice"], "settled": False}
    for bad_row in (
        dict(good, settled="false"),          # bool field as a (truthy) string
        dict(good, settled=0),                # bool field as an int
        dict(good, state="WEIRD"),            # not a tally state
        dict(good, state=None),
        dict(good, pending_required="alice"),  # not a list
        dict(good, pending_required=[1]),      # non-str reviewer
        dict(good, pending_required=[""]),     # empty reviewer
        dict(good, name=""),
        "not-a-dict",
    ):
        agg = {projection.REVIEWS_KEY: _fresh_reviews_section(rows=[bad_row])}
        out = cli._pending_reviews_for(t, TEAM, "alice", rows=[],
                                       aggregate_doc=agg)
        src = [r for r in out if r.get("type") == "review-source"]
        assert len(src) == 1 and src[0]["source"] == "raw-scan", bad_row
        assert src[0]["reason"] == "reviews projection malformed", bad_row
        # the loud raw scan still surfaces the real pending review
        assert [r["name"] for r in out
                if r.get("type") == "review-pending"] == ["pr-1"], bad_row


def test_review_fold_head_verdict_transport_error_is_unknown_loud():
    # Round-3 P1a regression: the authoritative head tally routes through
    # _review_tally, whose inner verdict-read loop has no TransportError guard —
    # a transient failure on ONE verdict read must degrade that slug to
    # UNKNOWN-loud (review-head-degraded + degraded-sink entry), never crash
    # the projection-backed fold.
    t = FakeTransport()
    _put_review(t, "pr-down", "alice, bob", verdicts=[("alice", "approve")])
    _reconcile(t)
    agg = _agg(t)  # fresh, complete:true, pr-down PENDING for bob

    class VerdictReadDown(FakeTransport):
        def read(self, path):
            if "/verdicts/" in path and path.endswith(".md"):
                raise TransportError("down")
            return super().read(path)

    down = VerdictReadDown()
    down.store, down.mtimes, down.sizes = t.store, t.mtimes, t.sizes
    rows = [{"id": "rr-down", "name": "rr-down",
             "title": "REVIEW REQUEST: pr-down", "status": "proposed",
             "assignee": "bob", "priority": "P2", "tags": []}]
    sink = []
    out = cli._pending_reviews_for(down, TEAM, "bob", rows=rows,
                                   aggregate_doc=agg, degraded_sink=sink)
    # no exception, no confident answer for the caller's own slug — loud UNKNOWN
    assert not [r for r in out if r.get("type") == "review-pending"]
    head = [r for r in out if r.get("type") == "review-head-degraded"]
    assert head == [{"type": "review-head-degraded", "scanned": 1, "total": 1,
                     "skipped": 1}]
    assert sink == ["review-verdicts:pr-down"]
    assert any(r.get("type") == "review-source"
               and r["source"] == "projection" for r in out)


def test_review_fold_duplicate_row_names_are_malformed():
    # Round-3 P1b regression: reconcile keys rows on the listing, so a duplicate
    # name cannot be produced — a section carrying one is malformed. Served
    # last-write-wins, a later settled row would silently replace the pending
    # one while still claiming source:projection.
    t = FakeTransport()
    _put_review(t, "pr-1", "alice")
    dup_rows = [
        {"name": "pr-1", "state": "PENDING", "pending_required": ["alice"],
         "settled": False},
        {"name": "pr-1", "state": "APPROVED", "pending_required": [],
         "settled": True},
    ]
    agg = {projection.REVIEWS_KEY: _fresh_reviews_section(rows=dup_rows)}
    out = cli._pending_reviews_for(t, TEAM, "alice", rows=[], aggregate_doc=agg)
    src = [r for r in out if r.get("type") == "review-source"]
    assert len(src) == 1 and src[0]["source"] == "raw-scan"
    assert src[0]["reason"] == "reviews projection malformed"
    # the loud raw scan still surfaces the real obligation
    assert [r["name"] for r in out
            if r.get("type") == "review-pending"] == ["pr-1"]


def test_review_fold_inconsistent_settled_rows_are_malformed():
    # Round-3 P1b: settled:true is ONLY legal as a terminal APPROVED tally with
    # nothing pending — any other combination could suppress live work and is
    # malformed, never served.
    t = FakeTransport()
    _put_review(t, "pr-1", "alice")
    for bad_row in (
        {"name": "pr-1", "state": "PENDING", "pending_required": ["alice"],
         "settled": True},
        {"name": "pr-1", "state": "CHANGES", "pending_required": [],
         "settled": True},
        {"name": "pr-1", "state": "APPROVED", "pending_required": ["alice"],
         "settled": True},
    ):
        agg = {projection.REVIEWS_KEY: _fresh_reviews_section(rows=[bad_row])}
        out = cli._pending_reviews_for(t, TEAM, "alice", rows=[],
                                       aggregate_doc=agg)
        src = [r for r in out if r.get("type") == "review-source"]
        assert len(src) == 1 and src[0]["source"] == "raw-scan", bad_row
        assert src[0]["reason"] == "reviews projection malformed", bad_row
        assert [r["name"] for r in out
                if r.get("type") == "review-pending"] == ["pr-1"], bad_row


def test_review_fold_malformed_slug_lists_fall_back_loud():
    t = FakeTransport()
    _put_review(t, "pr-1", "alice")
    for key, val in (("orphans", "x"), ("orphans_unknown", [1]),
                     ("tombstones", [""])):
        agg = {projection.REVIEWS_KEY: _fresh_reviews_section(**{key: val})}
        out = cli._pending_reviews_for(t, TEAM, "alice", rows=[],
                                       aggregate_doc=agg)
        src = [r for r in out if r.get("type") == "review-source"]
        assert len(src) == 1 and src[0]["source"] == "raw-scan", key
        assert src[0]["reason"] == "reviews projection malformed", key


def test_forge_fold_malformed_nested_values_fall_back_loud():
    # Round-2 P1, forge side: a non-list responsibility entry or an id-less
    # feedback item must not be silently skipped — any invalid nested value is
    # a loud raw-scan fallback.
    t = FakeTransport()
    _seed_forge_team(t)
    fresh = {"schema": projection.FORGE_SCHEMA, "generated_at": _now_iso(),
             "complete": True, "responsible": {"o-r-9": ["bob"]},
             "feedback": {"o-r-9": [{"id": "review-1", "author": "rev"}]}}
    for over in (
        {"responsible": {"o-r-9": "bob"}},                       # not a list
        {"responsible": {"o-r-9": [1]}},                         # non-str agent
        {"responsible": {"o-r-9": [""]}},                        # empty agent
        {"feedback": {"o-r-9": {"id": "review-1"}}},             # not a list
        {"feedback": {"o-r-9": ["review-1"]}},                   # not a dict
        {"feedback": {"o-r-9": [{"author": "rev"}]}},            # id-less item
        {"feedback": {"o-r-9": [{"id": "", "author": "rev"}]}},  # empty id
        {"feedback": {"o-r-9": [{"id": "review-1", "author": 7}]}},
    ):
        agg = {projection.FORGE_KEY: dict(fresh, **over)}
        out = cli._forge_feedback_for(t, TEAM, "bob", aggregate_doc=agg)
        src = [r for r in out if r.get("type") == "forge-source"]
        assert len(src) == 1 and src[0]["source"] == "raw-scan", over
        assert src[0]["reason"] == "forge projection malformed", over
        # the loud raw scan still finds the real feedback
        assert [r["pr_slug"] for r in out
                if r.get("type") == "forge-feedback"] == ["o-r-9"], over


def test_review_fold_unresolvable_head_slug_is_unknown_loud():
    class ReviewDocDown(FakeTransport):
        def read(self, path):
            if path.endswith("gone-pr.md"):
                return None
            return super().read(path)

    t = ReviewDocDown()
    _reconcile(t)
    agg = _agg(t)
    rows = [{"id": "rr-gone-pr", "name": "rr-gone-pr",
             "title": "REVIEW REQUEST: gone-pr", "status": "proposed",
             "assignee": "alice", "priority": "P2", "tags": []}]
    sink = []
    out = cli._pending_reviews_for(t, TEAM, "alice", rows=rows,
                                   aggregate_doc=agg, degraded_sink=sink)
    head = [r for r in out if r.get("type") == "review-head-degraded"]
    assert len(head) == 1 and head[0]["skipped"] == 1
    assert sink == ["review-verdicts:gone-pr"]


# --- read side: the forge fold ----------------------------------------------

def _seed_forge_team(t):
    url = "https://github.com/o/r/pull/9"
    t.put(f"team/{TEAM}/_coord/forge/watch/o-r-9.md",
          f"---\ntype: Watch\nurl: {url}\nagent: bob\n---\n")
    t.put(f"team/{TEAM}/_coord/forge/feedback/o-r-9/review-1.md",
          "---\ntype: ForgeFeedback\nauthor: rev\n---\nfix\n")


def test_forge_fold_consumes_fresh_projection_without_scanning():
    t = CountingTransport()
    _seed_forge_team(t)
    _reconcile(t)
    agg = _agg(t)
    t.reset_counts()
    out = cli._forge_feedback_for(t, TEAM, "bob", aggregate_doc=agg)
    fb = [r for r in out if r.get("type") == "forge-feedback"]
    assert fb == [{"type": "forge-feedback", "pr_slug": "o-r-9", "count": 1,
                   "authors": ["rev"], "items": ["review-1"]}]
    src = [r for r in out if r.get("type") == "forge-source"]
    assert src and src[0]["source"] == "projection"
    # zero forge/review scans: the only transport work is this agent's ack read
    assert not [p for p in t.listed if "/forge/" in p or "/review/" in p]
    assert t.reads == [cli._ack_path(TEAM, "review-1", "bob")]


def test_forge_fold_projection_hides_acked_items():
    t = FakeTransport()
    _seed_forge_team(t)
    _reconcile(t)
    t.put(cli._ack_path(TEAM, "review-1", "bob"),
          "---\ntype: Ack\nagent: bob\n---\nacked\n")
    out = cli._forge_feedback_for(t, TEAM, "bob", aggregate_doc=_agg(t))
    assert not [r for r in out if r.get("type") == "forge-feedback"]
    assert any(r.get("type") == "forge-source" for r in out)


def test_forge_fold_stale_projection_falls_back_loudly():
    t = CountingTransport()
    _seed_forge_team(t)
    _reconcile(t, now=_iso(datetime.now(timezone.utc) - timedelta(hours=48)))
    agg = _agg(t)
    t.reset_counts()
    out = cli._forge_feedback_for(t, TEAM, "bob", aggregate_doc=agg)
    assert f"team/{TEAM}/_coord/forge/watch/" in t.listed  # raw scan really ran
    src = [r for r in out if r.get("type") == "forge-source"]
    assert len(src) == 1 and src[0]["source"] == "raw-scan"
    assert "stale" in src[0]["reason"]
    assert [r["pr_slug"] for r in out if r.get("type") == "forge-feedback"] == ["o-r-9"]


def test_forge_fold_missing_projection_behaves_exactly_as_today():
    t = FakeTransport()
    _seed_forge_team(t)
    agg = {"schema": "coord.teams.summaries.v1", "rows": []}
    assert (cli._forge_feedback_for(t, TEAM, "bob", aggregate_doc=agg)
            == cli._forge_feedback_for(t, TEAM, "bob"))


# --- end to end: projection agrees with the raw per-slug truth ---------------

def test_projection_states_match_raw_review_status_end_to_end():
    t = FakeTransport()
    _put_review(t, "pending-pr", "alice")
    _put_review(t, "approved-pr", "alice", verdicts=[("alice", "approve")])
    _put_review(t, "changes-pr", "alice", verdicts=[("alice", "changes")])
    _put_review(t, "half-pr", "alice, bob", verdicts=[("alice", "approve")])
    _reconcile(t)
    sec = _agg(t)[projection.REVIEWS_KEY]
    by_name = {r["name"]: r for r in sec["rows"]}
    for slug in ("pending-pr", "approved-pr", "changes-pr", "half-pr"):
        tally, doc_ok, vok, listing_ok = cli._review_tally(t, TEAM, slug)
        assert doc_ok and vok and listing_ok
        assert by_name[slug]["state"] == tally["state"], slug
        assert by_name[slug]["pending_required"] == tally["pending_required"], slug
    # and the fold serves exactly the raw fold's answer, from the projection
    proj_out = cli._pending_reviews_for(t, TEAM, "bob", rows=[],
                                        aggregate_doc=_agg(t))
    raw_out = cli._pending_reviews_raw(t, TEAM, "bob", rows=[])
    want = [r for r in raw_out if r.get("type") == "review-pending"]
    got = [r for r in proj_out if r.get("type") == "review-pending"]
    assert got == want == [{"type": "review-pending", "name": "half-pr",
                            "state": "PENDING", "pending_required": ["bob"]}]


def test_needs_me_end_to_end_serves_projection(capsys):
    t = CountingTransport()
    _put_review(t, "pr-e2e", "alice")
    _reconcile(t)
    t.reset_counts()
    assert cli.main(["needs-me", TEAM, "--agent", "alice", "--json"],
                    transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in got
            if r.get("type") == "review-pending"] == ["pr-e2e"]
    assert any(r.get("type") == "review-source"
               and r.get("source") == "projection" for r in got)
    assert any(r.get("type") == "forge-source"
               and r.get("source") == "projection" for r in got)
    assert not [p for p in t.listed if "/review/" in p or "/forge/" in p]


def _put_directive(t, slug="mine"):
    t.put(f"team/{TEAM}/task/{slug}.md",
          "---\ntype: Task\ntitle: Mine\nstatus: active\npriority: P1\n"
          "assignee: alice\ntags: [kind:directive]\n---\n")


def test_needs_me_task_projection_hit_skips_ack_fanout(capsys):
    t = CountingTransport()
    _put_directive(t)
    _reconcile(t)
    agg = _agg(t)
    assert agg[projection.NEEDS_ME_KEY]["complete"] is True
    t.reset_counts()

    assert cli.main(["needs-me", TEAM, "--agent", "alice", "--json"],
                    transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    assert [r["name"] for r in got if r.get("name") == "mine"] == ["mine"]
    assert {"type": "needs-me-source", "source": "projection",
            "as_of": agg[projection.NEEDS_ME_KEY]["generated_at"]} in got
    assert not [p for p in t.reads if "/_coord/acks/" in p]
    assert len(t.updates_calls) == 1


def test_needs_me_task_projection_stale_falls_back_loudly(capsys):
    t = CountingTransport()
    _put_directive(t)
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=48))
    _reconcile(t, now=old)
    t.updates_result = None
    t.reset_counts()

    assert cli.main(["needs-me", TEAM, "--agent", "alice", "--json"],
                    transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    src = [r for r in got if r.get("type") == "needs-me-source"]
    assert len(src) == 1 and src[0]["source"] == "raw-scan"
    assert "stale" in src[0]["reason"]
    assert [p for p in t.reads if "/_coord/acks/mine/" in p]


def test_needs_me_task_projection_stale_but_feed_clean_is_current(
        capsys, monkeypatch):
    t = CountingTransport()
    _put_directive(t)
    monkeypatch.setenv("COORD_PROJECTION_MAX_AGE_HOURS", "1")
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=2))
    _reconcile(t, now=old)
    # Mixed-fleet carry: the container was refreshed without rebuilding this
    # section. The feed window must use the section's older anchor.
    agg = _agg(t)
    agg["generated_at"] = _now_iso()
    t.store[f"team/{TEAM}/_coord/summaries.json"] = json.dumps(agg)
    t.reset_counts()

    assert cli.main(["needs-me", TEAM, "--agent", "alice", "--json"],
                    transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    src = [r for r in got if r.get("type") == "needs-me-source"]
    assert len(src) == 1 and src[0]["source"] == "projection"
    assert not [p for p in t.reads if "/_coord/acks/" in p]
    assert len(t.updates_calls) == 1
    assert int(t.updates_calls[0][0].split()[0]) > 3600


def test_needs_me_feed_delta_raw_tallies_only_changed_ack_slug(capsys):
    t = CountingTransport()
    _put_directive(t, "changed")
    _put_directive(t, "stable")
    _reconcile(t)
    t.updates_result = [{
        "path": f"team/{TEAM}/_coord/acks/changed/alice.md",
        "state": "uploaded", "uploaded_at": _now_iso(),
        "archived_at": None, "deleted_at": None,
    }]
    t.reset_counts()
    # reset_counts deliberately leaves the configured feed result intact

    assert cli.main(["needs-me", TEAM, "--agent", "alice", "--json"],
                    transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in got if r.get("name") in {"changed", "stable"}} == {
        "changed", "stable"}
    ack_reads = [p for p in t.reads if "/_coord/acks/" in p]
    assert ack_reads == [cli._ack_path(TEAM, "changed", "alice")]
    assert len(t.updates_calls) == 1


def test_needs_me_task_projection_malformed_falls_back_loudly(capsys):
    t = CountingTransport()
    _put_directive(t)
    _reconcile(t)
    agg = _agg(t)
    agg[projection.NEEDS_ME_KEY]["rows"] = [{"name": "mine",
                                               "mtime": 17,
                                               "acked_by": []}]
    t.store[f"team/{TEAM}/_coord/summaries.json"] = json.dumps(agg)
    t.reset_counts()

    assert cli.main(["needs-me", TEAM, "--agent", "alice", "--json"],
                    transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    src = [r for r in got if r.get("type") == "needs-me-source"]
    assert src == [{"type": "needs-me-source", "source": "raw-scan",
                    "reason": "needs-me projection malformed"}]
    assert [p for p in t.reads if "/_coord/acks/mine/" in p]


def test_needs_me_text_discloses_fold_source(capsys):
    t = FakeTransport()
    _put_review(t, "pr-txt", "alice")
    _reconcile(t)
    assert cli.main(["needs-me", TEAM, "--agent", "alice"], transport=t) == 0
    out = capsys.readouterr().out
    assert "review fold: projection (as of " in out
    assert "forge fold: projection (as of " in out


def test_obligations_ignore_source_rows_and_stay_clear(capsys):
    # provenance rows are informational: they are neither owed work nor
    # degradation, so a projection-served CLEAR stays CLEAR (rc 0)
    t = FakeTransport()
    _put_review(t, "pr-x", "somebody-else")
    _reconcile(t)
    assert cli.main(["obligations", TEAM, "--agent", "alice", "--json"],
                    transport=t) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["state"] == "CLEAR"
    assert got["owed_count"] == 0


def test_projection_carried_by_mixed_fleet_passthrough():
    # an aggregate rebuilt by build_aggregate with this build's prior must keep
    # the sections (the v1.6.8 anchor-wipe lesson, applied to the projection)
    from coord_engine import aggregate as aggregate_mod
    t = FakeTransport()
    _put_review(t, "pr-1", "alice")
    _reconcile(t)
    prior = _agg(t)
    rebuilt = aggregate_mod.build_aggregate(
        TEAM, [], generated_at=_now_iso(), reconcile_host="other",
        warnings=[], prior=prior)
    assert rebuilt[projection.REVIEWS_KEY] == prior[projection.REVIEWS_KEY]
    assert rebuilt[projection.FORGE_KEY] == prior[projection.FORGE_KEY]


def test_review_root_listing_failure_carries_prior_projection():
    t = FakeTransport()
    _put_review(t, "pr-1", "alice")
    old = _iso(datetime.now(timezone.utc) - timedelta(hours=1))
    _reconcile(t, now=old)
    prior = _agg(t)[projection.REVIEWS_KEY]

    class ReviewListingDown(FakeTransport):
        def list_dir(self, prefix):
            if prefix == f"team/{TEAM}/review/":
                raise TransportError("down")
            return super().list_dir(prefix)

    t2 = ReviewListingDown()
    t2.store, t2.mtimes, t2.sizes = t.store, t.mtimes, t.sizes
    _reconcile(t2)
    sec = json.loads(t2.store[f"team/{TEAM}/_coord/summaries.json"])[
        projection.REVIEWS_KEY]
    # prior carried UNTOUCHED — the old stamp ages it out honestly rather than
    # re-stamping unknown state as current
    assert sec == prior
    assert sec["generated_at"] == old
