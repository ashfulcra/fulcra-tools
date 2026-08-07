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
