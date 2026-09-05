"""Same-second verdicts order by chronology, not digest; `review status --json` exposes the exact
winning shard per reviewer.

codex-reviewer reproduced, on coord-fold-plan-r2-65761fbd round 12 (2026-09-05): an earlier APPROVE
whose digest (feb86aee) sorted after a later CHANGES (058ddb93) in the same second, so the fold kept
the APPROVE and a ship gate would have read stale approving evidence. Both reviewers: fix the typed
surface, expose the winning envelope, do not refold filenames downstream.
"""
from __future__ import annotations

import json

from coord_engine import cli, okf, review
from coord_engine_test_helpers import FakeTransport

TEAM, SLUG, HEAD, REVIEWER = "r", "pr-1-thing", "a" * 40, "codex-reviewer"
PREFIX = f"team/{TEAM}/review/{SLUG}/verdicts/"
SECOND = "2026-09-05T01:32:10"


def _open_review(t, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_AGENT", "asker")
    assert cli.main(["review", "request", TEAM, SLUG, "--of", "PR #1", "--reviewer", REVIEWER, "--head", HEAD], transport=t) == 0


def _shard(t, digest, verdict, fraction):
    name = review.verdict_filename(REVIEWER, head=HEAD, ts=f"{SECOND}Z", digest=digest)
    fm = {"type": "Verdict", "reviewer": REVIEWER, "head": HEAD, "verdict": verdict}
    if fraction is not None:
        fm["ts"] = f"{SECOND}.{fraction}Z"
    t.store[PREFIX + name] = okf.render_frontmatter(fm) + f"\n{verdict}\n"
    return name


# --- the key ---------------------------------------------------------------------------

def test_canonical_key_takes_the_second_from_the_name_and_the_fraction_only_within_it():
    assert review.canonical_sort_key(f"{SECOND}Z", f"{SECOND}.900000Z", None) == f"{SECOND}.900000Z"
    assert review.canonical_sort_key(f"{SECOND}Z", "2026-09-05T01:32:11.900000Z", None) == f"{SECOND}.000000Z"   # frontmatter cannot move across seconds
    assert review.canonical_sort_key(f"{SECOND}Z", "", None) == f"{SECOND}.000000Z"                             # legacy shard: no fraction
    assert review.canonical_sort_key(None, f"{SECOND}.5Z", None) == f"{SECOND}.500000Z"                        # plain shard: frontmatter is all there is
    assert review.canonical_sort_key(None, None, f"{SECOND}Z") == f"{SECOND}.000000Z"                          # mtime fallback
    assert review.canonical_sort_key(None, None, None) == ""


def test_legacy_and_new_shards_compare_in_one_format():
    legacy = review.canonical_sort_key(f"{SECOND}Z", "", None)
    newer = review.canonical_sort_key(f"{SECOND}Z", f"{SECOND}.000001Z", None)
    assert legacy < newer and len(legacy) == len(newer)


# --- the fold -----------------------------------------------------------------------------

def _row(digest, verdict, fraction):
    name = review.verdict_filename(REVIEWER, head=HEAD, ts=f"{SECOND}Z", digest=digest)
    fm_ts = f"{SECOND}.{fraction}Z" if fraction is not None else ""
    return {"reviewer": REVIEWER, "name": name, "verdict": verdict,
            "sort_key": review.canonical_sort_key(f"{SECOND}Z", fm_ts, None)}


def test_same_second_reverse_digest_the_later_changes_wins():
    earlier_approve = _row("feb86aee", "approve", "100000")   # larger digest, EARLIER
    later_changes = _row("058ddb93", "changes", "900000")     # smaller digest, LATER
    kept, folded = review.fold_newest_per_reviewer([earlier_approve, later_changes])
    assert kept == [later_changes] and folded == 1


def test_without_fractions_an_unnamed_conflict_fails_closed_to_changes():
    """Two same-second shards with NO frontmatter ts and no supersession link: the CHANGES dominates."""
    a = _row("feb86aee", "approve", None)
    b = _row("058ddb93", "changes", None)
    kept, _ = review.fold_newest_per_reviewer([a, b])
    assert kept == [b]


# --- the typed surface --------------------------------------------------------------------

def test_review_status_json_exposes_the_exact_winning_shard(monkeypatch, capsys):
    t = FakeTransport()
    _open_review(t, monkeypatch)
    _shard(t, "feb86aee", "approve", "100000")
    later = _shard(t, "058ddb93", "changes", "900000")
    assert cli.main(["review", "status", TEAM, SLUG, "--json"], transport=t) == 0
    out = json.loads([l for l in capsys.readouterr().out.splitlines() if l.startswith("{")][-1])
    assert out["state"] == "CHANGES"
    assert out["winning"][REVIEWER]["name"] == later
    assert out["winning"][REVIEWER]["verdict"] == "changes"
    assert out["winning"][REVIEWER]["sort_key"] == f"{SECOND}.900000Z"


def test_the_verb_writes_a_microsecond_ts_in_frontmatter(monkeypatch):
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "approve", "--note", "tree: " + "1" * 40], transport=t) == 0
    shards = sorted(k for k in t.store if k.startswith(PREFIX + f"{HEAD}--{REVIEWER}--"))
    assert shards
    fm = okf.parse_frontmatter(t.store[shards[-1]])
    name_second = shards[-1].rsplit("--", 1)[1][:19]
    assert str(fm["ts"])[:19] == name_second and str(fm["ts"]).endswith("Z")


