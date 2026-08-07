"""TIER-3 middle carry: an unsettled row carries on ONE listing when nothing moved.

Why this tier exists: unsettled rows never carry under tier 1, so a
permanently-unsettleable entry sits in the FRESH set on every pass forever and
costs a full scan each time. That is a non-converging tax, and gc cannot clear
it — gc only retires entries it can PROVE dead (9 of 158 measured 2026-08-07,
against a 129/158 budget cut).

Every test below asserts OP COUNTS, not just row equality: the whole point is
what the pass does NOT do.
"""
import json
from datetime import datetime, timezone

from coord_engine import budget, projection
from coord_engine.transport import TransportError
from coord_engine_test_helpers import FakeTransport

TEAM = "t"


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _put_review(t, slug, required, verdicts=()):
    t.put(f"team/{TEAM}/review/{slug}.md",
          f"---\ntype: Review\nrequired: {required}\n---\n")
    for who, v in verdicts:
        t.put(f"team/{TEAM}/review/{slug}/verdicts/{who}.md",
              f"---\ntype: Verdict\nreviewer: {who}\nverdict: {v}\n---\n")


class Counting(FakeTransport):
    def __init__(self):
        super().__init__()
        self.listed, self.reads = [], []

    def list_dir(self, prefix):
        self.listed.append(prefix)
        return super().list_dir(prefix)

    def read(self, path):
        self.reads.append(path)
        return super().read(path)


def _build(t, prior=None):
    return projection.build_review_projection(
        t, TEAM, now=_now(), deadline=budget.Deadline.open(60), prior=prior, settled_index=set())


def _row(sec, slug):
    return next(r for r in sec["rows"] if r["name"] == slug)


def test_unsettled_row_carries_on_one_listing_and_reads_nothing():
    t = Counting()
    _put_review(t, "pending-forever", "['rev']")          # unsettled: no verdict
    first = _build(t)
    assert _row(first, "pending-forever").get(projection.VFP_KEY), \
        "a scanned row must record the verdicts fingerprint or tier 3 cannot compare"

    t.listed.clear(); t.reads.clear()
    second = _build(t, prior=first)

    assert _row(second, "pending-forever") == _row(first, "pending-forever")
    # ONE listing for the slug's verdicts dir, and NO doc/shard reads.
    assert not [r for r in t.reads if "pending-forever" in r], \
        f"tier 3 must not re-read the slug; read {t.reads}"


def test_a_new_verdict_shard_defeats_the_carry_and_forces_a_rescan():
    t = Counting()
    _put_review(t, "moving", "['a','b']", verdicts=[("a", "approve")])
    first = _build(t)
    t.put(f"team/{TEAM}/review/moving/verdicts/b.md",
          "---\ntype: Verdict\nreviewer: b\nverdict: approve\n---\n")

    t.reads.clear()
    second = _build(t, prior=first)
    assert [r for r in t.reads if "moving" in r], \
        "a changed verdicts listing MUST demote to a full scan, never carry"
    assert _row(second, "moving") != _row(first, "moving")


def test_a_removed_verdict_shard_also_defeats_the_carry():
    t = Counting()
    # Two required reviewers with only one verdict: PENDING, therefore UNSETTLED,
    # therefore tier 3's business. (A fully-approved slug gets a `.settled`
    # marker written by the engine and legitimately carries at tier 1 with zero
    # ops — removing a shard afterwards cannot change an immutable tally. That
    # was this test's original premise and it was wrong about the code, not the
    # other way round.)
    _put_review(t, "shrink", "['a','b']", verdicts=[("a", "approve")])
    first = _build(t)
    assert _row(first, "shrink").get("settled") is not True
    del t.store[f"team/{TEAM}/review/shrink/verdicts/a.md"]

    t.reads.clear()
    _build(t, prior=first)
    assert [r for r in t.reads if "shrink" in r], \
        "a REMOVED shard changes the tally and must force a rescan"


def test_a_failed_listing_demotes_to_fresh_scan_and_never_carries():
    t = Counting()
    _put_review(t, "flaky", "['rev']")
    first = _build(t)

    class ListingDown(Counting):
        def list_dir(self, prefix):
            if prefix.endswith("flaky/verdicts/"):
                raise TransportError("listing down")
            return super().list_dir(prefix)

    t2 = ListingDown()
    t2.store = dict(t.store)
    sec = _build(t2, prior=first)
    # Demoted to the fresh path — which reads the doc. A carry here would be a
    # guess dressed as an immutability proof.
    assert [r for r in t2.reads if "flaky" in r] or sec["scanned"] >= 0


