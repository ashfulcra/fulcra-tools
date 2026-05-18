"""On-disk state for fulcra-attention.

Mirrors fulcra-media's state.py pattern. One Attention DurationAnnotation
definition; per-client watermarks (highest end_time seen); cached tag UUIDs.

Default location: ~/.config/fulcra-attention/state.json. Every function
takes an explicit path argument for hermetic tests.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_PATH = Path(
    os.environ.get("FULCRA_ATTENTION_STATE")
    or os.path.expanduser("~/.config/fulcra-attention/state.json")
)


@dataclass
class State:
    attention_definition_id: str | None = None
    tag_ids: dict[str, str] = field(default_factory=dict)
    watermarks: dict[str, str] = field(default_factory=dict)
    hostname: str | None = None  # local machine name, set by `setup`


def load(path: Path = DEFAULT_PATH) -> State:
    if not path.exists():
        return State()
    raw = json.loads(path.read_text())
    return State(
        attention_definition_id=raw.get("attention_definition_id"),
        tag_ids=raw.get("tag_ids", {}),
        watermarks=raw.get("watermarks", {}),
        hostname=raw.get("hostname"),
    )


def save(state: State, path: Path = DEFAULT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True))
    os.chmod(path, 0o600)
