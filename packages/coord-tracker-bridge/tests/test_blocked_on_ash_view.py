"""The one view this lane exists to produce: what is blocked on the operator.

Ash, 2026-09-03: "The most essential ask of this project was a 'blocked on me'
view. I want to see all the agents blocked on me in one place."

Asks rows carry `assignee: human`, an `owner` naming the blocked agent, and NO
workstream — so `workstream_projects`, the only project hook the policy had,
could never gather them. Gathering is keyed on the lane instead.
"""

from __future__ import annotations

import json

import pytest

from coord_tracker_bridge.policy import _policy_from_mapping, load_policy

BASE = {
    "version": "2",
    "included_lanes": ["active", "asks"],
    "lane_states": {"active": "started", "asks": "unstarted"},
    "priority": {"P1": 2, "P2": 3},
    "managed_labels": [],
    "field_ownership": {},
}


def test_bundled_policy_gathers_asks_into_a_per_consumer_project() -> None:
    """The template is what makes one policy serve many humans. It must still
    render the SAME name for the existing consumer — a handle-named project
    ("Blocked on ash") would be a second project and move live cards into it."""

    policy = load_policy()
    assert policy.lane_projects["asks"] == "Blocked on {consumer}"
    assert policy.project_for("asks", None, "ash") == "Blocked on Ash"


def test_bundled_policy_makes_asks_the_only_urgent_lane() -> None:
    """41 Urgent cards at once is the same as none."""

    policy = load_policy()
    assert policy.lane_priority["asks"] == 1
    # No P-level maps to Urgent any more, so nothing outside asks can claim it.
    assert 1 not in set(policy.priority.values())


def test_lane_project_wins_over_workstream() -> None:
    policy = _policy_from_mapping(
        {**BASE, "lane_projects": {"asks": "Blocked on Ash"},
         "workstream_projects": {"w": "Other"}}
    )
    assert policy.project_for("asks", "w") == "Blocked on Ash"
    assert policy.project_for("active", "w") == "Other"


def test_projects_property_covers_both_sources() -> None:
    """A project the projection names but the resource plan omits makes sync
    raise ResourceMissing partway through, after earlier cards were written."""

    policy = _policy_from_mapping(
        {**BASE, "lane_projects": {"asks": "Blocked on Ash"},
         "workstream_projects": {"w": "Other"}}
    )
    assert policy.projects == ("Blocked on Ash", "Other")


def test_a_lane_mapping_outside_the_allowlist_is_refused() -> None:
    """Dead config that reads as configured is the failure this prevents."""

    with pytest.raises(ValueError, match="outside included_lanes"):
        _policy_from_mapping({**BASE, "lane_projects": {"threads-missed": "Nope"}})
    with pytest.raises(ValueError, match="outside included_lanes"):
        _policy_from_mapping({**BASE, "lane_priority": {"nonsense": 1}})


def test_an_empty_project_name_is_refused() -> None:
    with pytest.raises(ValueError, match="non-empty project"):
        _policy_from_mapping({**BASE, "lane_projects": {"asks": "  "}})


def test_policy_document_round_trips_the_new_keys() -> None:
    """The hash covers the document, and the ledger filename covers the hash."""

    policy = load_policy()
    document = json.loads(json.dumps(dict(policy.document)))
    assert document["lane_projects"] == {"asks": "Blocked on {consumer}"}
    assert document["consumers"]["ash"]["display"] == "Ash"
    assert document["consumers"]["ash"]["linear_user"]
    assert document["lane_priority"] == {"asks": 1}


# --------------------------------------------------------------------------
# multiple human consumers
# --------------------------------------------------------------------------

MULTI = {
    **BASE,
    "lane_projects": {"asks": "Blocked on {consumer}"},
    "consumers": {"ash": "Ash", "liz": "Liz"},
    "unassigned_consumer": "someone",
}


def test_each_consumer_gets_their_own_view() -> None:
    policy = _policy_from_mapping(MULTI)
    assert policy.project_for("asks", None, "ash") == "Blocked on Ash"
    assert policy.project_for("asks", None, "liz") == "Blocked on Liz"


def test_an_unresolved_consumer_goes_to_triage_not_a_persons_view() -> None:
    """A row whose consumer we could not resolve must never appear in somebody's
    'blocked on me' view, and must never render the literal placeholder."""

    policy = _policy_from_mapping(MULTI)
    assert policy.project_for("asks", None, None) == "Blocked on someone"


def test_an_unconfigured_handle_still_gets_a_view() -> None:
    """The row DID name a person; we just have no display name. Falling back to
    the handle is honest, dropping them into triage would lose the attribution."""

    policy = _policy_from_mapping(MULTI)
    assert policy.project_for("asks", None, "brad") == "Blocked on brad"


def test_resource_plan_covers_every_consumer_and_triage() -> None:
    """sync refuses a non-empty resource plan, so a project first named mid-run
    would halt the run after earlier cards were already written."""

    policy = _policy_from_mapping(MULTI)
    assert policy.projects == ("Blocked on Ash", "Blocked on Liz", "Blocked on someone")


def test_triage_name_may_not_collide_with_a_real_consumer() -> None:
    with pytest.raises(ValueError, match="must not name a configured consumer"):
        _policy_from_mapping({**MULTI, "unassigned_consumer": "Liz"})


def test_a_consumer_template_without_consumers_is_refused() -> None:
    """Otherwise the resource plan cannot create the per-person projects."""

    with pytest.raises(ValueError, match="requires a non-empty consumers list"):
        _policy_from_mapping({**BASE, "lane_projects": {"asks": "Blocked on {consumer}"}})


def test_a_consumer_needs_both_handle_and_display_name() -> None:
    with pytest.raises(ValueError, match="non-empty handle and display name"):
        _policy_from_mapping({**MULTI, "consumers": {"ash": "  "}})


# --------------------------------------------------------------------------
# reaching the person, not just the project
# --------------------------------------------------------------------------

def test_a_consumer_with_a_linear_user_gets_the_card_assigned() -> None:
    """Measured on the live board 2026-09-03: 226 of 229 cards unassigned, and
    the newest thing in the operator's own Linear queue was six weeks stale. A
    project the person never opens is not a view."""

    policy = _policy_from_mapping({
        **BASE,
        "lane_projects": {"asks": "Blocked on {consumer}"},
        "consumers": {"ash": {"display": "Ash", "linear_user": "u-ash"}},
    })
    assert policy.linear_user_for("ash") == "u-ash"


def test_display_only_consumers_still_work_but_assign_nobody() -> None:
    """The short form is legal — you just get a view without notifications."""

    policy = _policy_from_mapping({
        **BASE,
        "lane_projects": {"asks": "Blocked on {consumer}"},
        "consumers": {"liz": "Liz"},
    })
    assert policy.project_for("asks", None, "liz") == "Blocked on Liz"
    assert policy.linear_user_for("liz") is None


def test_an_unresolved_consumer_assigns_nobody() -> None:
    """Written through as unassign: a row whose consumer became unresolvable
    must not sit in the previous person's queue claiming to be theirs."""

    policy = _policy_from_mapping({
        **BASE,
        "lane_projects": {"asks": "Blocked on {consumer}"},
        "consumers": {"ash": {"display": "Ash", "linear_user": "u-ash"}},
    })
    assert policy.linear_user_for(None) is None
    assert policy.linear_user_for("nobody-we-know") is None


def test_the_bundled_policy_assigns_its_consumer() -> None:
    assert load_policy().linear_user_for("ash")
