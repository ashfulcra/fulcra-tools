"""One normalized, fail-closed change feed for ordinary coordination detection.

The file feed is a trigger, not authority: canonical documents remain the
source of truth.  This module only says which canonical namespaces may need a
bounded read.  A malformed envelope, missed record materialisation, exhausted
budget, or an unrecognised namespace is explicitly UNKNOWN and cannot license a
watermark advance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
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
    "router_state",
    "agent_state",
    "annotation_state",
    "health",
    "member_state",
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
    record: Optional[Mapping[str, Any]] = None


@dataclass(frozen=True)
class ChangeBatch:
    """The sealed result of one feed query; ``trusted`` gates watermark writes."""

    changes: tuple[Change, ...]
    coverage: Mapping[str, Coverage]
    trusted: bool
    envelope: Optional[Mapping[str, Any]] = None
    # The only cursor frontier a generation may publish.  The current
    # data-updates response does not attest one, so this is usually None and
    # generation publication fails closed until the feed supplies it.
    watermark: Optional[str] = None

    def for_namespace(self, namespace: str) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.namespace == namespace)


def _unknown() -> ChangeBatch:
    return ChangeBatch(
        (), MappingProxyType({name: Coverage.UNKNOWN for name in NAMESPACES}), False,
    )


def _freeze(value: Any) -> Any:
    """Recursively remove mutable containers from the sealed detector output."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _namespace(team: str, path: str) -> Optional[str]:
    prefix = f"team/{team}/"
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if rest.startswith("task/"):
        if rest in ("task/index.md", "task/log.md"):
            return "projection_metadata"
        return "tasks"
    if rest.startswith("directive/") or rest.startswith("_coord/directives/"):
        return "directives"
    if rest.startswith("review/"):
        return "reviews"
    if rest.startswith("_coord/forge/"):
        return "forge"
    if rest.startswith("presence/") or rest.startswith("roles/") or rest.startswith("role/"):
        return "presence_roles"
    if (rest.startswith("_coord/acks/")
            or rest.startswith("_coord/responses/")):
        return "acknowledgments_responses"
    if rest.startswith("_coord/router/"):
        return "router_state"
    if rest.startswith("_coord/agents/"):
        return "agent_state"
    if rest.startswith("_coord/annotate/"):
        return "annotation_state"
    if rest.startswith("_coord/health/"):
        return "health"
    if rest.startswith("member/"):
        return "member_state"
    if (rest in ("_coord/summaries.json", "_coord/projection-build-progress.json")
            or rest.startswith("_coord/projection/")
            or rest.startswith("_coord/projections/")):
        return "projection_metadata"
    return None


def _instant(row: Mapping[str, Any], state: str) -> Optional[tuple[datetime, str]]:
    key = {"uploaded": "uploaded_at", "archived": "archived_at", "deleted": "deleted_at"}.get(state)
    value = row.get(key) if key else None
    # Legacy deletion rows sometimes retain only uploaded_at.  It remains a
    # lifecycle instant, while a total absence is uncertain.
    value = value or row.get("uploaded_at")
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    instant = parsed.astimezone(timezone.utc)
    return instant, instant.isoformat().replace("+00:00", "Z")


def _file_identity(row: Mapping[str, Any]) -> Optional[str]:
    value = row.get("update_id", row.get("id", row.get("version_id")))
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _record_window(
    value: Any, *, prior_watermark: Optional[str],
) -> Optional[tuple[str, list[Mapping[str, Any]]]]:
    """Accept only a server-attested, exact record cursor window.

    The detector cannot turn a local observation time into a cursor boundary.
    ``after`` must prove the requested boundary, ``through`` must prove the
    returned horizon, and raw rows must carry their own identities. The
    ``data_types`` count is only a detector signal: production measurements show
    it persistently disagrees with cursor enumeration, so it is never an exact
    cardinality, threshold, diff, or substitute identity.
    """
    if not isinstance(value, Mapping) or not isinstance(prior_watermark, str):
        return None
    after, through, rows = value.get("after"), value.get("through"), value.get("records")
    if not isinstance(after, str) or not isinstance(through, str) or not isinstance(rows, list):
        return None
    requested = _instant({"uploaded_at": prior_watermark}, "uploaded")
    start = _instant({"uploaded_at": after}, "uploaded")
    end = _instant({"uploaded_at": through}, "uploaded")
    if requested is None or start is None or end is None or start[0] != requested[0] or end[0] < start[0]:
        return None
    if any(not isinstance(row, Mapping) for row in rows):
        return None
    for row in rows:
        at = _instant({"uploaded_at": row.get("recorded_at")}, "uploaded")
        if at is None or not (start[0] < at[0] <= end[0]):
            return None
    return end[1], rows