# --- the projection and the generation-backed surface (codex-coder, review-winning-envelope r1) ---

def test_the_projection_orders_same_second_shards_by_chronology_and_records_winning():
    """Two canonical readers must not disagree: reconcile over the same directory must keep the
    later CHANGES and say which shard it kept."""
    from coord_engine import projection, reconcile
    t = FakeTransport()
    _open_review(t, __import__("pytest").MonkeyPatch())
    _shard(t, "feb86aee", "approve", "100000")
    later = _shard(t, "058ddb93", "changes", "900000")
    now = "2026-09-05T02:30:00Z"
    reconcile.reconcile(t, TEAM, now=now, today=now[:10], host="h")
    sec = json.loads(t.store[f"team/{TEAM}/_coord/summaries.json"])[projection.REVIEWS_KEY]
    row = next(r for r in sec["rows"] if r["name"] == SLUG)
    assert row["state"] == "CHANGES"
    assert row["tally"]["winning"][REVIEWER]["name"] == later
    assert row["tally"]["winning"][REVIEWER]["verdict"] == "changes"
    assert row["tally"]["winning"][REVIEWER]["sort_key"] == f"{SECOND}.900000Z"


class _Authority:
    """The minimum a validated generation needs to be served through the public-read branch."""
    def __init__(self, row):
        self._row = row

    def section(self, name):
        return {"rows": [self._row]} if name == "reviews" else None


def _generation_row(tally):
    return {"name": SLUG, "tally": tally}


def _served(monkeypatch, capsys, tally):
    token = cli._PUBLIC_READ_CONTEXT.set(_Authority(_generation_row(tally)))
    try:
        rc = cli.main(["review", "status", TEAM, SLUG, "--json"], transport=FakeTransport())
    finally:
        cli._PUBLIC_READ_CONTEXT.reset(token)
    out = capsys.readouterr()
    return rc, out


def _base_tally(**extra):
    t = {"state": "APPROVED", "approvals": [REVIEWER], "changes": [], "required": [REVIEWER],
         "pending_required": [], "evidence": "", "of": "PR #1", "head": HEAD}
    t.update(extra)
    return t


def test_generation_backed_status_serves_winning(monkeypatch, capsys):
    name = review.verdict_filename(REVIEWER, head=HEAD, ts=f"{SECOND}Z", digest="058ddb93")
    rc, out = _served(monkeypatch, capsys, _base_tally(winning={REVIEWER: {"name": name, "verdict": "approve", "sort_key": f"{SECOND}.900000Z"}}))
    assert rc == 0
    assert json.loads([l for l in out.out.splitlines() if l.startswith("{")][-1])["winning"][REVIEWER]["name"] == name


