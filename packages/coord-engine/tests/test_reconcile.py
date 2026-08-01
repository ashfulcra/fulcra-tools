import json

from coord_engine import aggregate as aggregate_mod
from coord_engine import annotate, reconcile
from coord_engine.transport import TransportError


class FakeTransport:
    """In-memory Fulcra File Store: {path: content} + per-path mtime."""

    def __init__(self):
        self.store: dict[str, str] = {}
        self.mtimes: dict[str, str] = {}
        self.sizes: dict[str, str] = {}
        self.fail_list = False

    def put(self, path, content, mtime="2026-07-01 04:00PM UTC", size=None):
        self.store[path] = content
        self.mtimes[path] = mtime
        # Mirror `fulcra-api file list`, which reports a byte size per entry. Size
        # is the sub-minute change signal the incremental-reuse guard relies on:
        # default it to the content length so a same-minute edit that changes the
        # doc changes its listed size.
        self.sizes[path] = size if size is not None else f"{len(content)}B"

    def list_dir(self, prefix):
        if self.fail_list:
            raise TransportError("boom")
        out = []
        seen_dirs = set()
        for p in sorted(self.store):
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):]
            head = rest.rstrip("/")
            if "/" in head:
                # deeper than a direct child -> synthesize the intermediate dir
                # entry, matching `fulcra-api file list` (which shows `sub/`).
                seg = head.split("/", 1)[0] + "/"
                if seg not in seen_dirs:
                    seen_dirs.add(seg)
                    out.append({"name": seg, "mtime": None, "is_dir": True})
                continue
            out.append({"name": rest, "mtime": self.mtimes.get(p),
                        "size": self.sizes.get(p),
                        "is_dir": rest.endswith("/")})
        return out

    def read(self, path):
        return self.store.get(path)

    def write(self, path, content):
        self.store[path] = content
        return True

    def delete(self, path):
        return self.store.pop(path, None) is not None

    def delete_idempotent(self, path):
        if path not in self.store:
            return True
        return self.delete(path)


def _task(title, status, priority="P2"):
    return f"---\ntype: Task\ntitle: {title}\nstatus: {status}\npriority: {priority}\n---\nbody"


def _run(t):
    return reconcile.reconcile(t, "r", now="2026-07-01T00:00:00Z", today="2026-07-01", host="h")


def test_reconcile_builds_index_and_aggregate():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    t.put("team/r/task/b.md", _task("Bravo", "done"))
    res = _run(t)
    assert res["tasks"] == 2
    idx = t.store["team/r/task/index.md"]
    assert "## Active" in idx and "[Alpha](a.md)" in idx
    raw = t.store["team/r/_coord/summaries.json"]
    agg = json.loads(raw)
    assert agg["team"] == "r"
    assert {r["name"] for r in agg["rows"]} == {"a", "b"}
    assert raw == json.dumps(agg, separators=(",", ":"))


def test_reconcile_skips_index_and_non_task_docs():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    t.put("team/r/task/index.md", "# Tasks\n(stale)")
    t.put("team/r/task/note.md", "---\ntype: Reference\n---\nnot a task")
    assert _run(t)["tasks"] == 1


def _task_ts(title, status, ts):
    return (f"---\ntype: Task\ntitle: {title}\nstatus: {status}\n"
            f"timestamp: {ts}\n---\nbody")


def test_reconcile_persists_pending_when_resolution_live():
    # opt-in on the bus -> reconcile threads the pass's STRUCTURED transitions to
    # the projection artifact (ts = the task's own updated_at, normalized).
    t = FakeTransport()
    t.put(annotate.resolution_path("r"), "transitions\n")
    t.put("team/r/task/a.md", _task_ts("Alpha", "active", "2026-07-09T09:00:00Z"))
    _run(t)  # first pass: creation transition for Alpha
    assert annotate.pending_path("r") in t.store
    pend = json.loads(t.store[annotate.pending_path("r")])
    assert pend["transitions"] == [
        {"task_id": "a", "kind": "create", "ts": "2026-07-09T09:00:00Z", "title": "Alpha"}]
    # bullets/log.md are still produced unchanged (behavior-preserving)
    assert "Creation" in t.store["team/r/task/log.md"]


def test_reconcile_no_pending_when_resolution_off():
    # default (no resolution on the bus) -> no projection artifact written at all.
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _run(t)
    assert annotate.pending_path("r") not in t.store


def test_reconcile_empty_diff_does_not_wipe_live_pending():
    # CRITICAL-1: a reconcile whose diff is empty must NOT overwrite a pending
    # transition the projection fold is still holding LIVE — i.e. one that has not
    # landed (absent from the cursor's seen_ids). The deployed
    # `reconcile && annotate project` topology reconciles every beat; a blind
    # overwrite with [] here would DROP that moment before projection ever emits it.
    t = FakeTransport()
    t.put(annotate.resolution_path("r"), "transitions\n")
    t.put("team/r/task/a.md", _task_ts("Alpha", "active", "2026-07-09T09:00:00Z"))
    _run(t)  # first pass: create-a persisted to pending; cursor never advanced
    pend1 = json.loads(t.store[annotate.pending_path("r")])["transitions"]
    assert [x["task_id"] for x in pend1] == ["a"]
    # second pass: task a unchanged -> empty diff. Merge-and-carry keeps the
    # un-landed create-a (cursor has no seen_ids) rather than wiping to [].
    _run(t)
    pend2 = json.loads(t.store[annotate.pending_path("r")])["transitions"]
    assert [x["task_id"] for x in pend2] == ["a"]


def test_reconcile_is_orphan_proof():
    t = FakeTransport()
    prior = {"schema": "coord.teams.summaries.v1", "rows": [
        {"id": "ghost", "name": "ghost", "status": "active", "mtime": "old"}]}
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/live.md", _task("Live", "active"))
    _run(t)
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    assert {r["name"] for r in agg["rows"]} == {"live"}  # ghost pruned
    assert "Deprecation" in t.store["team/r/task/log.md"]


