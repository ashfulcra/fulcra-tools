"""Shared native quick-record row styling and protocol2 view entry point.

The durable client captures requests before socket IO; server state hydrates
recent records and timers on refresh. The default view is composed in
quick_record_v2.py.
"""
from __future__ import annotations

from typing import Callable

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSBezelStyleRounded, NSColor,
    NSTextField, NSView, NSMakeRect,
)

from .._objc_targets import attach as _attach
from ..daemon_client import DaemonClient
from ..model import StatusModel
from ..theme import colors, typography


# Headings shown above each group of rows. The order of keys mirrors the
# server's sort order (moments first, then durations, then anything else)
# so we never reorder client-side — we just chunk by annotation_type as we
# walk the flat list.
_GROUP_LABELS: dict[str, str] = {
    "moment": "Moments",
    "duration": "Durations",
    "read": "Read",
    "watched": "Watched",
    "listened": "Listened",
}


def _group_label(annotation_type: str) -> str:
    """Human-readable section header for a group. Falls back to a
    title-cased version of the raw annotation_type so a future Fulcra
    type doesn't render as an empty header."""
    return _GROUP_LABELS.get(annotation_type,
                             (annotation_type or "Other").title())


def quick_record_view_state(all_defs: list[dict]) -> tuple[list[dict], str]:
    """Decide what the quick-record popover body shows.

    The popover is a CURATED surface — it lists only the tracks the user
    pinned (in Preferences -> Annotations), never the full account. Returns
    ``(defs_to_render, state)`` where state is one of:

      - ``"list"``        -> defs_to_render = the pinned defs (non-empty)
      - ``"none_pinned"`` -> the account has defs but none are pinned; the
                             body shows a "Choose tracks to pin..." CTA
      - ``"no_defs"``     -> the account has no (non-deleted) defs at all; the
                             body points the user to create one on the web

    Pure: no I/O, no AppKit — unit-tested in test_quick_record_view_state.py.
    ``all_defs`` is the daemon's quick_record_list payload, each def carrying
    a ``pinned`` bool.
    """
    if not all_defs:
        return [], "no_defs"
    pinned = [d for d in all_defs if d.get("pinned")]
    if not pinned:
        return [], "none_pinned"
    return pinned, "list"


def make_quick_record_view(
    *,
    client: DaemonClient,
    model: StatusModel,
    on_view_status: Callable[[], None],
    width: float,
    height: float = 360.0,
    active_timers: dict[str, dict] | None = None,
    recent: list[dict] | None = None,
    on_timer_changed: Callable[[], None] | None = None,
    on_preferences: Callable[[str | None], None] | None = None,
) -> NSView:
    """Build the durable protocol2 view; row factories below share its styling."""
    from .quick_record_v2 import make_view
    return make_view(client=client, model=model, on_view_status=on_view_status,
                     width=width, height=height, active_timers=active_timers,
                     recent=recent, on_timer_changed=on_timer_changed,
                     on_preferences=on_preferences)


# ── Favorites helpers ────────────────────────────────────────────────────────

def _pinned_row_bg() -> NSColor:
    """Faint violet tint used as the background colour of pinned rows.
    Same hue as the menubar app's accent so the visual language stays
    consistent; alpha kept low so the row text still reads at full
    contrast.

    The default-bg fallback path uses ``colors.bg()`` so the contrast
    is automatic in both light and dark mode.
    """
    return NSColor.colorWithSRGBRed_green_blue_alpha_(
        0x8B / 255.0, 0x5C / 255.0, 0xF6 / 255.0, 0.10,
    )


def _make_star_button(*, pinned: bool, height: float,
                       on_click: Callable[[], None]) -> NSButton:
    """The per-row pin toggle. Uses the Unicode star glyphs (★ filled,
    ☆ outline) rather than SF Symbols so the same code renders on macOS
    11+ without needing the symbol bundle. The button is borderless so
    it reads as an inline icon rather than a competing CTA next to the
    Record button.

    Tooltip differs per state so accessibility tooling (and a curious
    user with mouseover-tooltips) can confirm what the click will do.
    """
    btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, 22, 22))
    btn.setTitle_("★" if pinned else "☆")
    btn.setBordered_(False)
    btn.setFont_(typography.body())
    # Filled star uses the same violet as the accent; empty star is
    # neutral so it doesn't visually scream for attention.
    if pinned:
        try:
            btn.setContentTintColor_(
                NSColor.colorWithSRGBRed_green_blue_alpha_(
                    0x8B / 255.0, 0x5C / 255.0, 0xF6 / 255.0, 1.0,
                )
            )
        except AttributeError:
            # macOS < 10.14 doesn't expose setContentTintColor_; the
            # text colour will fall back to system default which is fine.
            pass
    btn.setToolTip_(
        "Unpin from quick-record" if pinned else "Pin to quick-record favorites"
    )
    _attach(btn, lambda _s: on_click())
    return btn



# ── Empty-state renderers ─────────────────────────────────────────────────────