def test_generation_backed_status_fails_closed_without_winning(monkeypatch, capsys):
    """An old generation (projected before this change) cannot prove which shard won: rc 3, not a
    tally that a ship gate would read as the whole truth."""
    rc, out = _served(monkeypatch, capsys, _base_tally())
    assert rc == 3 and "does not record the winning shard" in out.err


# --- the writer samples the clock once (both reviewers, review-winning-envelope r3) -----------

def test_the_verb_samples_the_clock_once_so_name_and_frontmatter_seconds_agree(monkeypatch):
    """A clock that returns adjacent seconds on successive calls: with two samples the name says
    :10Z and the frontmatter :11.000001Z, the fraction is discarded, and the later correction loses."""
    from datetime import datetime, timezone
    t = FakeTransport()
    _open_review(t, monkeypatch)
    _shard(t, "feb86aee", "approve", "900000")                 # earlier APPROVE at :10.900000
    ticks = iter([datetime(2026, 9, 5, 1, 32, 10, 999999, tzinfo=timezone.utc),
                  datetime(2026, 9, 5, 1, 32, 11, 1, tzinfo=timezone.utc)])
    monkeypatch.setattr(cli, "_now", lambda: next(ticks))
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "changes", "--note", "later correction"], transport=t) == 0
    new = [k for k in t.store if k.startswith(PREFIX + f"{HEAD}--{REVIEWER}--") and "feb86aee" not in k]
    assert len(new) == 1
    fm = okf.parse_frontmatter(t.store[new[0]])
    name_second = new[0].rsplit("--", 1)[1][:19]
    assert str(fm["ts"])[:19] == name_second == "2026-09-05T01:32:10"
    assert str(fm["ts"]).startswith("2026-09-05T01:32:10.999999")
    tally, *_ = cli._review_tally(t, TEAM, SLUG)
    assert tally["state"] == "CHANGES"
    assert tally["winning"][REVIEWER]["name"] == new[0].rsplit("/", 1)[-1]


# --- explicit supersession (codex-coder, review-winning-envelope r4) ----------------------------

def _row2(name, verdict, key, supersedes=(), digest="", mtime=""):
    return {"reviewer": REVIEWER, "name": name, "verdict": verdict, "sort_key": key, "supersedes": list(supersedes), "digest": digest, "mtime_iso": mtime}


T0, T1, T2 = "2026-09-05T12:00:00Z", "2026-09-05T12:01:00Z", "2026-09-05T12:02:00Z"


A = review.verdict_filename(REVIEWER, head=HEAD, ts="2026-09-05T12:00:10Z", digest="aaaaaaaa")
C = review.verdict_filename(REVIEWER, head=HEAD, ts="2026-09-05T12:00:09Z", digest="cccccccc")


def test_a_later_withdrawal_from_a_host_whose_clock_is_behind_still_dominates():
    approve = _row2(A, "approve", "2026-09-05T12:00:10.900000Z")
    changes_from_slow_host = _row2(C, "changes", "2026-09-05T12:00:09.900000Z")   # filed LATER, stamped earlier
    kept, _ = review.fold_newest_per_reviewer([approve, changes_from_slow_host])
    assert kept[0]["name"] == C


def test_equal_timestamps_fail_closed_to_changes():
    approve = _row2(A, "approve", "2026-09-05T12:00:10.900000Z")
    changes = _row2(C, "changes", "2026-09-05T12:00:10.900000Z")
    assert review.fold_newest_per_reviewer([approve, changes])[0][0]["name"] == C