def test_reconcile_incremental_reuses_unchanged_without_download():
    from coord_engine import model
    t = FakeTransport()
    body = _task("A", "active")
    # A well-formed prior row carries a stamped `size` AND the current row-schema
    # stamp `sv` (the row format now includes both); an old mtime (no generated_at
    # anchor) reuses on the mtime+size compare.
    prior = {"rows": [{"id": "a", "name": "a", "status": "active", "title": "A",
                       "mtime": "2026-07-01 04:00PM UTC", "size": f"{len(body)}B",
                       "sv": model.ROW_SCHEMA_VERSION, "description": "d"}]}
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/a.md", body, mtime="2026-07-01 04:00PM UTC")
    reads = []
    orig = t.read
    t.read = lambda p: (reads.append(p), orig(p))[1]
    res = _run(t)
    assert res["reused"] == 1 and res["parsed"] == 0
    assert "team/r/task/a.md" not in reads  # unchanged mtime+size -> never downloaded


def test_reconcile_reparses_when_mtime_changes():
    t = FakeTransport()
    prior = {"rows": [{"id": "a", "name": "a", "status": "active", "title": "A",
                       "mtime": "2026-07-01 04:00PM UTC", "description": "d"}]}
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/a.md", _task("A", "done"), mtime="2026-07-01 05:00PM UTC")
    res = _run(t)
    assert res["parsed"] == 1
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    assert agg["rows"][0]["status"] == "done"


def _reconcile_at(t, now, team="r"):
    return reconcile.reconcile(t, team, now=now, today=now[:10], host="h")


def test_reconcile_same_minute_double_write_not_fossilized():
    """ENG-1-4 (index-side twin of PR #356): a doc changed TWICE in one clock-minute
    keeps an identical minute-resolution mtime; a length-CHANGING second write is
    caught by the size compare so the row reflects the LATEST content, never the
    fossil."""
    t = FakeTransport()
    minute = "2026-07-01 04:23PM UTC"
    t.put("team/r/task/a.md", _task("A", "proposed"), mtime=minute)
    _reconcile_at(t, "2026-07-01T16:23:20Z")
    assert json.loads(t.store["team/r/_coord/summaries.json"])["rows"][0]["status"] == "proposed"
    t.put("team/r/task/a.md", _task("A", "done"), mtime=minute)  # different length -> size differs
    _reconcile_at(t, "2026-07-01T16:24:05Z")
    assert json.loads(t.store["team/r/_coord/summaries.json"])["rows"][0]["status"] == "done", \
        "same-minute length-changing write must be re-read, not fossilized"


def test_reconcile_same_minute_equal_length_edit_reflects_latest():
    """Codex probe 1 (the mtime+size blind spot): a SAME-length edit in the SAME
    clock-minute leaves mtime AND byte size identical, so mtime+size alone would
    fossilize the stale row. The same-minute guard (mtime-minute not proven closed
    before our last reconcile) forces a reparse — the row reflects the LATEST
    content. `blocked`->`waiting` are both 7 chars, so the doc size is unchanged."""
    t = FakeTransport()
    minute = "2026-07-01 04:23PM UTC"
    t.put("team/r/task/a.md", _task("A", "blocked"), mtime=minute)
    _reconcile_at(t, "2026-07-01T16:23:20Z")  # pass 1 reads DURING minute 16:23
    a1 = json.loads(t.store["team/r/_coord/summaries.json"])
    assert a1["rows"][0]["status"] == "blocked"
    assert a1["rows"][0].get("size") is not None  # size stamped
    t.put("team/r/task/a.md", _task("A", "waiting"), mtime=minute)  # SAME size, SAME minute
    _reconcile_at(t, "2026-07-01T16:24:05Z")  # last reconcile (16:23) did not outlast minute 16:23
    a2 = json.loads(t.store["team/r/_coord/summaries.json"])
    assert a2["rows"][0]["status"] == "waiting", \
        "same-minute equal-length edit must be re-read, not fossilized as 'blocked'"


def test_reconcile_legacy_no_size_row_reparsed_once():
    """Codex probe 2 (HIGH-2, migration): a legacy prior row carries NO stamped
    `size` (pre-upgrade aggregate). It must be reparsed ONCE — never reused on
    mtime alone — so a pre-existing stale/fossilized row is healed and re-stamped
    on the first post-upgrade reconcile, even for a same-length live edit."""
    t = FakeTransport()
    minute = "2026-07-01 04:23PM UTC"
    prior = {"schema": "coord.teams.summaries.v1", "generated_at": "2026-07-01T16:20:00Z",
             "rows": [{"id": "a", "name": "a", "status": "blocked", "title": "A",
                       "mtime": minute, "description": ""}]}  # NO `size` -> legacy
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/a.md", _task("A", "waiting"), mtime=minute)  # live doc moved on
    _reconcile_at(t, "2026-07-01T16:25:00Z")
    a = json.loads(t.store["team/r/_coord/summaries.json"])
    assert a["rows"][0]["status"] == "waiting", \
        "legacy no-size prior must be reparsed once, not reused stale"
    assert a["rows"][0].get("size") is not None  # and re-stamped for next time


def test_reconcile_legacy_unstamped_row_reparsed_and_capped():
    """v1.6.7 item B: a legacy row built BEFORE the #388 text cap carries an
    uncapped title/description and NO `sv` stamp. Even with matching mtime+size
    (an unchanged, static task) it must NOT be reused — it is force-reparsed so
    the cap applies, then re-stamped with the current schema version."""
    from coord_engine import model
    t = FakeTransport()
    minute = "2026-07-01 04:00PM UTC"
    long_title = "T" * 5000
    long_desc = "D" * 5000
    # The live doc carries multi-KB title/description (the #388 scenario). A legacy
    # prior row built pre-cap holds the SAME uncapped text and matching mtime+size,
    # so mtime+size reuse would carry it forward untouched — the cap never applies.
    body = (f"---\ntype: Task\ntitle: {long_title}\ndescription: {long_desc}\n"
            f"status: proposed\npriority: P2\n---\nbody")
    prior = {"schema": "coord.teams.summaries.v1", "generated_at": "2026-06-01T00:00:00Z",
             "rows": [{"id": "a", "name": "a", "status": "proposed", "title": long_title,
                       "description": long_desc, "mtime": minute,
                       "size": f"{len(body)}B"}]}  # matching mtime+size, NO `sv` -> legacy
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/a.md", body, mtime=minute)
    res = _run(t)
    assert res["parsed"] == 1 and res["reused"] == 0, \
        "legacy unstamped row must be reparsed, not reused uncapped"
    row = json.loads(t.store["team/r/_coord/summaries.json"])["rows"][0]
    assert len(row["title"]) == model.DEFAULT_SUMMARY_TEXT_CAP
    assert len(row["description"]) == model.DEFAULT_SUMMARY_TEXT_CAP
    assert row["sv"] == model.ROW_SCHEMA_VERSION  # re-stamped for next time


