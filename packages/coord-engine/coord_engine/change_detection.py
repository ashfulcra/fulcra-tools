"""One normalized, fail-closed change feed for ordinary coordination detection.

The file feed is a trigger, not authority: canonical documents remain the
source of truth.  This module only says which canonical namespaces may need a
bounded read.  A malformed envelope, missed record materialisation, exhausted
budget, or an unrecognised namespace is explicitly UNKNOWN and cannot license a
watermark advance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

from .budget import Deadline
from . import records


class Coverage(str, Enum):
    NOT_RUN = "NOT_RUN"
    CLEAR = "CLEAR"
    DATA = "DATA"
    UNKNOWN = "UNKNOWN"


NAMESPACES = (
    "tasks",
    "directives",
    "reviews",
    "forge",
    "presence_roles",
    "acknowledgments_responses",
    "projection_metadata",
    "unknown_unsupported",
)


@dataclass(frozen=True)
class Change:
    """One normalized lifecycle update, identified independently of its path."""

    update_id: str
    path: str
    state: str
    at: str
    namespace: str
    record: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ChangeBatch:
    """The sealed result of one feed query; ``trusted`` gates watermark writes."""

    changes: tuple[Change, ...]
    coverage: Mapping[str, Coverage]
    trusted: bool
    envelope: Optional[Mapping[str, Any]] = None

    def for_namespace(self, namespace: str) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.namespace == namespace)


def _unknown() -> ChangeBatch:
    return ChangeBatch((), {name: Coverage.UNKNOWN for name in NAMESPACES}, False)


def _namespace(team: str, path: str) -> Optional[str]:
    prefix = f"team/{team}/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if rest.startswith("task/"):
        return "tasks"
    if rest.startswith("directive/") or rest.startswith("_coord/directives/"):
        return "directives"
    if rest.startswith("review/"):
        return "reviews"
    if rest.startswith("_coord/forge/"):
        return "forge"
    if rest.startswith("presence/") or rest.startswith("roles/") or rest.startswith("role/"):
        return "presence_roles"
    if rest.startswith("_coord/acks/") or rest.startswith("response/"):
        return "acknowledgments_responses"
    if rest in ("_coord/summaries.json", "_coord/projection-build-progress.json") or rest.startswith("_coord/projection/"):
        return "projection_metadata"
    return None


def _instant(row: Mapping[str, Any], state: str) -> Optional[str]:
    key = {"uploaded": "uploaded_at", "archived": "archived_at", "deleted": "deleted_at"}.get(state)
    value = row.get(key) if key else None
    # Legacy deletion rows sometimes retain only uploaded_at.  It remains a
    # lifecycle instant, while a total absence is uncertain.
    value = value or row.get("uploaded_at")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _file_identity(row: Mapping[str, Any], path: str, state: str, at: str) -> str:
    value = row.get("update_id", row.get("id", row.get("version_id")))
    if isinstance(value, str) and value.strip():
        return value.strip()
    # The normalized lifecycle tuple is immutable for this feed generation. It
    # is intentionally NOT a path-only key: delete/recreate retains both facts.
    return f"file:{path}:{state}:{at}"


class ChangeDetector:
    """The sole ordinary detector: exactly one bounded ``data-updates`` read."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport

    def poll(self, team: str, prior_watermark: Optional[str], deadline: Deadline) -> ChangeBatch:
        if deadline.expired():
            return _unknown()
        reader = getattr(self.transport, "data_updates", None)
        if reader is None:
            return _unknown()
        try:
            envelope = reader(prior_watermark, deadline=deadline)
        except Exception:
            return _unknown()
        if deadline.expired() or not isinstance(envelope, Mapping):
            return _unknown()
        rows = envelope.get("file_changes")
        if not isinstance(rows, list):
            return _unknown()

        coverage = {name: Coverage.CLEAR for name in NAMESPACES}
        changes: list[Change] = []
        identities: set[str] = set()
        for row in rows:
            if deadline.expired() or not isinstance(row, Mapping):
                return _unknown()
            raw_path, state = row.get("path", row.get("full_name")), row.get("state")
            if not isinstance(raw_path, str) or not raw_path.strip() or state not in ("uploaded", "archived", "deleted"):
                return _unknown()
            path = raw_path.strip().lstrip("/")
            namespace = _namespace(team, path)
            if namespace is None:
                # Account-wide changes outside this team are harmless; an unknown
                # path *inside* the team makes the team's feed coverage doubtful.
                if path.startswith(f"team/{team}/"):
                    coverage["unknown_unsupported"] = Coverage.UNKNOWN
                continue
            at = _instant(row, state)
            if at is None:
                return _unknown()
            update_id = _file_identity(row, path, state, at)
            coverage[namespace] = Coverage.DATA
            if update_id not in identities:
                identities.add(update_id)
                changes.append(Change(update_id, path, state, at, namespace))

        record_counts = envelope.get("record_counts", {})
        if record_counts is not None:
            if not isinstance(record_counts, Mapping):
                return _unknown()
            count = record_counts.get("coordination", 0)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                return _unknown()
            if count:
                if deadline.expired():
                    return _unknown()
                materialize = getattr(self.transport, "records_cursor", None)
                if materialize is None:
                    return _unknown()
                try:
                    rows = materialize("coordination", prior_watermark, deadline=deadline)
                except Exception:
                    return _unknown()
                if deadline.expired() or not isinstance(rows, list):
                    return _unknown()
                materialized = 0
                for row in rows:
                    identity = records.immutable_record_identity(row)
                    if identity is None:
                        return _unknown()
                    materialized += 1
                    update_id = f"record:{identity}"
                    if update_id not in identities:
                        identities.add(update_id)
                        at = row["recorded_at"]
                        changes.append(Change(update_id, "record:" + identity, "recorded", at,
                                              "acknowledgments_responses", dict(row)))
                if materialized < count:
                    return _unknown()
                coverage["acknowledgments_responses"] = Coverage.DATA

        trusted = not any(value is Coverage.UNKNOWN for value in coverage.values())
        changes.sort(key=lambda change: (change.at, change.path, change.update_id))
        return ChangeBatch(tuple(changes), coverage, trusted, envelope)
