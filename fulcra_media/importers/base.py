"""Shared types for importers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

VALID_CATEGORIES = {"watched", "listened"}
VALID_CONFIDENCE = {"high", "medium", "low"}


@dataclass
class NormalizedEvent:
    importer: str
    service: str
    category: str            # "watched" or "listened"
    note: str
    title: str
    start_time: datetime
    end_time: datetime
    deterministic_id: str    # full source string e.g. "com.fulcra.media.netflix.<sha16>"
    timestamp_confidence: str
    external_ids: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.category not in VALID_CATEGORIES:
            raise ValueError(f"invalid category {self.category!r}")
        if self.timestamp_confidence not in VALID_CONFIDENCE:
            raise ValueError(f"invalid timestamp_confidence {self.timestamp_confidence!r}")
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("start_time and end_time must be timezone-aware")