def test_reconcile_stamped_short_row_still_reused():
    """A row already stamped with the current schema version and within the cap,
    with unchanged mtime+size, is reused as before — the stamp check must not
    force needless reparses of already-current rows."""
    from coord_engine import model
    t = FakeTransport()
    minute = "2026-07-01 04:00PM UTC"
    body = _task("A", "active")
    prior = {"rows": [{"id": "a", "name": "a", "status": "active", "title": "A",
                       "description": "d", "mtime": minute, "size": f"{len(body)}B",
                       "sv": model.ROW_SCHEMA_VERSION}]}
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/a.md", body, mtime=minute)
    reads = []
    orig = t.read
    t.read = lambda p: (reads.append(p), orig(p))[1]
    res = _run(t)
    assert res["reused"] == 1 and res["parsed"] == 0
    assert "team/r/task/a.md" not in reads  # stamped + unchanged -> never downloaded


def test_reconcile_unparseable_keeps_prior_row_and_warns():
    t = FakeTransport()
    prior = {"rows": [{"id": "a", "name": "a", "status": "active", "title": "A",
                       "mtime": "old", "description": ""}]}
    t.put("team/r/_coord/summaries.json", json.dumps(prior))
    t.put("team/r/task/a.md", "garbage, no frontmatter", mtime="new")
    res = _run(t)
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    assert {r["name"] for r in agg["rows"]} == {"a"}
    assert any("unparseable" in w for w in res["warnings"])


def test_reconcile_degraded_on_list_failure_writes_nothing():
    t = FakeTransport()
    t.fail_list = True
    t.put("team/r/_coord/summaries.json", json.dumps({"rows": [{"id": "a", "name": "a"}]}))
    before = dict(t.store)
    res = _run(t)
    assert res["degraded"] is True
    assert t.store == before  # no truncated index written


# --- data-updates fast path ---

def _reconciled(t, now="2026-07-01T12:00:00Z"):
    from coord_engine import reconcile as rec
    return rec.reconcile(t, "r", now=now, today=now[:10], host="h")


def _with_updates(t, changes):
    t.updates_calls = []
    def updates(period):
        t.updates_calls.append(period)
        return changes
    t.updates = updates
    return t


def test_fast_path_skips_full_pass_when_no_relevant_changes():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)                                     # seed aggregate at 12:00
    _with_updates(t, [{"full_name": "/team/r/presence/x.md"},   # irrelevant churn
                      {"full_name": "/other/thing.md"}])
    index_before = t.store["team/r/task/index.md"]
    t.store["team/r/task/a.md"] = _task("Alpha CHANGED", "active")  # sneaky edit the feed missed
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert res.get("fast_path") is True
    assert t.store["team/r/task/index.md"] == index_before   # index untouched (fold of unchanged inputs)
    assert res["tasks"] == 1 and res["parsed"] == 0


def test_fast_path_declines_on_task_change():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "/team/r/task/b.md"}])
    t.put("team/r/task/b.md", _task("Bravo", "active"))
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")
    assert res["tasks"] == 2


def test_fast_path_declines_on_ack_change():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "/team/r/_coord/acks/x.json"}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")


def test_fast_path_declines_on_malformed_feed_entry():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "/team/r/presence/x.md"}, {}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")


def test_fast_path_declines_without_updates_support_or_on_error():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    res = _reconciled(t, now="2026-07-01T12:30:00Z")   # no .updates attr
    assert not res.get("fast_path")
    def broken(period):
        return None
    t.updates = broken
    res = _reconciled(t, now="2026-07-01T12:35:00Z")   # updates errors -> None
    assert not res.get("fast_path")


def test_fast_path_declines_when_aggregate_stale():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)                                     # generated_at 12:00
    _with_updates(t, [])
    res = _reconciled(t, now="2026-07-01T19:00:00Z")   # 7h later > 6h guard
    assert not res.get("fast_path")


def test_fast_path_still_writes_health_shard():
    from coord_engine import health as health_mod
    from coord_engine.tasks import agent_key
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    shard_path = f"{health_mod.health_prefix('r')}{agent_key('h')}.json"
    t.store.pop(shard_path, None)                      # wipe; fast path must re-beat
    _with_updates(t, [])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert res.get("fast_path") is True
    assert shard_path in t.store                       # host doesn't go dark on fast path


def test_fast_path_ignores_own_derived_artifacts_in_feed():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    # the previous pass's own index/log writes show up in the store feed
    _with_updates(t, [{"full_name": "/team/r/task/index.md"},
                      {"full_name": "/team/r/task/log.md"}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert res.get("fast_path") is True


def test_fast_path_normalizes_missing_leading_slash():
    # a relevant change WITHOUT the leading slash must still decline (fail-closed)
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "team/r/task/b.md"}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")


def test_fast_path_declines_on_unparseable_feed_entries():
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    for bad in (["just-a-string"], [{"full_name": 7}], [{"no_name": "x"}], [None]):
        _with_updates(t, bad)
        res = _reconciled(t, now="2026-07-01T12:30:00Z")
        assert not res.get("fast_path"), f"feed {bad!r} must be doubt -> full pass"


def test_fast_path_declines_on_deletion_entry():
    # LIVE-CAPTURED feed shape for a deleted file (2026-07-05, fulcra data-updates):
    # {"id": "6b369982-...", "full_name": "/team/fulcra/_scratch/del-probe.txt",
    #  "scan_state": "unscanned", "size": 6, "uploaded_at": "2026-07-05T12:46:43Z",
    #  "archived_at": null, "deleted_at": "2026-07-05T12:46:43.832485Z",
    #  "state": "deleted"}
    # -> deletions DO carry full_name; a deleted task file declines the fast path.
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [{"full_name": "/team/r/task/gone.md", "state": "deleted",
                       "deleted_at": "2026-07-01T12:10:00Z"}])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path")