def _add_empty_message(content, width: float, text: str) -> None:
    """Render a single wrapped, secondary-text message in the popover body
    (used for the 'no tracks on the account' state)."""
    label = NSTextField.labelWithString_(text)
    label.setFont_(typography.small())
    label.setTextColor_(colors.text_secondary())
    label.setLineBreakMode_(0)  # NSLineBreakByWordWrapping
    label.setFrame_(NSMakeRect(16, 4, width - 32, 40))
    content.addSubview_(label)
    content.setFrame_(NSMakeRect(0, 0, width, 48))


def _add_pin_cta(content, width: float, on_preferences) -> None:
    """Render the 'nothing pinned yet' empty state: a short prompt plus a
    'Choose tracks to pin…' button that opens Preferences -> Annotations."""
    msg = NSTextField.labelWithString_(
        "No tracks pinned yet. Pin the ones you want to log from here."
    )
    msg.setFont_(typography.small())
    msg.setTextColor_(colors.text_secondary())
    msg.setLineBreakMode_(0)
    msg.setFrame_(NSMakeRect(16, 44, width - 32, 36))
    content.addSubview_(msg)

    btn = NSButton.alloc().initWithFrame_(NSMakeRect(16, 10, 200, 28))
    btn.setTitle_("Choose tracks to pin…")
    btn.setBezelStyle_(NSBezelStyleRounded)

    def _open(_sender):
        if on_preferences is not None:
            on_preferences("annotations")
    _attach(btn, _open)
    content.addSubview_(btn)
    content.setFrame_(NSMakeRect(0, 0, width, 88))


# ── Row factories ────────────────────────────────────────────────────────────

