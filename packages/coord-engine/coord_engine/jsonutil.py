"""Canonical compact JSON serialization for coord-engine hot paths.

Whitespace is not part of the machine-output contract. Keeping the policy in
one helper prevents public ``--json`` surfaces and hot stored aggregates from
drifting back to pretty-printed payloads independently.
"""

from __future__ import annotations

import json
from typing import Any


def dumps(value: Any) -> str:
    """Serialize *value* without insignificant spaces or a trailing newline."""
    return json.dumps(value, separators=(",", ":"))


def print_json(value: Any) -> None:
    """Emit one compact JSON document followed by exactly one newline."""
    print(dumps(value))