def _unstamp_prior_rows(t):
    """Strip the `sv` stamp from the written aggregate — what a pre-#388 host
    (v1.6.6, which predates both the text cap and the stamp) leaves behind."""
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    for row in agg["rows"]:
        row.pop("sv", None)
    t.store["team/r/_coord/summaries.json"] = json.dumps(agg)
    return t


def test_fast_path_does_not_gate_on_row_schema_stamps():
    """v1.6.9: the fast path must NOT decline over stale-schema rows.

    v1.6.7 gated it there so a quiet fleet couldn't perpetuate legacy unstamped
    rows. That reasoning only holds for a fleet that converges. It does not: a
    MIXED fleet reconciles one shared index, and every pass by a pre-stamp host
    re-introduces unstamped rows. The gate therefore never settles — it declines
    forever, making a mixed fleet do MORE full passes than before it existed.
    Healing is the reuse gate's job (below), not the fast path's."""
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _unstamp_prior_rows(t)
    _with_updates(t, [])                               # feed shows no changes
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert res.get("fast_path") is True, "stale-schema rows must not stall the fast path"


def test_stale_schema_rows_still_heal_via_the_reuse_gate():
    """The counterpart to the above: dropping the fast-path gate must not cost us
    the heal. On any pass that does real work, the incremental-reuse gate still
    refuses a stale-projection row, forcing the reparse that re-caps + re-stamps
    it. This is the heal, and it is the ONLY place the `sv` check now lives."""
    from coord_engine import model
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _unstamp_prior_rows(t)
    # A full pass (no updates feed -> no fast path at all): the unstamped row is
    # reparsed rather than reused, and comes back stamped.
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert res["parsed"] == 1 and res["reused"] == 0, "stale row must be reparsed, not reused"
    healed = json.loads(t.store["team/r/_coord/summaries.json"])
    assert healed["rows"][0]["sv"] == model.ROW_SCHEMA_VERSION


def test_fast_path_still_declines_when_ack_fold_owes_a_pass():
    """The OTHER fast-path guard is untouched. Unlike the row-schema gate, the ack
    anchor is self-settling — it is about a fold owing a pass, and one full fold
    settles it — so it stays."""
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    agg.pop(reconcile.ACKS_ANCHOR_KEY)                 # a legacy/older-host write
    t.store["team/r/_coord/summaries.json"] = json.dumps(agg)
    _with_updates(t, [])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert not res.get("fast_path"), "ack fold owing a pass must still decline"


def test_fast_path_fires_on_a_quiet_beat():
    """The baseline the two guards above carve out of: a fully-settled aggregate
    plus a feed showing nothing fold-relevant takes the fast path."""
    t = FakeTransport()
    t.put("team/r/task/a.md", _task("Alpha", "active"))
    _reconciled(t)
    _with_updates(t, [])
    res = _reconciled(t, now="2026-07-01T12:30:00Z")
    assert res.get("fast_path") is True and res["parsed"] == 0


# --- acks fold: change-driven via recent_changes (v1.6.8) ---
#
# The acks fold used to list EVERY ack dir every pass (280 listings on the live
# bus = ~336s at a remote host's 1.2s/op). It now asks the store what changed and
# re-folds only those slugs. These tests pin the invariant that makes that safe:
# the incremental path is an OPTIMIZATION, and every way it can fail — no change
# query, a query error, a bad anchor, feed-shape drift — falls back to the full
# fold and says so. Nothing here may ever "assume unchanged".

def _seed_acks(t):
    """Three live tasks, each with one ack shard."""
    for slug, agent in (("a", "amy"), ("b", "bob"), ("c", "cat")):
        t.put(f"team/r/task/{slug}.md", _task(slug.upper(), "active"))
        t.put(f"team/r/_coord/acks/{slug}/{agent}.md",
              f"---\ntype: Ack\nagent: {agent}\ntimestamp: 2026-07-01T15:00:00Z\n---\n")
    return t


def _with_recent_changes(t, files):
    """Attach the change-query capability to the fake. ``files`` is the endpoint's
    flat entry list (or a callable returning one, or None to simulate a 500)."""
    t.rc_calls = []

    def recent_changes(start_iso, end_iso):
        t.rc_calls.append((start_iso, end_iso))
        return files(start_iso, end_iso) if callable(files) else files

    t.recent_changes = recent_changes
    return t


def _spy_lists(t):
    calls = []
    orig = t.list_dir

    def spy(prefix):
        calls.append(prefix)
        return orig(prefix)

    t.list_dir = spy
    return calls


def _capture_log():
    import io
    from coord_engine.log import Logger
    stream = io.StringIO()
    return Logger("reconcile", level="info", stream=stream), stream


def _ack_change(slug, agent):
    return {"full_name": f"/team/r/_coord/acks/{slug}/{agent}.md",
            "state": "uploaded", "uploaded_at": "2026-07-01T16:10:00Z"}


def _acked(t):
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    return {r["name"]: r.get("acked_by") for r in agg["rows"]}


def _seeded():
    """A transport whose prior aggregate carries acked_by for every slug + a
    generated_at anchor — the state a steady-cadence host reconciles from."""
    t = _seed_acks(FakeTransport())
    _reconciled(t, now="2026-07-01T16:05:00Z")
    assert _acked(t) == {"a": ["amy"], "b": ["bob"], "c": ["cat"]}
    return t


def _ack_lists(calls):
    return [c for c in calls if c.startswith("team/r/_coord/acks")]