def test_an_approve_lifts_a_changes_only_by_naming_it():
    changes = _row2(C, "changes", "2026-09-05T12:00:09.900000Z", digest="d1", mtime=T0)
    approve_naming = _row2(A, "approve", "2026-09-05T12:00:08.000000Z", supersedes=[C + "@sha256:d1"], mtime=T1)   # even with an EARLIER client stamp
    assert review.fold_newest_per_reviewer([changes, approve_naming])[0][0]["name"] == A
    approve_dangling = _row2(A, "approve", "2026-09-05T12:00:11.000000Z", supersedes=["no-such-shard.md"], mtime=T1)
    assert review.fold_newest_per_reviewer([changes, approve_dangling])[0][0]["name"] == C


def test_the_verb_names_every_prior_shard_so_a_verb_filed_approve_lifts_a_prior_changes(monkeypatch):
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "changes", "--note", "first"], transport=t) == 0
    tally, *_ = cli._review_tally(t, TEAM, SLUG)
    assert tally["state"] == "CHANGES"
    changes_path = next(k for k in t.store if k.startswith(PREFIX + f"{HEAD}--{REVIEWER}--"))
    t.mtimes[changes_path] = "2026-09-05 12:00PM UTC"                    # the STORE dates the prior
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "approve", "--note", "fixed"], transport=t) == 0
    shards = {k: okf.parse_frontmatter(t.store[k]) for k in t.store if k.startswith(PREFIX + f"{HEAD}--{REVIEWER}--")}
    # pick the APPROVE by its frontmatter — NOT by sorting names, which is the digest-order defect itself
    approve_path = next(k for k, fm in shards.items() if fm["verdict"] == "approve")
    c_name = changes_path.rsplit("/", 1)[-1]
    assert shards[approve_path]["supersedes"] == [f"{c_name}@sha256:{review.content_digest(t.store[changes_path])}"]   # the target's bytes (r8)
    t.mtimes[approve_path] = "2026-09-05 12:01PM UTC"                    # ...and dates the superseder later
    tally, *_ = cli._review_tally(t, TEAM, SLUG)
    assert tally["state"] == "APPROVED" and tally["winning"][REVIEWER]["name"] == approve_path.rsplit("/", 1)[-1]


def test_a_degraded_listing_makes_the_verb_supersede_nothing_and_say_so(monkeypatch, capsys):
    from coord_engine.transport import TransportError
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "changes", "--note", "first"], transport=t) == 0
    real = t.list_dir
    def flaky(prefix):
        if prefix.endswith("/verdicts/"):
            raise TransportError("degraded")
        return real(prefix)
    monkeypatch.setattr(t, "list_dir", flaky)
    # rc 3, not 0: the verdict LANDS, and the verb's existing post-write settle-marker check also
    # cannot list the prefix, so it reports "recorded, but I cannot tell whether a settle marker exists".
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "approve", "--note", "fixed"], transport=t) == 3
    err = capsys.readouterr().err
    assert "supersedes NOTHING" in err
    monkeypatch.setattr(t, "list_dir", real)
    tally, *_ = cli._review_tally(t, TEAM, SLUG)
    assert tally["state"] == "CHANGES"


# --- invalid supersession edges (codex-reviewer, review-winning-envelope r5) ------------------

def test_a_changes_that_names_itself_cannot_erase_itself():
    """Reproduced by codex-reviewer on 6ab678cb: old APPROVE + newer CHANGES whose supersedes holds its
    own filename -> the CHANGES left the live set and the fold said APPROVED."""
    approve = _row2(A, "approve", "2026-09-05T12:00:10.000000Z")
    self_erasing_changes = _row2(C, "changes", "2026-09-05T12:00:11.000000Z", supersedes=[C + "@sha256:beef"], digest="beef", mtime=T1)
    kept, _ = review.fold_newest_per_reviewer([approve, self_erasing_changes])
    assert kept[0]["name"] == C
    bad = review.invalid_supersession_edges([approve, self_erasing_changes])
    assert bad == [{"shard": C, "edge": C + "@sha256:beef", "why": "self-link"}]


def test_a_cycle_fails_closed_to_changes():
    approve = _row2(A, "approve", "2026-09-05T12:00:10.000000Z", supersedes=[C + "@sha256:cc"], digest="aa", mtime=T0)
    changes = _row2(C, "changes", "2026-09-05T12:00:11.000000Z", supersedes=[A + "@sha256:aa"], digest="cc", mtime=T1)
    assert review.fold_newest_per_reviewer([approve, changes])[0][0]["name"] == C


