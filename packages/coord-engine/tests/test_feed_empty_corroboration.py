"""A zero signal is not proof of CLEAR -- corroborate an empty feed answer.

Measured at the RAW HTTP boundary (`GET /data/v1/updates`) and through the CLI,
2026-10-02: `-1d` returned 13135 file changes in 4.0 MB and `-7d` returned
106187 in 32.7 MB, while `-8d` and beyond returned **HTTP 500 with an empty
body** -- which our transport correctly maps to `None` = UNKNOWN. The defect
this module is about is different and worse: a window WELL INSIDE the working
range **intermittently** returns rc 0 with `file_changes: []` and a populated
`data_types` map. 1 of 3 reps at `-7d`, 1 of 3 at `-25d`, 1 of 30 at `-1d`
(one batch of 20 was entirely clean, so the rate is low and variable).

One explanation covers all of it: the endpoint degrades under window size,
usually to 500 and sometimes to a 200 whose `file_changes` leg is empty, and
the wider the window the more often it degrades. There is no declared horizon;
~7 days is just where degradation becomes the common case.

So an empty `file_changes` is never proof of quiet **at any width**, and the
bad answer is well-formed, fast, and passes every attestation the detector
already performs -- shape alone cannot reject it.

The check pinned here needs no horizon constant, because it tests the
contradiction rather than predicting a limit. Its limit is explicit: it cannot
catch a false empty on a window narrower than the corroboration bound.

It is DORMANT BY DEFAULT and these tests pin both branches, because a gate that
is only ever tested switched on is not a gate.
"""

import pytest

from coord_engine import change_detection as cd
from coord_engine.budget import Deadline


COORDINATION_TYPE = "MomentAnnotation/00000000-0000-4000-8000-000000000102"
FRONTIER = "2026-10-02T18:00:00Z"
LONG_AFTER = "2026-09-07T15:00:41Z"        # wider than the corroboration bound
RECENT_AFTER = "2026-10-02T12:00:00Z"      # inside the 24h corroboration window
CHANGE = {"path": "team/r/task/a.md", "state": "uploaded",
          "uploaded_at": "2026-10-02T17:00:00Z", "update_id": "u1"}


class Feed:
    """Real-boundary double: answers `data_updates` per `after`, records each ask.

    Shaped after `test_v2_change_detection.FeedTransport` so a poll that gets
    past the window check also gets past the record-cursor leg -- otherwise an
    unrelated bare UNKNOWN masks whatever this module is trying to measure.
    """

    def __init__(self, answer):
        self._answer = answer
        self.asked: list = []

    def _envelope(self, after, rows):
        return {"after": after, "through": FRONTIER, "file_changes": list(rows),
                "data_types": {COORDINATION_TYPE: 0}}

    def data_updates(self, after, *, deadline=None):
        self.asked.append(after)
        answer = self._answer(after)
        if isinstance(answer, Exception):
            raise answer
        if answer is None or not isinstance(answer, (list, tuple)):
            return answer          # a deliberately broken corroboration answer
        return self._envelope(after, answer)

    def records_cursor(self, _channel, since, *, deadline=None):
        return {"after": since, "through": FRONTIER, "records": []}

    def read_classified(self, _path, *, deadline=None):
        import json
        return json.dumps({"data_type": COORDINATION_TYPE}), "ok"


EMPTY, HAS_DATA = [], [CHANGE]


def _poll(feed, after=LONG_AFTER):
    return cd.ChangeDetector(feed).poll("r", after, Deadline.open(30.0))


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv(cd.EMPTY_CORROBORATION_ENV, "1")


# --- dormant by default ----------------------------------------------------

def test_dormant_by_default_the_bogus_empty_is_still_believed(monkeypatch):
    """Today's behaviour, pinned. Switching the gate on is a separate decision."""
    monkeypatch.delenv(cd.EMPTY_CORROBORATION_ENV, raising=False)
    feed = Feed(lambda after: EMPTY if after == LONG_AFTER else HAS_DATA)
    batch = _poll(feed)
    assert batch.trusted is True, "unchanged default: an empty long window reads as CLEAR"
    assert feed.asked == [LONG_AFTER], "and costs exactly one read"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe", "2"])