def test_acks_incremental_folds_only_changed_slugs():
    t = _seeded()
    _with_recent_changes(t, [_ack_change("b", "bob"),
                             {"full_name": "/team/r/presence/x.md"}])  # irrelevant churn
    t.put("team/r/_coord/acks/b/dan.md",
          "---\ntype: Ack\nagent: dan\ntimestamp: 2026-07-01T16:10:00Z\n---\n")
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert _ack_lists(calls) == ["team/r/_coord/acks/b/"]   # ONE dir, not all three
    assert _acked(t) == {"a": ["amy"], "b": ["bob", "dan"], "c": ["cat"]}
    assert len(t.rc_calls) == 1


def test_acks_unknown_change_query_falls_back_to_full_fold_and_says_so():
    """A 500/timeout/unparseable response is UNKNOWN, never 'nothing changed'."""
    t = _seeded()
    _with_recent_changes(t, None)                      # the endpoint 500s
    t.put("team/r/_coord/acks/b/dan.md",
          "---\ntype: Ack\nagent: dan\ntimestamp: 2026-07-01T16:10:00Z\n---\n")
    calls = _spy_lists(t)
    log, stream = _capture_log()
    reconcile.reconcile(t, "r", now="2026-07-01T16:15:00Z", today="2026-07-01",
                        host="h", logger=log)
    assert _ack_lists(calls) == ["team/r/_coord/acks/",   # the full fold: every dir
                                 "team/r/_coord/acks/a/",
                                 "team/r/_coord/acks/b/",
                                 "team/r/_coord/acks/c/"]
    assert _acked(t) == {"a": ["amy"], "b": ["bob", "dan"], "c": ["cat"]}
    out = stream.getvalue()
    assert "full fold" in out and "change query" in out   # degraded, loudly


def test_acks_no_change_query_support_falls_back_to_full_fold():
    t = _seeded()                                      # fake has no .recent_changes
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert "team/r/_coord/acks/" in _ack_lists(calls)
    assert _acked(t) == {"a": ["amy"], "b": ["bob"], "c": ["cat"]}


def test_acks_full_fold_without_an_ack_anchor():
    """A legacy aggregate (pre-1.6.8) carries no ack anchor: there is no window to
    ask about, so the pass must full-fold — not silently reuse the prior acks.
    generated_at is NOT a fallback anchor: it advances on every pass, including
    passes whose ack fold was inconclusive, so trusting it would reuse across a
    change we never read."""
    t = _seeded()
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    agg.pop(reconcile.ACKS_ANCHOR_KEY)
    assert agg["generated_at"]                         # present, and deliberately unused
    t.store["team/r/_coord/summaries.json"] = json.dumps(agg)
    _with_recent_changes(t, [])                        # query is healthy, anchor isn't
    t.put("team/r/_coord/acks/b/dan.md",
          "---\ntype: Ack\nagent: dan\ntimestamp: 2026-07-01T16:10:00Z\n---\n")
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert "team/r/_coord/acks/" in _ack_lists(calls)
    assert t.rc_calls == []                            # never asked: no usable anchor
    assert _acked(t)["b"] == ["bob", "dan"]


def test_acks_malformed_change_entry_falls_back_to_full_fold():
    t = _seeded()
    _with_recent_changes(t, [_ack_change("b", "bob"), {}])   # shape drift -> doubt
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert "team/r/_coord/acks/" in _ack_lists(calls)


def test_acks_new_slug_absent_from_prior_is_folded_not_assumed_empty():
    """A task that wasn't in the prior aggregate has no prior acked_by to reuse —
    its ack dir is folded even when the change query reports nothing for it."""
    t = _seeded()
    t.put("team/r/task/d.md", _task("D", "active"))
    t.put("team/r/_coord/acks/d/eve.md",
          "---\ntype: Ack\nagent: eve\ntimestamp: 2026-06-30T09:00:00Z\n---\n")
    _with_recent_changes(t, [])
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert _ack_lists(calls) == ["team/r/_coord/acks/d/"]
    assert _acked(t) == {"a": ["amy"], "b": ["bob"], "c": ["cat"], "d": ["eve"]}


def test_acks_periodic_backstop_forces_a_full_fold(monkeypatch):
    """A missed change can never persist indefinitely: every Nth pass full-folds
    even while the change query is healthy."""
    monkeypatch.setenv("COORD_ACKS_FULL_EVERY", "2")
    t = _seeded()                                      # seed pass = a full fold
    _with_recent_changes(t, [])
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-01T16:15:00Z")         # pass 1 -> incremental
    assert _ack_lists(calls) == []
    calls.clear()
    _reconciled(t, now="2026-07-01T16:25:00Z")         # pass 2 -> backstop
    assert "team/r/_coord/acks/" in _ack_lists(calls)
    calls.clear()
    _reconciled(t, now="2026-07-01T16:35:00Z")         # streak reset -> incremental
    assert _ack_lists(calls) == []


def test_acks_full_every_bad_value_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("COORD_ACKS_FULL_EVERY", "banana")
    t = _seeded()
    _with_recent_changes(t, [])
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert _ack_lists(calls) == []                     # default is > 1: incremental


def test_acks_full_every_one_disables_the_incremental_path(monkeypatch):
    monkeypatch.setenv("COORD_ACKS_FULL_EVERY", "1")
    t = _seeded()
    _with_recent_changes(t, [])
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert "team/r/_coord/acks/" in _ack_lists(calls)


def test_acks_stale_anchor_falls_back_to_full_fold():
    """An anchor older than the endpoint's usable window (a host that was down for
    days) would 500 the query; don't spend the op — full-fold."""
    t = _seeded()
    _with_recent_changes(t, [])
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-05T16:15:00Z")         # 4 days after the anchor
    assert "team/r/_coord/acks/" in _ack_lists(calls)
    assert t.rc_calls == []


def test_acks_gc_runs_on_full_fold_but_not_on_the_incremental_path():
    """GC is cleanup, not correctness: it rides the full fold (which already lists
    every dir) and is deliberately skipped on the incremental path, which never
    sees the orphan dirs. It is never DROPPED — the backstop full fold collects."""
    t = _seeded()
    t.put("team/r/_coord/acks/ghost/amy.md",
          "---\ntype: Ack\nagent: amy\ntimestamp: 2020-01-01T00:00:00Z\n---\n")
    _with_recent_changes(t, [])
    _reconciled(t, now="2026-07-01T16:15:00Z")         # incremental: no GC
    assert "team/r/_coord/acks/ghost/amy.md" in t.store
    _with_recent_changes(t, None)                      # unknown -> full fold -> GC
    _reconciled(t, now="2026-07-01T16:25:00Z")
    assert "team/r/_coord/acks/ghost/amy.md" not in t.store