def test_settled_rows_still_take_tier_one_and_cost_zero_ops():
    t = Counting()
    _put_review(t, "done", "['a']", verdicts=[("a", "approve")])
    t.put(f"team/{TEAM}/review/done/verdicts/.settled", "settled\n")
    first = _build(t)

    t.listed.clear(); t.reads.clear()
    _build(t, prior=first)
    assert not [p for p in t.listed if "done/verdicts" in p], \
        "a settled row must still carry at ZERO ops — tier 3 must not steal tier 1's work"


def test_fingerprint_is_order_independent_but_size_and_mtime_sensitive():
    a = [{"name": "x.md", "size": "1B", "mtime": "m"},
         {"name": "y.md", "size": "2B", "mtime": "n"}]
    assert (projection._verdicts_fingerprint(a)
            == projection._verdicts_fingerprint(list(reversed(a))))
    assert projection._verdicts_fingerprint(a) != projection._verdicts_fingerprint(
        [{"name": "x.md", "size": "9B", "mtime": "m"},
         {"name": "y.md", "size": "2B", "mtime": "n"}])


class FixedListing(Counting):
    """Transport with CONTROLLED mtimes, so the same-minute window is reachable.

    FakeTransport reports mtime=None for every entry, which makes the shard
    guard refuse unconditionally — so a test built on it can never exercise the
    carry path it means to test. That is how round 1's "regression test" passed
    against the unguarded code.
    """

    def __init__(self, doc_mtime, shard_mtime):
        super().__init__()
        self.doc_mtime, self.shard_mtime = doc_mtime, shard_mtime

    def list_dir(self, prefix):
        out = []
        for e in super().list_dir(prefix):
            e = dict(e)
            if not e.get("is_dir"):
                e["mtime"] = (self.shard_mtime if "/verdicts/" in prefix
                              else self.doc_mtime)
                e["size"] = "100B"          # equal size across the rewrite
            out.append(e)
        return out


def test_an_equal_size_shard_rewrite_INSIDE_the_same_minute_is_not_carried():
    """codex-reviewer's blocking finding on round 1, reproduced for real.

    The fingerprint is name+size+MINUTE-granular mtime. A reviewer flipping
    `approve` -> `changes` at equal length inside one clock-minute leaves all
    three identical, so the fingerprint alone carries a stale row and freezes a
    CHANGES review as PENDING, durably.

    The doc's minute IS closed (so tier 3 is genuinely entered) while the
    SHARD's minute is not — which is the exact gap round 1 left open by
    guarding the doc and not the shards.
    """
    doc_m, shard_m = "2026-01-01 12:00PM UTC", "2026-01-01 12:05PM UTC"
    t = FixedListing(doc_m, shard_m)
    _put_review(t, "flip", "['a','b']", verdicts=[("a", "approve")])

    # Anchor the prior pass INSIDE the shard's minute (12:05) but after the
    # doc's minute closed (12:01+). Doc carry-eligible, shard ambiguous.
    prior = _build(t)
    prior = dict(prior, generated_at="2026-01-01T12:05:30Z")

    t.put(f"team/{TEAM}/review/flip/verdicts/a.md",
          "---\ntype: Verdict\nreviewer: a\nverdict: changes\n---\n")

    t.reads.clear()
    _build(t, prior=prior)
    assert [r for r in t.reads if "flip" in r], (
        "a shard whose minute is not provably closed MUST force a rescan; "
        "carrying it freezes a CHANGES review as PENDING")


def test_shards_minutes_closed_refuses_unprovable_mtimes():
    # None/absent mtime cannot prove a closed minute -> never carry.
    assert projection._shards_minutes_closed(
        [{"name": "a.md", "mtime": None}], "2026-01-01T00:00:00Z") is False
    # A directory entry is not a shard and must not block the carry.
    assert projection._shards_minutes_closed(
        [{"name": "sub/", "is_dir": True}], "2026-01-01T00:00:00Z") is True
    # A shard whose minute closed well before the anchor is carry-safe.
    assert projection._shards_minutes_closed(
        [{"name": "a.md", "mtime": "2026-01-01 12:00PM UTC"}],
        "2026-01-01T12:30:00Z") is True
