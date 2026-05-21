"""The reader-agnostic Day One entry model."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DayOneEntry:
    """One Day One journal entry, normalized across the readers.

    `creation_date` is timezone-aware (UTC). `tags` is a tuple so the
    record stays hashable/frozen. `location` is a composed place string
    or None. `photo_count` and `word_count` are lightweight metadata.
    """
    uuid: str
    creation_date: datetime
    text: str
    tags: tuple[str, ...]
    starred: bool
    journal: str
    location: str | None
    photo_count: int
    word_count: int
