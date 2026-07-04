from coord_engine import review


def _v(reviewer, verdict):
    return {"reviewer": reviewer, "verdict": verdict}


def test_normalize_verdict():
    assert review.normalize_verdict("approve") == "approve"
    assert review.normalize_verdict("LGTM") == "approve"
    assert review.normalize_verdict("request-changes") == "changes"
    assert review.normalize_verdict("meh") is None


def test_pending_when_no_verdicts():
    assert review.tally([])["state"] == review.PENDING


def test_approved_on_single_approve():
    assert review.tally([_v("a", "approve")])["state"] == review.APPROVED


def test_changes_dominates():
    t = review.tally([_v("a", "approve"), _v("b", "changes")])
    assert t["state"] == review.CHANGES
    assert t["changes"] == ["b"]


def test_last_verdict_per_reviewer_wins():
    # reviewer flips changes -> approve
    t = review.tally([_v("a", "changes"), _v("a", "approve")])
    assert t["state"] == review.APPROVED


def test_required_reviewers_gate_approval():
    t = review.tally([_v("a", "approve")], required=["a", "b"])
    assert t["state"] == review.PENDING
    assert t["pending_required"] == ["b"]
    t2 = review.tally([_v("a", "approve"), _v("b", "approve")], required=["a", "b"])
    assert t2["state"] == review.APPROVED


def test_garbage_verdicts_ignored():
    t = review.tally([{"reviewer": "a"}, {"verdict": "approve"}, "nope", _v("b", "approve")])
    assert t["state"] == review.APPROVED
    assert t["approvals"] == ["b"]