def test_acks_change_query_window_covers_the_anchor_with_skew_margin():
    t = _seeded()
    _with_recent_changes(t, [])
    _reconciled(t, now="2026-07-01T16:15:00Z")
    (start, end), = t.rc_calls
    # anchor 16:05 - 15min margin, now 16:15 + 15min margin
    assert start == "2026-07-01T15:50:00Z" and end == "2026-07-01T16:30:00Z"


# --- the no-false-advance discipline (codex P1 on 56f9215) ---
#
# A fold that could not READ a slug it knew had changed must not let the pass
# behave as though it had. The ack anchor is what makes that enforceable: it is
# the engine's OWN record of what it has provably folded through, separate from
# generated_at (which the task path advances every pass, unconditionally). An
# inconclusive fold holds the anchor back, so the unread change stays inside the
# next pass's query window instead of being consumed by it.

def _anchor(t):
    return json.loads(t.store["team/r/_coord/summaries.json"]).get(reconcile.ACKS_ANCHOR_KEY)


def _streak(t):
    return json.loads(t.store["team/r/_coord/summaries.json"]).get(reconcile.ACKS_STREAK_KEY)


def _fail_list_for(t, *prefixes):
    """Make specific list_dir prefixes raise, leaving the rest of the fake intact."""
    orig = t.list_dir

    def guard(prefix):
        if prefix in prefixes:
            raise TransportError("boom")
        return orig(prefix)

    t.list_dir = guard


def _windowed_changes(entries):
    """A change query that answers HONESTLY: only entries inside [start, end].
    Anything the pass fails to consume must stay reachable by a later window —
    that is what these tests prove."""
    def files(start_iso, end_iso):
        return [e for e in entries if start_iso <= e["uploaded_at"] <= end_iso]
    return files


def test_acks_changed_slug_listing_failure_falls_back_to_full_fold():
    """(a) The slug we KNOW changed is the one we couldn't read: that is doubt, so
    the pass full-folds like any other doubt path — it does not reuse-and-continue."""
    t = _seeded()
    _with_recent_changes(t, [_ack_change("b", "dan")])
    t.put("team/r/_coord/acks/b/dan.md",
          "---\ntype: Ack\nagent: dan\ntimestamp: 2026-07-01T16:10:00Z\n---\n")
    _fail_list_for(t, "team/r/_coord/acks/b/")
    calls = _spy_lists(t)
    log, stream = _capture_log()
    reconcile.reconcile(t, "r", now="2026-07-01T16:15:00Z", today="2026-07-01",
                        host="h", logger=log)
    assert "team/r/_coord/acks/" in _ack_lists(calls)   # escalated to the full fold
    assert "full fold" in stream.getvalue()
    # b is unreadable, so its prior acks are PRESERVED, never stamped to []
    assert _acked(t) == {"a": ["amy"], "b": ["bob"], "c": ["cat"]}


def test_acks_inconclusive_fold_does_not_advance_the_anchor_or_streak():
    """(a) The false advance itself: a pass that failed to read a changed slug must
    not move the ack anchor past that change, nor spend a backstop pass on it."""
    t = _seeded()
    assert _anchor(t) == "2026-07-01T16:05:00Z" and _streak(t) == 0
    _with_recent_changes(t, [_ack_change("b", "dan")])
    _fail_list_for(t, "team/r/_coord/acks/b/")
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert _anchor(t) == "2026-07-01T16:05:00Z"        # held back, NOT advanced to 16:15
    assert _streak(t) == 0                             # not spent on false evidence


def test_acks_change_unfolded_in_one_pass_is_still_folded_in_the_next():
    """(b) The retry-window boundary — the point of holding the anchor. The change
    lands at 16:06; pass N (16:40) can't read it. If the anchor had advanced to
    16:40, pass N+1's window would start at 16:25 and the change would be gone for
    good (until the ~daily backstop (DEFAULT_ACKS_FULL_EVERY=72 at a 20-min heartbeat)). With the anchor held at 16:05, it is still in
    the window and folds."""
    t = _seeded()                                      # anchor: 2026-07-01T16:05:00Z
    changes = [{"full_name": "/team/r/_coord/acks/b/dan.md",
                "state": "uploaded", "uploaded_at": "2026-07-01T16:06:00Z"}]
    _with_recent_changes(t, _windowed_changes(changes))
    t.put("team/r/_coord/acks/b/dan.md",
          "---\ntype: Ack\nagent: dan\ntimestamp: 2026-07-01T16:06:00Z\n---\n")
    _fail_list_for(t, "team/r/_coord/acks/b/")
    _reconciled(t, now="2026-07-01T16:40:00Z")         # pass N: b unreadable
    assert _acked(t)["b"] == ["bob"] and _anchor(t) == "2026-07-01T16:05:00Z"

    t.list_dir = FakeTransport.list_dir.__get__(t)     # transport recovers
    calls = _spy_lists(t)
    _reconciled(t, now="2026-07-01T16:45:00Z")         # pass N+1
    (start, _end), = t.rc_calls[-1:]
    assert start == "2026-07-01T15:50:00Z"             # the HELD anchor - margin
    assert _acked(t)["b"] == ["bob", "dan"]            # the change is not lost
    assert _ack_lists(calls) == ["team/r/_coord/acks/b/"]   # and it stayed cheap
    assert _anchor(t) == "2026-07-01T16:45:00Z"        # conclusive now -> advances


