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
    document = json.loads(json.dumps(raw))
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return Policy(
        version=version,
        lane_states=MappingProxyType({str(k): str(v) for k, v in raw.get("lane_states", {}).items()}),
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
        "policies/default-v1.json"
    )
    raw = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("policy root must be an object")
    return _policy_from_mapping(raw)