def test_a_cross_reviewer_edge_resolves_nothing_and_is_reported():
    other = {"reviewer": "someone-else", "name": "x--someone-else.md", "verdict": "changes", "sort_key": "2026-09-05T12:00:10.000000Z", "supersedes": []}
    approve_naming_other = _row2(A, "approve", "2026-09-05T12:00:11.000000Z", supersedes=["x--someone-else.md"], mtime=T1)
    kept, _ = review.fold_newest_per_reviewer([other, approve_naming_other])
    assert {r["reviewer"]: r["verdict"] for r in kept} == {"someone-else": "changes", REVIEWER: "approve"}
    assert review.invalid_supersession_edges([other, approve_naming_other]) == [{"shard": A, "edge": "x--someone-else.md", "why": "resolves nothing"}]


def test_malformed_edges_surface_in_the_direct_tally_and_the_projection(monkeypatch):
    from coord_engine import projection, reconcile
    t = FakeTransport()
    _open_review(t, monkeypatch)
    _shard(t, "aaaaaaaa", "approve", "100000")
    name_c = review.verdict_filename(REVIEWER, head=HEAD, ts=f"{SECOND}Z", digest="cccccccc")
    t.store[PREFIX + name_c] = okf.render_frontmatter({"type": "Verdict", "reviewer": REVIEWER, "head": HEAD, "verdict": "changes",
                                                       "ts": f"{SECOND}.900000Z", "supersedes": [name_c]}) + "\nself-link\n"
    tally, *_ = cli._review_tally(t, TEAM, SLUG)
    assert tally["state"] == "CHANGES" and tally["malformed_supersedes"][0]["why"] == "self-link"
    now = "2026-09-05T02:30:00Z"
    reconcile.reconcile(t, TEAM, now=now, today=now[:10], host="h")
    row = next(r for r in json.loads(t.store[f"team/{TEAM}/_coord/summaries.json"])[projection.REVIEWS_KEY]["rows"] if r["name"] == SLUG)
    assert row["state"] == "CHANGES" and row["tally"]["malformed_supersedes"][0]["why"] == "self-link"


# --- causal supersession: the store's facts, not the client's (both reviewers, r7) ----------

def test_a_predeclared_edge_to_a_later_written_target_never_resolves():
    """codex-coder's r7 reproduction: an older APPROVE predeclares the edge (name and even the digest);
    the CHANGES written LATER must stay live — the store's mtime says it came after."""
    later_changes = _row2(C, "changes", "2026-09-05T12:00:11.000000Z", digest="chosen", mtime=T1)
    older_approve = _row2(A, "approve", "2026-09-05T12:00:10.000000Z", supersedes=[C + "@sha256:chosen"], mtime=T0)
    kept, _ = review.fold_newest_per_reviewer([older_approve, later_changes])
    assert kept[0]["name"] == C
    assert review.invalid_supersession_edges([older_approve, later_changes]) == [{"shard": A, "edge": C + "@sha256:chosen", "why": "no causal proof"}]


def test_an_in_place_rewrite_of_a_resolved_target_un_resolves_it():
    """codex-reviewer's r7 reproduction: plain CHANGES C resolved by A; C rewritten in place as a later
    CHANGES (any client field kept) — its digest AND its store mtime both change; the old edge is void."""
    c_v1 = _row2(C, "changes", "2026-09-05T12:00:09.000000Z", digest="v1", mtime=T0)
    a = _row2(A, "approve", "2026-09-05T12:00:10.000000Z", supersedes=[C + "@sha256:v1"], mtime=T1)
    assert review.fold_newest_per_reviewer([c_v1, a])[0][0]["name"] == A
    c_v2 = _row2(C, "changes", "2026-09-05T12:00:12.000000Z", digest="v2", mtime=T2)     # rewritten in place
    assert review.fold_newest_per_reviewer([c_v2, a])[0][0]["name"] == C
    assert review.invalid_supersession_edges([c_v2, a])[0]["why"] == "digest mismatch"


