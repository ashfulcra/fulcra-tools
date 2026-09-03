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

        return tuple(sorted({*self.workstream_projects.values(), *self.lane_projects.values()}))

    def project_for(self, lane: str, workstream: str | None) -> str | None:
        """Lane wins: it is the specific claim, the workstream is the general one."""

        return self.lane_projects.get(lane) or self.workstream_projects.get(workstream or "")


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
