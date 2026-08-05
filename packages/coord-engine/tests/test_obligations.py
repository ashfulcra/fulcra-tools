"""Slice 3: the obligation fold cannot say CLEAR unless it earned it.

Every test here is one way the fold could lie about certainty. The single
property under test throughout: CLEAR is a positive claim about complete
coverage, so anything short of complete coverage must be structurally unable to
produce it — not merely annotated.
"""

from __future__ import annotations

import pytest

from coord_engine.obligations import (
    OBLIGATION_COMPONENTS,
    Component,
    ObligationState,
    ProbeResult,
    ProbeState,
    fold,
)


def ok(*owed):
    return ProbeResult(state=ProbeState.OK, owed=list(owed))


def unreadable(detail="transport"):
    return ProbeResult(state=ProbeState.UNREADABLE, detail=detail)


def malformed(detail="bad json"):
    return ProbeResult(state=ProbeState.MALFORMED, detail=detail)


def comps(**by_name):
    """Components from ``name=ProbeResult`` pairs, in registry order."""
    return [Component(name=n, probe=(lambda r=r: r)) for n, r in by_name.items()]


def all_ok(**overrides):
    """Every registry component OK, with named overrides applied."""
    built = {name: ok() for name in OBLIGATION_COMPONENTS}
    built.update(overrides)
    return comps(**built)


# --- CLEAR is reachable, and only honestly ----------------------------------

def test_clear_requires_every_component_consulted():
    result = fold(all_ok(), expected=OBLIGATION_COMPONENTS)
    assert result.state is ObligationState.CLEAR
    assert result.can_claim_clear
    assert result.consulted == sorted(OBLIGATION_COMPONENTS)
    assert not result.degraded and not result.malformed


def test_owed_work_is_data():
    result = fold(all_ok(directives=ok({"slug": "respec-s3"})),
                  expected=OBLIGATION_COMPONENTS)
    assert result.state is ObligationState.DATA
    assert [o["slug"] for o in result.owed] == ["respec-s3"]


# --- the fail-closed core ---------------------------------------------------

def test_one_unreadable_component_makes_clear_unreachable():
    result = fold(all_ok(reviews=unreadable()), expected=OBLIGATION_COMPONENTS)
    assert result.state is ObligationState.UNKNOWN
    assert not result.can_claim_clear
    assert result.degraded == ["reviews"]


def test_unreadable_component_cannot_be_masked_by_other_components_being_fine():
    """Five clean components do not add up to an answer when the sixth is dark."""
    result = fold(all_ok(role_duties=unreadable()),
                  expected=OBLIGATION_COMPONENTS)
    assert result.state is ObligationState.UNKNOWN
    assert len(result.consulted) == len(OBLIGATION_COMPONENTS) - 1


def test_malformed_is_invalid_not_unknown_and_not_clear():
    result = fold(all_ok(tasks=malformed()), expected=OBLIGATION_COMPONENTS)
    assert result.state is ObligationState.INVALID
    assert result.malformed == ["tasks"]
    assert not result.can_claim_clear


def test_unreadable_outranks_malformed_but_both_stay_visible():
    """The weaker claim wins; the fixable problem is still reported."""
    result = fold(all_ok(tasks=malformed(), reviews=unreadable()),
                  expected=OBLIGATION_COMPONENTS)
    assert result.state is ObligationState.UNKNOWN
    assert result.degraded == ["reviews"]
    assert result.malformed == ["tasks"], (
        "a malformed component must not vanish from the report just because "
        "something else was also unreadable — it is the one a human can fix"
    )


def test_a_probe_that_raises_is_a_degraded_component_not_a_crash():
    """The fold is a never-crash surface; an exploding probe is doubt, not death."""
    def boom() -> ProbeResult:
        raise RuntimeError("transport exploded")

    components = all_ok()
    components.append(Component(name="explodes", probe=boom))
    result = fold(components, expected=OBLIGATION_COMPONENTS + ("explodes",))
    assert result.state is ObligationState.UNKNOWN
    assert "explodes" in result.degraded


def test_a_silently_missing_component_is_unknown():
    """The case a marker row cannot catch.

    Every component that ran said OK — but one was never offered to the fold at
    all. "Nothing reported it" and "nothing was asked" are indistinguishable in
    the output unless the fold checks its own coverage, so it does.
    """
    partial = {name: ok() for name in OBLIGATION_COMPONENTS if name != "reminders"}
    result = fold(comps(**partial), expected=OBLIGATION_COMPONENTS)
    assert result.state is ObligationState.UNKNOWN
    assert result.degraded == ["reminders"]


def test_partial_work_is_still_returned_while_degraded():
    """Doubt about completeness does not justify discarding what was found."""
    result = fold(all_ok(directives=ok({"slug": "a"}), reviews=unreadable()),
                  expected=OBLIGATION_COMPONENTS)
    assert result.state is ObligationState.UNKNOWN
    assert [o["slug"] for o in result.owed] == ["a"]


# --- diagnostics ------------------------------------------------------------

def test_degraded_lists_are_deterministic():
    """Diagnostics that reorder between runs cannot be diffed."""
    a = fold(all_ok(reviews=unreadable(), tasks=unreadable()),
             expected=OBLIGATION_COMPONENTS)
    b = fold(all_ok(tasks=unreadable(), reviews=unreadable()),
             expected=OBLIGATION_COMPONENTS)
    assert a.degraded == b.degraded == ["reviews", "tasks"]


@pytest.mark.parametrize("state,needle", [
    (ObligationState.UNKNOWN, "cannot prove"),
    (ObligationState.INVALID, "malformed"),
])
def test_reason_names_the_problem(state, needle):
    probe = unreadable() if state is ObligationState.UNKNOWN else malformed()
    result = fold(all_ok(reviews=probe), expected=OBLIGATION_COMPONENTS)
    assert result.state is state
    assert needle in result.reason()
    assert "reviews" in result.reason()


def test_registry_is_the_single_source_of_the_component_set():
    """The set is correctness-critical, so it stays one reviewable constant."""
    assert OBLIGATION_COMPONENTS == tuple(sorted(OBLIGATION_COMPONENTS)), (
        "keep the registry sorted so degraded lists and diffs stay stable"
    )
    assert len(set(OBLIGATION_COMPONENTS)) == len(OBLIGATION_COMPONENTS)
