"""Convert a DayOneEntry into a fulcra_csv GenericEvent."""
from __future__ import annotations

import re

from fulcra_csv.events import INSTANT, GenericEvent, derive_source_id

from .entry import DayOneEntry

SOURCE_PREFIX = "com.fulcra.dayone"

# Day One embeds photos/videos as Markdown image links pointing at a
# dayone-moment:// (or dayone2://) URI. Replace each with a [photo] marker.
_MOMENT_RE = re.compile(r"!\[[^\]]*\]\(dayone[^)]*\)")


def _clean_text(text: str) -> str:
    return _MOMENT_RE.sub("[photo]", text)


def _title_from(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return None


def to_event(entry: DayOneEntry) -> GenericEvent:
    """Map a DayOneEntry to an instant GenericEvent ready for run_import."""
    external_ids: dict = {
        "dayone_uuid": entry.uuid,
        "journal": entry.journal,
        "starred": entry.starred,
        "word_count": entry.word_count,
        "photo_count": entry.photo_count,
    }
    if entry.location:
        external_ids["location"] = entry.location
    return GenericEvent(
        start_time=entry.creation_date,
        note=_clean_text(entry.text),
        title=_title_from(entry.text),
        source_id=derive_source_id(SOURCE_PREFIX, entry.uuid),
        end_time=None,
        extra_tags=tuple(entry.tags),
        annotation_type=INSTANT,
        external_ids=external_ids,
    )
