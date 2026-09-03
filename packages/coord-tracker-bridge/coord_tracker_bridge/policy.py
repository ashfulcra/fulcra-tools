"""Versioned projection policy loading and validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


OWNERSHIP_VALUES = frozenset({"source", "tracker", "merge"})


@dataclass(frozen=True, slots=True)
class Policy:
    version: str
    included_lanes: frozenset[str]
    lane_states: Mapping[str, str]
    priority: Mapping[str, int]
    managed_labels: tuple[str, ...]
    workstream_projects: Mapping[str, str]
    #: lane -> Linear project name. A workstream is the wrong key for the one
    #: view this lane exists to produce: "what is blocked on Ash" is a property
    #: of the LANE (asks), and asks rows carry no workstream at all, so a
    #: workstream mapping can never gather them.
    lane_projects: Mapping[str, str]
    #: lane -> Linear priority, overriding the P-level map. Fleet-P1 means "top
    #: of the fleet's queue", which is a far lower bar than what Urgent implies
    #: on a human board — 41 Urgent cards at once is the same as none.
    lane_priority: Mapping[str, int]
    #: The human consumers this deployment serves: coord handle -> display name.
    #: They are two different things — `ash` is an identity the bus resolves,
    #: "Ash" is what a person reads on a board — and collapsing them would name
    #: projects after handles. Every consumer gets their own rendering of a
    #: `{consumer}` lane project, and the resource plan creates all of them up
    #: front: `sync` refuses a non-empty resource plan, so a consumer first seen
    #: mid-run would otherwise halt the run partway through.
    consumers: Mapping[str, str]
    #: Where a row lands when its consumer cannot be resolved. Named, not blank:
    #: unattributed work must be visibly somebody's to triage.
    unassigned_consumer: str
    #: coord handle -> Linear user id. Without it a card lands in the right
    #: PROJECT but is assigned to nobody, so it never appears in that
    #: person's My Issues, inbox, notifications or phone. Measured on the
    #: live board 2026-09-03: 226 of 229 cards unassigned, and the newest
    #: thing in the operator's own queue was six weeks stale.
    consumer_linear_users: Mapping[str, str]
    included_origins: frozenset[str]
    close_absent: bool
    field_ownership: Mapping[str, str]
    document: Mapping[str, Any]
    hash: str

    def owns(self, field: str) -> str:
        return self.field_ownership.get(field, "source")

    @property
    def projects(self) -> tuple[str, ...]:
        """Every Linear project this policy can assign, for the resource plan.

        A project the projection names but the resource plan omits makes `sync`
        raise ResourceMissing on the first card that wants it, after earlier
        cards have already been written.
        """

        names: set[str] = set(self.workstream_projects.values())
        for template in self.lane_projects.values():
            if "{consumer}" not in template:
                names.add(template)
                continue
            # Every configured consumer, plus the triage destination for rows
            # whose consumer never resolved.
            for who in (*self.consumers.values(), self.unassigned_consumer):
                names.add(template.format(consumer=who))
        return tuple(sorted(names))

    def project_for(
        self, lane: str, workstream: str | None, consumer: str | None = None
    ) -> str | None:
        """Which Linear project this row belongs in.

        Lane wins over workstream: it is the specific claim. A lane project may
        carry `{consumer}`, which is how one policy serves many humans — each
        gets their own "blocked on me" view from the same configuration.

        A row whose consumer is unresolved renders the template with
        `unassigned_consumer` rather than dropping the placeholder: it must land
        somewhere a person will triage it, never in a named person's view and
        never in a project literally called "Blocked on {consumer}".
        """

        template = self.lane_projects.get(lane)
        if template is None:
            return self.workstream_projects.get(workstream or "") or None
        if "{consumer}" not in template:
            return template
        if not consumer:
            return template.format(consumer=self.unassigned_consumer)
        # An unconfigured handle still gets a view rather than vanishing into
        # triage: the row DID name a person, we just have no display name for
        # them. Falling back to the handle is honest; dropping them is not.
        return template.format(consumer=self.consumers.get(consumer, consumer))

    def linear_user_for(self, consumer: str | None) -> str | None:
        """The Linear account to assign this row to, or None to leave it alone.

        None is returned for an unresolved or unmapped consumer, and the caller
        writes that through as "unassign": a row whose consumer changed or became
        unresolvable must not sit in the previous person's queue claiming to be
        theirs.
        """

        if not consumer:
            return None
        return self.consumer_linear_users.get(consumer)



def _policy_from_mapping(raw: Mapping[str, Any]) -> Policy:
    version = str(raw.get("version", "")).strip()
    if not version:
        raise ValueError("policy version is required")
    labels = tuple(str(item) for item in raw.get("managed_labels", []))
    if len(labels) != len(set(labels)):
        raise ValueError("managed_labels must be unique")
    max_labels = int(raw.get("max_managed_labels", 32))
    if max_labels <= 0 or len(labels) > max_labels:
        raise ValueError("managed label taxonomy exceeds max_managed_labels")
    ownership = {str(k): str(v) for k, v in raw.get("field_ownership", {}).items()}
    invalid = set(ownership.values()) - OWNERSHIP_VALUES
    if invalid:
        raise ValueError(f"invalid field ownership values: {sorted(invalid)}")
    included_lanes_raw = raw.get("included_lanes", [])
    if not isinstance(included_lanes_raw, list):
        raise ValueError("included_lanes must be a list")
    included_lane_values = [str(value).strip() for value in included_lanes_raw]
    included_lanes = frozenset(included_lane_values)
    if "" in included_lanes:
        raise ValueError("included_lanes entries must be non-empty")
    if len(included_lane_values) != len(included_lanes):
        raise ValueError("included_lanes must be unique")
    lane_states = {str(k): str(v) for k, v in raw.get("lane_states", {}).items()}
    missing_lane_states = included_lanes - lane_states.keys()
    if missing_lane_states:
        raise ValueError(
            f"included_lanes missing lane_states: {sorted(missing_lane_states)}"
        )
    lane_projects = {str(k): str(v) for k, v in raw.get("lane_projects", {}).items()}
    lane_priority = {str(k): int(v) for k, v in raw.get("lane_priority", {}).items()}
    consumers: dict[str, str] = {}
    consumer_linear_users: dict[str, str] = {}
    for handle, value in (raw.get("consumers", {}) or {}).items():
        key = str(handle).strip()
        if isinstance(value, Mapping):
            display = str(value.get("display", "")).strip()
            linear_user = str(value.get("linear_user", "")).strip()
            if linear_user:
                consumer_linear_users[key] = linear_user
        else:
            # Display-only form: the person gets a view but no card is assigned
            # to them, so it never reaches their My Issues or notifications.
            display = str(value).strip()
        if not key or not display:
            raise ValueError("consumers entries need a non-empty handle and display name")
        consumers[key] = display
    unassigned_consumer = str(raw.get("unassigned_consumer", "someone")).strip()
    if not unassigned_consumer:
        raise ValueError("unassigned_consumer must be non-empty")
    if unassigned_consumer in consumers.values():
        # Otherwise unattributed rows silently pile into a real person's view.
        raise ValueError("unassigned_consumer must not name a configured consumer")
    if any("{consumer}" in template for template in lane_projects.values()) and not consumers:
        raise ValueError(
            "a {consumer} lane project requires a non-empty consumers list: with none "
            "configured the resource plan cannot create the per-person projects"
        )
    # A lane mapping outside the allowlist is dead config that reads as
    # configured. Fail loudly at load rather than silently never firing.
    for name, mapping in (("lane_projects", lane_projects), ("lane_priority", lane_priority)):
        stray = mapping.keys() - included_lanes
        if stray:
            raise ValueError(f"{name} names lanes outside included_lanes: {sorted(stray)}")
    if any(not value.strip() for value in lane_projects.values()):
        raise ValueError("lane_projects entries must name a non-empty project")
    document = json.loads(json.dumps(raw))
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return Policy(
        version=version,
        included_lanes=included_lanes,
        lane_states=MappingProxyType(lane_states),
        priority=MappingProxyType({str(k): int(v) for k, v in raw.get("priority", {}).items()}),
        managed_labels=labels,
        workstream_projects=MappingProxyType(
            {str(k): str(v) for k, v in raw.get("workstream_projects", {}).items()}
        ),
        lane_projects=MappingProxyType(lane_projects),
        lane_priority=MappingProxyType(lane_priority),
        consumers=MappingProxyType(consumers),
        unassigned_consumer=unassigned_consumer,
        consumer_linear_users=MappingProxyType(consumer_linear_users),
        included_origins=frozenset(str(v) for v in raw.get("included_origins", [])),
        close_absent=bool(raw.get("close_absent", True)),
        field_ownership=MappingProxyType(ownership),
        document=MappingProxyType(document),
        hash=hashlib.sha256(canonical).hexdigest(),
    )


def load_policy(path: str | Path | None = None) -> Policy:
    target = Path(path) if path is not None else files("coord_tracker_bridge").joinpath(
        "policies/default-v2.json"
    )
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("policy root must be an object")
    return _policy_from_mapping(raw)