def test_only_an_explicit_affirmative_enables_it(monkeypatch, value):
    monkeypatch.setenv(cd.EMPTY_CORROBORATION_ENV, value)
    assert cd._corroboration_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
def test_affirmative_spellings_enable_it(monkeypatch, value):
    monkeypatch.setenv(cd.EMPTY_CORROBORATION_ENV, value)
    assert cd._corroboration_enabled() is True


# --- enabled: the live defect ---------------------------------------------

def test_a_long_empty_is_disproven_by_a_nonempty_recent_sub_window(enabled):
    """The live shape: the wide window says nothing changed, a recent one disagrees."""
    feed = Feed(lambda after: EMPTY if after == LONG_AFTER else HAS_DATA)
    batch = _poll(feed)
    assert batch.trusted is False
    assert batch.reason == cd.FEED_EMPTY_DISPROVEN
    assert len(feed.asked) == 2, "one extra read, and only on the empty path"


def test_a_long_empty_corroborated_by_an_empty_sub_window_stands(enabled):
    """A genuinely quiet team must not be frozen by this check."""
    feed = Feed(lambda after: EMPTY)
    batch = _poll(feed)
    assert batch.trusted is True
    assert len(feed.asked) == 2


def test_a_nonempty_long_window_is_never_corroborated(enabled):
    """The check rides the empty path only, so the common case costs nothing."""
    feed = Feed(lambda after: HAS_DATA)
    batch = _poll(feed)
    assert batch.trusted is True
    assert feed.asked == [LONG_AFTER], "no second read when there is data to act on"


def test_a_recent_window_that_is_empty_is_not_corroborated(enabled):
    """Nothing to compare: the window asked about is already inside the bound."""
    feed = Feed(lambda after: EMPTY)
    batch = _poll(feed, after=RECENT_AFTER)
    assert batch.trusted is True
    assert feed.asked == [RECENT_AFTER]


# --- enabled: corroboration itself failing --------------------------------

@pytest.mark.parametrize("broken", [
    None,                                                          # returned None
    RuntimeError("boom"),                                          # raised
    {"after": "x", "through": FRONTIER},                           # no file_changes
    {"after": "x", "through": FRONTIER, "file_changes": "nope"},   # wrong shape
], ids=["none", "raises", "no-key", "wrong-shape"])
def test_an_unreadable_corroboration_is_uncorroborated_not_clear(enabled, broken):
    """Fail-closed: not disproven, but not proven either.

    This is the branch that makes the gate risky and therefore dormant -- a
    transient failure on the EXTRA read turns a genuinely clean pass into
    UNKNOWN. Pinned deliberately so the cost is visible, not discovered.
    """
    feed = Feed(lambda after: EMPTY if after == LONG_AFTER else broken)
    batch = _poll(feed)
    assert batch.trusted is False
    assert batch.reason == cd.FEED_EMPTY_UNCORROBORATED


def test_the_sub_window_is_cut_from_the_feeds_frontier_not_a_local_clock(enabled):
    """Cross-host clock skew must not be able to move the comparison boundary."""
    feed = Feed(lambda after: EMPTY)
    _poll(feed)
    assert len(feed.asked) == 2
    assert feed.asked[1] == "2026-10-01T18:00:00Z", "frontier 18:00Z minus the 24h default"


def test_the_window_width_is_configurable(enabled, monkeypatch):
    monkeypatch.setenv(cd.EMPTY_CORROBORATION_HOURS_ENV, "6")
    feed = Feed(lambda after: EMPTY)
    _poll(feed)
    assert feed.asked[1] == "2026-10-02T12:00:00Z"


# --- the recovery set must not quietly grow -------------------------------

def test_the_new_reasons_do_not_license_the_full_scan_recovery():
    """Whether a feed caught lying should trigger a full scan is Fix 1's
    decision, not this change's. Pinned so it cannot drift in silently."""
    for reason in (cd.FEED_EMPTY_DISPROVEN, cd.FEED_EMPTY_UNCORROBORATED):
        assert reason not in cd.RECOVERABLE_FEED_WINDOW_REASONS
        batch = cd._unknown(reason)
        assert cd.detector_recovery_reason(batch) is None