def _coordination_count(
    transport: Any, team: str, envelope: Mapping[str, Any], deadline: Deadline,
) -> Optional[tuple[str, int]]:
    """Return the queue authority's count from a live ``data_types`` envelope.

    ``data-updates`` reports counts by concrete data type, not an invented
    ``record_counts.coordination`` alias.  The queue's classified records
    authority is the only proof of which stream belongs to this team.  Missing
    or malformed evidence is UNKNOWN rather than a zero-count observation.
    """
    data_types = envelope.get("data_types")
    if not isinstance(data_types, Mapping) or deadline.expired():
        return None
    try:
        config, status = records.load_canonical_config_classified(
            transport, team, deadline=deadline,
        )
    except Exception:
        return None
    if deadline.expired() or status != "ok" or not isinstance(config, Mapping):
        return None
    channel = config.get("data_type")
    if not isinstance(channel, str) or not channel.strip() or channel not in data_types:
        return None
    count = data_types[channel]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return None
    return channel, count


def _feed_watermark(envelope: Mapping[str, Any], prior_watermark: Optional[str]) -> Optional[str]:
    """Accept only a server-attested feed frontier that cannot move backward."""
    frontier = _instant({"uploaded_at": envelope.get("through")}, "uploaded")
    if frontier is None:
        return None
    if prior_watermark is not None:
        prior = _instant({"uploaded_at": prior_watermark}, "uploaded")
        if prior is None or frontier[0] < prior[0]:
            return None
    return frontier[1]


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
        instants: dict[str, datetime] = {}
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
            normalized = _instant(row, state)
            if normalized is None:
                return _unknown()
            instant, at = normalized
            update_id = _file_identity(row)
            if update_id is None:
                return _unknown()
            coverage[namespace] = Coverage.DATA
            if update_id not in identities:
                identities.add(update_id)
                instants[update_id] = instant
                changes.append(Change(update_id, path, state, at, namespace))

        record_count = _coordination_count(self.transport, team, envelope, deadline)
        if record_count is None:
            return _unknown()
        channel, count = record_count
        # A cursor window needs a prior boundary.  On bootstrap, a zero signal
        # cannot supply one and therefore cannot claim CLEAR; the named recovery
        # scan may establish canonical section coverage later in the pipeline.
        # A positive signal without a boundary is UNKNOWN because observed data
        # could not be materialized.
        if prior_watermark is None:
            if count > 0:
                return _unknown()
            coverage["acknowledgments_responses"] = Coverage.NOT_RUN
        else:
            if deadline.expired():
                return _unknown()
            materialize = getattr(self.transport, "records_cursor", None)
            if materialize is None:
                return _unknown()
            try:
                cursor = materialize(channel, prior_watermark, deadline=deadline)
            except Exception:
                return _unknown()
            if deadline.expired():
                return _unknown()
            window = _record_window(cursor, prior_watermark=prior_watermark)
            if window is None:
                return _unknown()
            _through, record_rows = window
            # A positive signal that materializes no identities is an observed
            # disagreement, not a clean no-op. A zero signal is not proof either:
            # only this enumerated window can establish CLEAR or DATA.
            if count > 0 and not record_rows:
                return _unknown()
            for row in record_rows:
                identity = records.immutable_record_identity(row)
                if identity is None:
                    return _unknown()
                update_id = f"record:{identity}"
                if update_id not in identities:
                    identities.add(update_id)
                    normalized = _instant({"uploaded_at": row["recorded_at"]}, "uploaded")
                    if normalized is None:
                        return _unknown()
                    instant, at = normalized
                    instants[update_id] = instant
                    changes.append(Change(update_id, "record:" + identity, "recorded", at,
                                          "acknowledgments_responses", _freeze(dict(row))))
            if record_rows:
                coverage["acknowledgments_responses"] = Coverage.DATA

        trusted = not any(value is Coverage.UNKNOWN for value in coverage.values())
        changes.sort(key=lambda change: (instants[change.update_id], change.path, change.update_id))
        return ChangeBatch(
            tuple(changes), MappingProxyType(dict(coverage)), trusted, _freeze(envelope),
            _feed_watermark(envelope, prior_watermark),
        )
