"""PyObjC NSColor factories built from the pure hex tokens in palette.

This module is macOS-only — it imports PyObjC. Anything that needs
colours but does not import PyObjC should pull from theme.palette
instead.
"""
from __future__ import annotations

from AppKit import NSColor  # type: ignore[import-not-found]

from . import palette


def _hex(value: str) -> NSColor:
    h = value.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)


def bg() -> NSColor: return _hex(palette.BG)
def bg_elev() -> NSColor: return _hex(palette.BG_ELEV)
def border() -> NSColor: return _hex(palette.BORDER)

def text() -> NSColor: return _hex(palette.TEXT)
def text_secondary() -> NSColor: return _hex(palette.TEXT_SECONDARY)
def text_tertiary() -> NSColor: return _hex(palette.TEXT_TERTIARY)

def violet() -> NSColor: return _hex(palette.ACCENT_VIOLET)
def violet_hover() -> NSColor: return _hex(palette.ACCENT_VIOLET_HOVER)
def violet_tint() -> NSColor: return _hex(palette.ACCENT_VIOLET_TINT)

def mint() -> NSColor: return _hex(palette.ACCENT_MINT)
def mint_hover() -> NSColor: return _hex(palette.ACCENT_MINT_HOVER)
def mint_tint() -> NSColor: return _hex(palette.ACCENT_MINT_TINT)

def cyan() -> NSColor: return _hex(palette.ACCENT_CYAN)
def cyan_deep() -> NSColor: return _hex(palette.ACCENT_CYAN_DEEP)

def warning() -> NSColor: return _hex(palette.WARNING)
def error() -> NSColor: return _hex(palette.ERROR)