def _make_section_header(width: float, height: float, label: str) -> NSView:
    """A subtle header bar — secondary-colour text on a slightly elevated
    background, with a hairline separator below."""
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    view.setWantsLayer_(True)
    view.layer().setBackgroundColor_(colors.bg().CGColor())

    text = NSTextField.labelWithString_(label.upper())
    text.setFont_(typography.small())
    text.setTextColor_(colors.text_secondary())
    text.setFrame_(NSMakeRect(16, 2, width - 32, height - 4))
    view.addSubview_(text)

    sep = NSView.alloc().initWithFrame_(NSMakeRect(16, 0, width - 16, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    view.addSubview_(sep)
    return view


def _make_moment_row(
    definition: dict,
    width: float,
    height: float,
    *,
    on_record: Callable[[str, str, str], None],
    on_toggle_favorite: Callable[[str, bool], None] | None = None,
) -> NSView:
    """One Moment row: star + name + comment input + Record button.

    The star button is rendered on the LEFT before the name. Empty
    star (☆) when not pinned → click adds to favorites; filled star
    (★) when pinned → click removes. Pinned rows also get a faint
    violet tint background so the pin state is visible at a glance
    without parsing the icon.
    """
    row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    row.setWantsLayer_(True)
    pinned = bool(definition.get("pinned"))
    row.layer().setBackgroundColor_(
        _pinned_row_bg().CGColor() if pinned else colors.bg().CGColor()
    )

    sep = NSView.alloc().initWithFrame_(NSMakeRect(16, 0, width - 16, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    row.addSubview_(sep)

    def_id = definition.get("id", "")
    def_name = definition.get("name", "(unnamed)")

    # Layout: star + name on the left, then comment field, then Record button.
    # With width=360 the slots end up as:
    #   star      12 .. 34   (22pt)
    #   name      38 .. 130  (92pt)
    #   comment  138 .. 272  (134pt)  — right edge flush to Record's left edge
    #   Record   272 .. 348  (76pt, gap 0pt — tight but not overlapping)
    # COMMENT_W = Record_left - comment_x = (width - BUTTON_W - 12) - (12 + STAR_W + 4 + NAME_W + 8)
    #           = (360 - 76 - 12) - (12 + 22 + 4 + 92 + 8) = 272 - 138 = 134
    STAR_W = 22.0
    NAME_W = 92.0
    COMMENT_W = 134.0
    BUTTON_W = 76.0

    star_btn = _make_star_button(
        pinned=pinned, height=height,
        on_click=lambda: (on_toggle_favorite(def_id, pinned)
                          if on_toggle_favorite and def_id else None),
    )
    star_btn.setFrame_(NSMakeRect(12, (height - 22) / 2, STAR_W, 22))
    row.addSubview_(star_btn)

    name_label = NSTextField.labelWithString_(def_name)
    name_label.setToolTip_(def_name)
    name_label.setFont_(typography.body())
    name_label.setTextColor_(colors.text())
    name_label.setLineBreakMode_(4)  # NSLineBreakByTruncatingTail
    name_label.setFrame_(NSMakeRect(12 + STAR_W + 4, (height - 18) / 2,
                                     NAME_W, 18))
    row.addSubview_(name_label)

    comment_field = NSTextField.alloc().initWithFrame_(
        NSMakeRect(12 + STAR_W + 4 + NAME_W + 8, (height - 22) / 2,
                   COMMENT_W, 22)
    )
    comment_field.setPlaceholderString_("Comment (optional)")
    comment_field.setFont_(typography.small())
    row.addSubview_(comment_field)

    btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(width - BUTTON_W - 12, (height - 24) / 2, BUTTON_W, 24)
    )
    btn.setTitle_("Record")
    btn.setBezelStyle_(NSBezelStyleRounded)

    def _on_click(_sender):
        if not def_id:
            return
        comment = str(comment_field.stringValue() or "")
        if on_record(def_id, def_name, comment):
            comment_field.setStringValue_("")

    _attach(btn, _on_click)
    row.addSubview_(btn)
    return row


def _make_duration_row(
    definition: dict,
    width: float,
    height: float,
    *,
    on_start_timer: Callable[[str, str], None],
    on_record_inline: Callable[[str, str, str, str], None],
    on_toggle_favorite: Callable[[str, bool], None] | None = None,
) -> NSView:
    """One duration track: note, inline duration and a separate Start action.

    Saved timers render in their own section by timer UUID, including timers
    whose track was unpinned or renamed. No timer lifetime lives in a row.
    """
    row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    row.setWantsLayer_(True)
    pinned = bool(definition.get("pinned"))
    row.layer().setBackgroundColor_(
        _pinned_row_bg().CGColor() if pinned else colors.bg().CGColor()
    )

    sep = NSView.alloc().initWithFrame_(NSMakeRect(16, 0, width - 16, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    row.addSubview_(sep)

    def_id = definition.get("id", "")
    def_name = definition.get("name", "(unnamed)")

    # Line 1: star + name label spanning most of the width, with the
    # timer-state hint on the right if a timer is running.
    name_y = height - 24
    STAR_W = 22.0
    star_btn = _make_star_button(
        pinned=pinned, height=22.0,
        on_click=lambda: (on_toggle_favorite(def_id, pinned)
                          if on_toggle_favorite and def_id else None),
    )
    star_btn.setFrame_(NSMakeRect(12, name_y - 2, STAR_W, 22))
    row.addSubview_(star_btn)

    name_label = NSTextField.labelWithString_(def_name)
    name_label.setToolTip_(def_name)
    name_label.setFont_(typography.body())
    name_label.setTextColor_(colors.text())
    name_label.setLineBreakMode_(4)
    name_label.setFrame_(NSMakeRect(12 + STAR_W + 4, name_y,
                                     width - 200 - STAR_W - 4, 18))
    row.addSubview_(name_label)

    # Line 2: comment field, inline-duration field, Record-inline button,
    # Start/Stop timer button.
    #
    # Widths are tuned so the Record and Timer buttons sit ~24pt apart —
    # they fire very different actions (Record = log a one-shot duration;
    # Timer = start/stop a running timer) and used to be 8pt apart, easy
    # to mis-click. If you widen COMMENT_W back toward 140, the gap
    # collapses again. See SP1 L1 in the 2026-05-27 menubar drift audit.
    COMMENT_W = 114.0
    DURATION_W = 58.0
    RECORD_W = 68.0
    TIMER_W = 60.0
    row_y = name_y - 32

    comment_field = NSTextField.alloc().initWithFrame_(
        NSMakeRect(16, row_y, COMMENT_W, 22)
    )
    comment_field.setPlaceholderString_("Note (optional)")
    comment_field.setFont_(typography.small())
    row.addSubview_(comment_field)

    duration_field = NSTextField.alloc().initWithFrame_(
        NSMakeRect(16 + COMMENT_W + 6, row_y, DURATION_W, 22)
    )
    duration_field.setPlaceholderString_("90m")
    duration_field.setToolTip_("Duration, such as 90m or 1h 30m.")
    duration_field.setFont_(typography.small())
    row.addSubview_(duration_field)

    record_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(16 + COMMENT_W + 6 + DURATION_W + 6, row_y, RECORD_W, 22)
    )
    record_btn.setTitle_("Record")
    record_btn.setBezelStyle_(NSBezelStyleRounded)

    def _on_record(_sender):
        if not def_id:
            return
        comment = str(comment_field.stringValue() or "")
        duration_text = str(duration_field.stringValue() or "")
        if on_record_inline(def_id, def_name, duration_text, comment):
            comment_field.setStringValue_("")
            duration_field.setStringValue_("")

    _attach(record_btn, _on_record)
    row.addSubview_(record_btn)

    timer_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(width - TIMER_W - 12, row_y, TIMER_W, 22)
    )
    timer_btn.setBezelStyle_(NSBezelStyleRounded)
    timer_btn.setTitle_("Start")
    timer_btn.setToolTip_("Save a timer. Its original start survives app and daemon restarts.")

    def _on_start(_sender):
        if def_id:
            on_start_timer(def_id, def_name)

    _attach(timer_btn, _on_start)
    row.addSubview_(timer_btn)
    return row
