"""On-disk state cache for fulcra-media-helpers.

Caches:
- Annotation definition IDs (created once via bootstrap)
- Tag UUIDs (created server-side, referenced by name locally)
- Per-importer watermarks (highest timestamp seen, for incremental runs)

Default location: ~/.config/fulcra-media/state.json. Every function takes an
explicit path argument to keep tests hermetic.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(
    os.environ.get("FULCRA_MEDIA_STATE")
    or os.path.expanduser("~/.config/fulcra-media/state.json")
)


@dataclass
class State:
    watched_definition_id: str | None = None
    listened_definition_id: str | None = None
    read_definition_id: str | None = None  # books (Goodreads, etc.)
    tag_ids: dict[str, str] = field(default_factory=dict)
    watermarks: dict[str, str] = field(default_factory=dict)


def load(path: Path = DEFAULT_PATH) -> State:
    if not path.exists():
        return State()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        # A truncated / zero-byte / mid-write state.json would brick every
        # importer. Fail open to an empty State so a partial-write
        # incident is recoverable on the next save without manual repair.
        return State()
    return State(
        watched_definition_id=raw.get("watched_definition_id"),
        listened_definition_id=raw.get("listened_definition_id"),
        read_definition_id=raw.get("read_definition_id"),
        tag_ids=raw.get("tag_ids", {}),
        watermarks=raw.get("watermarks", {}),
    )


def save(state: State, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True))
    # 0600: state holds annotation-def IDs + tag UUIDs + per-importer
    # watermarks. Not credentials per se, but the watermarks reveal
    # import cadence/recency and the def IDs let any local process
    # write into the user's Fulcra annotations via the importers.
    os.chmod(path, 0o600)