def test_same_minute_or_unknown_store_mtime_is_not_proof():
    changes = _row2(C, "changes", "2026-09-05T12:00:09.000000Z", digest="d1", mtime=T0)
    same_minute = _row2(A, "approve", "2026-09-05T12:00:10.000000Z", supersedes=[C + "@sha256:d1"], mtime=T0)
    assert review.fold_newest_per_reviewer([changes, same_minute])[0][0]["name"] == C
    unknown = _row2(A, "approve", "2026-09-05T12:00:10.000000Z", supersedes=[C + "@sha256:d1"])
    assert review.fold_newest_per_reviewer([changes, unknown])[0][0]["name"] == C
    assert review.invalid_supersession_edges([changes, unknown])[0]["why"] == "no causal proof"


def test_a_legacy_bare_name_needs_the_same_mtime_proof():
    legacy_changes = _row2(C, "changes", "2026-09-05T12:00:09.000000Z", mtime=T0)
    assert review.fold_newest_per_reviewer([legacy_changes, _row2(A, "approve", "x", supersedes=[C], mtime=T1)])[0][0]["name"] == A
    assert review.fold_newest_per_reviewer([legacy_changes, _row2(A, "approve", "x", supersedes=[C], mtime=T0)])[0][0]["name"] == C


def _mt(iso):
    """Store-format mtime string the fake lists back ('%Y-%m-%d %I:%M%p %Z')."""
    from datetime import datetime, timezone
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).strftime("%Y-%m-%d %I:%M%p UTC")


def test_the_verb_quotes_the_priors_content_digest_and_the_store_mtime_decides(monkeypatch):
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "changes", "--note", "first"], transport=t) == 0
    changes_path = next(k for k in t.store if k.startswith(PREFIX + f"{HEAD}--{REVIEWER}--"))
    t.mtimes[changes_path] = _mt(T0)                                     # the store says: written at T0
    assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "approve", "--note", "fixed"], transport=t) == 0
    approve_path = next(k for k in t.store if k.startswith(PREFIX + f"{HEAD}--{REVIEWER}--") and k != changes_path)
    fm = okf.parse_frontmatter(t.store[approve_path])
    c_name = changes_path.rsplit("/", 1)[-1]
    assert fm["supersedes"] == [f"{c_name}@sha256:{review.content_digest(t.store[changes_path])}"]
    assert len(fm["nonce"]) == 16
    t.mtimes[approve_path] = _mt(T1)                                     # the store says: written at T1
    tally, *_ = cli._review_tally(t, TEAM, SLUG)
    assert tally["state"] == "APPROVED" and tally["winning"][REVIEWER]["name"] == approve_path.rsplit("/", 1)[-1]
    t.mtimes[approve_path] = _mt(T0)                                     # same minute as its target: no proof
    tally, *_ = cli._review_tally(t, TEAM, SLUG)
    assert tally["state"] == "CHANGES" and tally["malformed_supersedes"][0]["why"] == "no causal proof"


def test_identical_same_second_filings_get_distinct_names(monkeypatch):
    """codex-reviewer's r7 secondary: the name digest was reviewer|verdict|note at second precision."""
    from datetime import datetime, timezone
    t = FakeTransport()
    _open_review(t, monkeypatch)
    monkeypatch.setenv("FULCRA_COORD_AGENT", REVIEWER)
    monkeypatch.setattr(cli, "_now", lambda: datetime(2026, 9, 5, 1, 32, 10, 5, tzinfo=timezone.utc))
    for _ in range(2):
        assert cli.main(["review", "verdict", TEAM, SLUG, "--head", HEAD, "--verdict", "changes", "--note", "same"], transport=t) == 0
    assert len([k for k in t.store if k.startswith(PREFIX + f"{HEAD}--{REVIEWER}--")]) == 2
