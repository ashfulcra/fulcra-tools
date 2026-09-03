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


def test_bundled_policy_gathers_asks_into_one_project() -> None:
    policy = load_policy()
    assert policy.lane_projects["asks"] == "Blocked on Ash"
    assert policy.project_for("asks", None) == "Blocked on Ash"


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
    assert document["lane_projects"] == {"asks": "Blocked on Ash"}
    assert document["lane_priority"] == {"asks": 1}
