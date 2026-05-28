"""Frozen-aware resource location.

The daemon serves bundled data files (the web-ui SPA, the in-app docs).
In a dev checkout those live in the workspace tree; in a py2app `.app`
they're copied under `Contents/Resources`. This module is the single
place that knows the difference, so the rest of the daemon never has to
care whether it's running frozen.
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    """True inside a py2app/PyInstaller bundle (both set ``sys.frozen``)."""
    return bool(getattr(sys, "frozen", False))


def resource_root() -> Path:
    """Base directory for bundled data files.

    Frozen: the ``.app/Contents/Resources`` directory — ``sys.executable``
    is ``.../Contents/MacOS/<app>``, so Resources is its parent's sibling.
    Dev: the workspace root (``packages/collect/fulcra_collect/`` → up 3).
    """
    if is_frozen():
        return Path(sys.executable).resolve().parent.parent / "Resources"
    return Path(__file__).resolve().parents[3]


def frontend_dir() -> Path:
    """Directory holding the built web-ui SPA (index.html + static/)."""
    if is_frozen():
        return resource_root() / "web-ui" / "dist"
    return resource_root() / "packages" / "web-ui" / "dist"


def docs_dir() -> Path:
    """Directory holding the in-app docs (e.g. how-do-i-get-my-data.md)."""
    return resource_root() / "docs"
