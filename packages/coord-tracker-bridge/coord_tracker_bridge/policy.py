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
    included_origins: frozenset[str]
    close_absent: bool
    field_ownership: Mapping[str, str]
    document: Mapping[str, Any]
    hash: str

    def owns(self, field: str) -> str:
        return self.field_ownership.get(field, "source")


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
