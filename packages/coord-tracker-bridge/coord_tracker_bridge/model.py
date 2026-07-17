"""Normalized, provider-neutral records exchanged by bridge components."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class CapabilityState(StrEnum):
    """Whether a source capability can authoritatively describe absence."""

    COMPLETE = "complete"
    UNSUPPORTED = "unsupported"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True, order=True)
class SourceIdentity:
    """The complete source-side identity; never inferred from tracker text."""

    provider: str
    namespace: str
    item_id: str

    def __post_init__(self) -> None:
        if not all((self.provider.strip(), self.namespace.strip(), self.item_id.strip())):
            raise ValueError("source identity fields must be non-empty")

    @property
    def key(self) -> str:
        # Length-prefixing makes the representation unambiguous without relying
        # on a delimiter that a provider may legally use in an id.
        parts = (self.provider, self.namespace, self.item_id)
        return "".join(f"{len(part)}:{part}" for part in parts)

    def to_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider,
            "namespace": self.namespace,
            "item_id": self.item_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceIdentity:
        return cls(str(value["provider"]), str(value["namespace"]), str(value["item_id"]))


@dataclass(frozen=True, slots=True)
class Diagnostic:
    scope: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"scope": self.scope, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class WorkRecord:
    source: SourceIdentity
    capability: str
    title: str
    lane: str
    priority: str = "P2"
    description: str = ""
    owner: str | None = None
    assignee: str | None = None
    workstream: str | None = None
    origin: str | None = None
    tags: tuple[str, ...] = ()
    archived: bool = False
    due_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.capability.strip() or not self.title.strip() or not self.lane.strip():
            raise ValueError("capability, title, and lane must be non-empty")
        object.__setattr__(self, "tags", tuple(dict.fromkeys(self.tags)))
        if self.due_at is not None:
            object.__setattr__(self, "due_at", _utc(self.due_at))


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One source observation, including completeness and capability health."""

    items: tuple[WorkRecord, ...]
    complete: bool
    diagnostics: tuple[Diagnostic, ...]
    capabilities: Mapping[str, CapabilityState]
    observed_at: datetime
    source_revision: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        normalized = {str(k): CapabilityState(v) for k, v in self.capabilities.items()}
        object.__setattr__(self, "capabilities", MappingProxyType(normalized))
        object.__setattr__(self, "observed_at", _utc(self.observed_at))
        seen: set[str] = set()
        for item in self.items:
            if item.source.key in seen:
                raise ValueError(f"duplicate source identity: {item.source.key}")
            seen.add(item.source.key)
            if item.capability not in normalized:
                raise ValueError(f"item capability not declared: {item.capability}")

    def absence_is_authoritative(self, capability: str) -> bool:
        return self.complete and self.capabilities.get(capability) is CapabilityState.COMPLETE


@dataclass(frozen=True, slots=True)
class ManagedRecord:
    """Tracker-side record normalized by a tracker adapter."""

    provider_id: str
    source: SourceIdentity
    capability: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    closed: bool = False

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.capability.strip():
            raise ValueError("provider_id and capability must be non-empty")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
