"""Terminal output helpers — the one place stdout/stderr formatting lives.

Leaf module (stdlib only) so every layer can print user-facing output and
diagnostics without importing cli or risking an import cycle. Kept deliberately
tiny and dependency-free: these are the primitives the command and feature modules
build their human-readable output on.
"""

from __future__ import annotations

import json
import sys
from typing import Any


def print_json(data: Any) -> None:
    """Pretty-print a JSON document to stdout (the ``--format json`` surface)."""
    print(json.dumps(data, indent=2))


def err(msg: str) -> None:
    """Write an ``ERROR: ...`` line to stderr (does not exit)."""
    print(f"ERROR: {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    """Write a ``WARN: ...`` line to stderr."""
    print(f"WARN: {msg}", file=sys.stderr)


def info(msg: str) -> None:
    """Write an informational line to stdout."""
    print(msg)