def test_acks_full_fold_preserves_prior_acks_when_the_root_listing_fails():
    """(c) A transport failure must never DROP acknowledgements. The full fold's
    error paths return an incomplete map, and the caller stamps every missing slug
    to [] — so an unreadable ack root would silently un-ack every task."""
    t = _seeded()
    _fail_list_for(t, "team/r/_coord/acks/")           # no change query -> full fold
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert _acked(t) == {"a": ["amy"], "b": ["bob"], "c": ["cat"]}   # preserved, not []
    assert _anchor(t) == "2026-07-01T16:05:00Z"        # inconclusive -> anchor held


def test_acks_full_fold_preserves_prior_acks_when_one_slug_listing_fails():
    """(c) Same guarantee per-slug: one unreadable ack dir must not un-ack its task."""
    t = _seeded()
    _fail_list_for(t, "team/r/_coord/acks/b/")
    _reconciled(t, now="2026-07-01T16:15:00Z")
    assert _acked(t) == {"a": ["amy"], "b": ["bob"], "c": ["cat"]}
    assert _anchor(t) is None or _anchor(t) == "2026-07-01T16:05:00Z"


def test_acks_first_pass_with_an_unreadable_root_writes_no_anchor():
    """No prior acks to preserve and nothing provably folded: write no anchor at
    all, so the next pass full-folds rather than trusting a window it never read."""
    t = _seed_acks(FakeTransport())
    _fail_list_for(t, "team/r/_coord/acks/")
    _reconciled(t, now="2026-07-01T16:05:00Z")
    assert _acked(t) == {"a": [], "b": [], "c": []}    # nothing known, nothing claimed
    assert _anchor(t) is None


def test_acks_conclusive_full_fold_advances_the_anchor():
    t = _seeded()
    assert _anchor(t) == "2026-07-01T16:05:00Z"
    _reconciled(t, now="2026-07-01T16:15:00Z")         # no query support -> full fold
    assert _anchor(t) == "2026-07-01T16:15:00Z"


# --- the global fast path may only fire when EVERY sub-fold is settled (codex r2) ---
#
# Holding the ack anchor is necessary but useless if a pass can skip the fold that
# reads it. The fast path builds its window from generated_at, which advances even
# on a pass whose ack fold was inconclusive — so it would happily skip over an ack
# change that is still owed, and the held anchor would never be queried. Same shape
# as the v1.6.7 stale-schema-stamp guard: settle first, then skip.

def test_fast_path_declines_while_the_ack_fold_owes_a_pass():
    """The two-pass recovery: an inconclusive ack fold at 16:40 must not be
    stranded by a quiet global feed at 16:45. Both transport capabilities are
    live, which is the whole point — `updates` sees nothing in the 16:40->16:45
    window, but the ack fold is still owed the 16:06 change from BEFORE it."""
    t = _seeded()                                      # anchor == generated_at == 16:05
    changes = [{"full_name": "/team/r/_coord/acks/b/dan.md",
                "state": "uploaded", "uploaded_at": "2026-07-01T16:06:00Z"}]
    _with_recent_changes(t, _windowed_changes(changes))
    t.put("team/r/_coord/acks/b/dan.md",
          "---\ntype: Ack\nagent: dan\ntimestamp: 2026-07-01T16:06:00Z\n---\n")
    _fail_list_for(t, "team/r/_coord/acks/b/")
    _reconciled(t, now="2026-07-01T16:40:00Z")         # inconclusive: anchor held...
    assert _anchor(t) == "2026-07-01T16:05:00Z"        # ...at 16:05, generated_at at 16:40
    assert _acked(t)["b"] == ["bob"]

    t.list_dir = FakeTransport.list_dir.__get__(t)     # transport recovers
    _with_updates(t, [])                               # global feed: nothing since 16:40
    log, stream = _capture_log()
    res = reconcile.reconcile(t, "r", now="2026-07-01T16:45:00Z", today="2026-07-01",
                              host="h", logger=log)
    assert not res.get("fast_path"), "ack evidence is inconclusive — must not skip the fold"
    assert "ack fold" in stream.getvalue()             # declined for its OWN reason
    assert _acked(t)["b"] == ["bob", "dan"]            # the 16:06 ack finally lands
    assert _anchor(t) == "2026-07-01T16:45:00Z"        # conclusive -> anchor advances


def test_fast_path_resumes_once_the_ack_fold_is_settled():
    """The guard is a settle-first rule, not a permanent block: once a fold is
    conclusive (anchor == generated_at), a quiet beat takes the fast path again."""
    t = _seeded()
    _with_recent_changes(t, [])
    _reconciled(t, now="2026-07-01T16:15:00Z")         # conclusive incremental fold
    assert _anchor(t) == "2026-07-01T16:15:00Z"
    _with_updates(t, [])
    assert _reconciled(t, now="2026-07-01T16:20:00Z").get("fast_path") is True


def test_fast_path_declines_on_a_legacy_aggregate_without_an_ack_anchor():
    """A pre-1.6.8 aggregate has never had a conclusive ack fold recorded. Skipping
    on the strength of its generated_at would reuse acks nothing ever verified."""
    t = _seeded()
    agg = json.loads(t.store["team/r/_coord/summaries.json"])
    agg.pop(reconcile.ACKS_ANCHOR_KEY)
    t.store["team/r/_coord/summaries.json"] = json.dumps(agg)
    _with_updates(t, [])
    assert not _reconciled(t, now="2026-07-01T16:15:00Z").get("fast_path")
    assert _anchor(t) == "2026-07-01T16:15:00Z"        # full pass settles it


# --- aggregate round-trip persistence (v1.6.9) ---------------------------------
#
# WHY THIS CLASS EXISTS. v1.6.8 shipped the ack fold's anchor + streak as
# top-level aggregate keys. Every test written for that feature exercised the fold
# in ISOLATION, against a hand-built prior-aggregate dict — so the whole suite
# would have stayed green even if reconcile had never persisted those keys at all,
# and neither review caught the gap. The class of test that closes it: reconcile
# for real, read the aggregate back OUT of the transport (never from an in-memory
# return), and feed it to the next pass. Anything that must survive a pass gets a
# test HERE, not just a fold-level one.
#
# The live-fleet lesson these pin (see build_aggregate's docstring): the aggregate
# is one shared document written by many hosts at many versions, and a top-level
# key added in version N is wiped by any host older than N. Preservation cannot
# save us from a host that predates the key, but it stops a ≥v1.6.9 host from
# erasing a NEWER host's fold state — the same defect, one version later.

