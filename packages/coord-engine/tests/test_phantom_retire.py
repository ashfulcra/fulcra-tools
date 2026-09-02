"""Retiring an obligation whose backing document does not exist.

coord-boss re-ruling `re-ruling-on-the-phantoms-...-b30ffc7d`: the specification
stands, the mechanism was wrong. `tell --closes` refuses a phantom because the
close path resolves THROUGH the document, which is the same fact that made it a
phantom.

THE DESIGN CONSTRAINT, stated by coord-boss and load-bearing here: `tell --closes`
reports "absent OR UNREADABLE" — the identical conflation option (2) was rejected
for. This path must NOT inherit it. It refuses rather than guesses whenever the
evidence does not positively distinguish absence from an unreadable store, and a
same-pass positive control is what does the distinguishing.

Retiring is NOT discharging: it records that the document is absent, never that
the work was done.
"""
import pytest

from coord_engine import phantom


FOUND = phantom.Probe(found=True, detail="(2500 bytes)")
NOT_FOUND = phantom.Probe(found=False, detail="File not found in Fulcra")
UNREADABLE = phantom.Probe(found=None, detail="connection reset")


def test_absent_target_with_a_clean_control_RETIRES():
    d = phantom.retirement_decision(probe=NOT_FOUND, control=FOUND)
    assert d.retire is True
    assert "absent" in d.why.lower()


def test_a_control_THAT_DID_NOT_COME_BACK_CLEAN_refuses():
    """The whole point. A not-found under a degraded store is indistinguishable
    from a genuine absence, and this write is not cheaply reversible."""
    d = phantom.retirement_decision(probe=NOT_FOUND, control=NOT_FOUND)
    assert d.retire is False
    assert "control" in d.why.lower()


def test_an_UNREADABLE_control_refuses_too():
    d = phantom.retirement_decision(probe=NOT_FOUND, control=UNREADABLE)
    assert d.retire is False


def test_an_UNREADABLE_probe_refuses_even_with_a_clean_control():
    """UNKNOWN is not absence. Only an explicit not-found counts."""
    d = phantom.retirement_decision(probe=UNREADABLE, control=FOUND)
    assert d.retire is False
    assert "unknown" in d.why.lower() or "unreadable" in d.why.lower()


def test_a_target_THAT_EXISTS_is_not_a_phantom():
    d = phantom.retirement_decision(probe=FOUND, control=FOUND)
    assert d.retire is False
    assert "exists" in d.why.lower()


def test_the_decision_carries_BOTH_results_for_the_record():
    """coord-boss required the close record to carry the not-found AND the
    same-pass control. If the decision does not carry them, the caller has to
    reconstruct them and will eventually reconstruct them wrongly."""
    d = phantom.retirement_decision(probe=NOT_FOUND, control=FOUND)
    assert "File not found in Fulcra" in d.evidence
    assert "(2500 bytes)" in d.evidence


def test_evidence_is_present_even_when_REFUSING():
    """A refusal has to be auditable too, or nobody can tell why it refused."""
    d = phantom.retirement_decision(probe=NOT_FOUND, control=UNREADABLE)
    assert "connection reset" in d.evidence


def test_a_missing_control_is_not_treated_as_a_clean_one():
    with pytest.raises(ValueError):
        phantom.retirement_decision(probe=NOT_FOUND, control=None)


# --- listing-derived probes: the RAISING listing is the evidence ------------
#
# coord-boss: "it needs the raising listing as its evidence, never a falsy read,
# and it must refuse rather than guess when the control does not come back
# clean." `transport.read` returns None for BOTH missing and failed — which is
# why `tell --closes` can only say "absent or unreadable". `list_dir` RAISES on
# transport failure, so a listing that RETURNS is itself the positive control:
# it proves the store answered in the same pass that established the absence.


def test_a_returned_listing_without_the_slug_is_an_explicit_absence():
    p = phantom.probe_from_listing(["other-a.md", "other-b.md"], "gone")
    assert p.found is False


def test_a_returned_listing_containing_the_slug_says_present():
    p = phantom.probe_from_listing(["gone.md", "other.md"], "gone")
    assert p.found is True


def test_an_EMPTY_listing_is_UNKNOWN_not_absence():
    """An empty task directory on a board with hundreds of tasks is not
    evidence that one slug is gone — it is evidence something is wrong."""
    assert phantom.probe_from_listing([], "gone").found is None


def test_a_listing_of_None_is_UNKNOWN():
    assert phantom.probe_from_listing(None, "gone").found is None


def test_the_listing_is_its_own_control_when_it_returns_entries():
    assert phantom.control_from_listing(["a.md"]).found is True


def test_an_empty_or_missing_listing_is_NOT_a_clean_control():
    assert phantom.control_from_listing([]).found is None
    assert phantom.control_from_listing(None).found is None


def test_end_to_end_listing_absence_retires():
    names = ["kept-a.md", "kept-b.md", "kept-c.md"]
    d = phantom.retirement_decision(probe=phantom.probe_from_listing(names, "gone"),
                                    control=phantom.control_from_listing(names))
    assert d.retire is True


def test_end_to_end_empty_listing_REFUSES():
    d = phantom.retirement_decision(probe=phantom.probe_from_listing([], "gone"),
                                    control=phantom.control_from_listing([]))
    assert d.retire is False
