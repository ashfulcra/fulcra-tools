"""PyObjC NSFont factories. macOS-only."""
from __future__ import annotations

from AppKit import NSFont  # type: ignore[import-not-found]


def title() -> NSFont:
    return NSFont.systemFontOfSize_weight_(16.0, 0.5)


def body() -> NSFont:
    return NSFont.systemFontOfSize_weight_(14.0, 0.0)


def small() -> NSFont:
    return NSFont.systemFontOfSize_weight_(12.0, 0.0)


def mono() -> NSFont:
    return NSFont.monospacedSystemFontOfSize_weight_(11.0, 0.0)