class TestAggregateRoundTrip:
    """Real write -> read-back-from-the-transport -> write cycles."""

    def _agg_from_store(self, t):
        """The aggregate as the NEXT pass will actually see it: parsed from the
        transport's bytes, not the reconcile return value. The distinction is the
        entire point of this class."""
        return json.loads(t.store["team/r/_coord/summaries.json"])

    def test_ack_anchor_and_streak_survive_a_real_write_read_cycle(self):
        """The keys must be in the JSON the transport holds — not merely computed."""
        t = _seed_acks(FakeTransport())
        _reconciled(t, now="2026-07-01T16:05:00Z")
        agg = self._agg_from_store(t)
        assert agg[reconcile.ACKS_ANCHOR_KEY] == "2026-07-01T16:05:00Z"
        assert agg[reconcile.ACKS_STREAK_KEY] == 0     # full fold resets the counter

    def test_pass_two_reads_the_persisted_anchor_and_folds_incrementally(self):
        """The end-to-end claim v1.6.8 makes and no test checked: pass 2 picks the
        anchor up OFF THE STORE and takes the incremental path. If persistence
        breaks, pass 2 silently full-folds every ack dir forever — the exact
        production symptom a mixed fleet exhibits."""
        t = _seed_acks(FakeTransport())
        _reconciled(t, now="2026-07-01T16:05:00Z")
        anchor = self._agg_from_store(t)[reconcile.ACKS_ANCHOR_KEY]

        _with_recent_changes(t, [])                    # store reports no ack changes
        calls = _spy_lists(t)
        _reconciled(t, now="2026-07-01T16:15:00Z")

        assert _ack_lists(calls) == [], "pass 2 full-folded: it did not read the anchor"
        # ...and the query it ran was anchored on the PERSISTED value, not on
        # generated_at (they differ here only by the pass cadence, so assert the
        # window start is derived from the anchor we actually wrote).
        assert t.rc_calls, "no change query ran"
        start, _end = t.rc_calls[0]
        assert start <= anchor, "window must start at/before the persisted anchor"
        assert self._agg_from_store(t)[reconcile.ACKS_STREAK_KEY] == 1  # incremental

    def test_streak_accumulates_across_round_trips(self):
        """The backstop counter is only a backstop if it survives passes."""
        t = _seed_acks(FakeTransport())
        _reconciled(t, now="2026-07-01T16:05:00Z")
        _with_recent_changes(t, [])
        for i, now in enumerate(("2026-07-01T16:15:00Z", "2026-07-01T16:35:00Z"), start=1):
            _reconciled(t, now=now)
            assert self._agg_from_store(t)[reconcile.ACKS_STREAK_KEY] == i

    def test_unknown_top_level_keys_from_a_newer_host_survive_a_pass(self):
        """A future version's fold state must not be erased by this build. We
        cannot interpret the key — we carry it."""
        t = _seed_acks(FakeTransport())
        _reconciled(t, now="2026-07-01T16:05:00Z")
        agg = self._agg_from_store(t)
        agg["some_future_fold_anchor"] = "2026-07-01T16:06:00Z"
        agg["some_future_state"] = {"nested": [1, 2]}
        t.store["team/r/_coord/summaries.json"] = json.dumps(agg)

        _with_recent_changes(t, [])
        _reconciled(t, now="2026-07-01T16:15:00Z")

        after = self._agg_from_store(t)
        assert after["some_future_fold_anchor"] == "2026-07-01T16:06:00Z"
        assert after["some_future_state"] == {"nested": [1, 2]}

    def test_this_builds_own_keys_win_over_the_prior_aggregates(self):
        """Preservation must not resurrect stale state: a key this build OWNS is
        recomputed every pass and overwrites whatever the prior aggregate held."""
        t = _seed_acks(FakeTransport())
        _reconciled(t, now="2026-07-01T16:05:00Z")
        agg = self._agg_from_store(t)
        agg[reconcile.ACKS_ANCHOR_KEY] = "1999-01-01T00:00:00Z"
        agg["reconcile_host"] = "some-other-host"
        agg["schema"] = "bogus"
        t.store["team/r/_coord/summaries.json"] = json.dumps(agg)

        _reconciled(t, now="2026-07-01T16:15:00Z")     # no change query -> full fold

        after = self._agg_from_store(t)
        assert after[reconcile.ACKS_ANCHOR_KEY] == "2026-07-01T16:15:00Z"
        assert after["reconcile_host"] == "h"
        assert after["schema"] == aggregate_mod.SCHEMA

    def test_a_pre_anchor_host_wipes_the_anchor(self):
        """The live production defect, pinned as a REGRESSION TEST OF RECORD.

        ArcBot et al. run v1.6.6, which predates the anchor entirely: its
        build_aggregate returns exactly six keys, so every pass it makes against
        the shared index deletes the anchor this build wrote. Preservation cannot
        help — v1.6.6 has no code to preserve with. This test documents that the
        heal is a FLEET UPGRADE, not a code change here, and it should start
        failing (and be deleted) once no pre-1.6.9 host writes the index.
        """
        t = _seed_acks(FakeTransport())
        _reconciled(t, now="2026-07-01T16:05:00Z")
        assert self._agg_from_store(t)[reconcile.ACKS_ANCHOR_KEY]

        # Simulate one v1.6.6 pass: its build_aggregate, verbatim.
        legacy = self._agg_from_store(t)
        t.store["team/r/_coord/summaries.json"] = json.dumps({
            "schema": legacy["schema"], "team": legacy["team"],
            "generated_at": "2026-07-01T16:10:00Z", "reconcile_host": "arcbot",
            "rows": legacy["rows"], "warnings": [],
        })

        _with_recent_changes(t, [])
        calls = _spy_lists(t)
        _reconciled(t, now="2026-07-01T16:15:00Z")
        # No anchor -> full fold. Our incremental path is dead until the fleet moves.
        assert _ack_lists(calls), "expected the full fold a wiped anchor forces"
