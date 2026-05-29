"""The popover's primary view: a list of user-recordable Fulcra
annotations the user logs often. Each row is one annotation definition
with a one-tap "Record" button that writes the annotation immediately
via the daemon's record_annotation UDS command.

Sprint B (2026-05-26) expanded this from Moment-only to ALL annotation
types. The view now:

  - Groups defs by ``annotation_type`` with a small section header per
    group ("Moments", "Durations", etc).
  - Adds a per-row comment input on every row.
  - Renders Duration rows with TWO patterns side-by-side: an inline
    duration field ("90m", "1h 30m") and a Start/Stop timer toggle.
  - Renders a "Recently recorded" section at the bottom with an Undo
    link per entry that calls the daemon's tombstone-write path.

The list is sourced from the daemon's ``quick_record_list`` UDS command
(60s cache). v1.5 should sort by recent-use and let the user pin/hide
entries via a Preferences tab.

State that survives popover open/close cycles (active timers + recent
list) lives on the PopoverRoot — see root.py. A daemon restart kills
any in-flight timer (we document that in the row tooltip).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSBezelStyleRounded, NSColor,
    NSScrollView,
    NSTextField, NSView, NSMakeRect,
)

from .._humanize import parse_duration_seconds
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
    """Build the quick-record primary view.

    Parameters
    ----------
    client:
        DaemonClient used to fetch definitions and record annotations.
    model:
        Shared StatusModel (unused by the current view but kept for
        future use, e.g. refreshing on daemon reconnect).
    on_view_status:
        Callback invoked when the user taps the "View Status" button to
        switch to the plugin-status secondary view.
    width:
        Popover content width in points.
    height:
        Body height the popover root has allotted to this view.
    active_timers:
        Shared dict keyed by definition_id; each entry is
        ``{"start_time": datetime, "name": str}``. Modified in place
        when the user starts/stops a Duration timer. Owned by the
        PopoverRoot so it survives popover close+reopen.
    recent:
        Shared list of ``{"source_id", "name", "ts", "undone"}`` entries
        for the "Recently recorded" section. New recordings appended at
        the end, rolled off oldest-first when the list exceeds 5.
    on_timer_changed:
        Called whenever the timer dict is mutated. The root uses this
        to toggle the cyan menubar overlay.
    on_preferences:
        Optional callback invoked with a tab name (e.g. ``"annotations"``)
        when the user taps the "Choose tracks to pin…" CTA in the empty
        state. Opens the Preferences window to the specified tab.
    """
    if active_timers is None:
        active_timers = {}
    if recent is None:
        recent = []

    HEIGHT = float(height)
    HEADER_H = 40.0
    FOOTER_H = 40.0
    BODY_H = HEIGHT - HEADER_H - FOOTER_H

    root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, HEIGHT))
    root.setWantsLayer_(True)
    root.layer().setBackgroundColor_(colors.bg().CGColor())

    # ── Header: "What do you want to log?" ─────────────────────────────────
    header = NSView.alloc().initWithFrame_(NSMakeRect(0, HEIGHT - HEADER_H, width, HEADER_H))
    header.setWantsLayer_(True)
    header.layer().setBackgroundColor_(colors.bg().CGColor())

    title = NSTextField.labelWithString_("What do you want to log?")
    title.setFont_(typography.title())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, 10, width - 32, 22))
    header.addSubview_(title)

    sep_top = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 1))
    sep_top.setWantsLayer_(True)
    sep_top.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    header.addSubview_(sep_top)
    root.addSubview_(header)

    # ── Scrollable annotation list ─────────────────────────────────────────
    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(0, FOOTER_H, width, BODY_H)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)

    from ..preferences.plugins_tab import _FlippedView
    content = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 0))
    content.setWantsLayer_(True)
    content.layer().setBackgroundColor_(colors.bg().CGColor())
    scroll.setDocumentView_(content)
    root.addSubview_(scroll)

    # ── Footer: "View Status →" button ────────────────────────────────────
    footer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, FOOTER_H))
    footer.setWantsLayer_(True)
    footer.layer().setBackgroundColor_(colors.bg().CGColor())

    sep_bot = NSView.alloc().initWithFrame_(NSMakeRect(0, FOOTER_H - 1, width, 1))
    sep_bot.setWantsLayer_(True)
    sep_bot.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    footer.addSubview_(sep_bot)

    status_btn = NSButton.alloc().initWithFrame_(NSMakeRect(width - 132, 8, 116, 24))
    status_btn.setTitle_("View Status →")
    status_btn.setBezelStyle_(NSBezelStyleRounded)
    _attach(status_btn, lambda _s: on_view_status())
    footer.addSubview_(status_btn)
    root.addSubview_(footer)

    # ── Helpers ───────────────────────────────────────────────────────────

    # Forward declaration — set after rebuild() is defined.
    rebuild_ref: dict[str, Callable[[], None]] = {}

    def _after_record(source_id: str, name: str) -> None:
        """Append a row to the in-memory recently-recorded list (cap
        RECENT_MAX). Triggers a rebuild so the user sees the new row
        immediately."""
        _append_recent(recent, {
            "source_id": source_id,
            "name": name,
            "ts": datetime.now(timezone.utc),
            "undone": False,
        })
        rebuild_ref["fn"]()

    def _start_timer(def_id: str, name: str) -> None:
        active_timers[def_id] = {
            "start_time": datetime.now(timezone.utc),
            "name": name,
        }
        if on_timer_changed is not None:
            on_timer_changed()
        rebuild_ref["fn"]()

    def _stop_timer_and_record(def_id: str, comment: str) -> None:
        timer = active_timers.pop(def_id, None)
        if on_timer_changed is not None:
            on_timer_changed()
        if timer is None:
            return
        end = datetime.now(timezone.utc)
        try:
            reply = client.record_annotation(
                def_id, comment=comment or None,
                start_time=_iso_z(timer["start_time"]),
                end_time=_iso_z(end),
            )
        except Exception:
            rebuild_ref["fn"]()
            return
        if reply.get("ok") and reply.get("source_id"):
            _after_record(reply["source_id"], reply.get("name") or timer["name"])
        else:
            rebuild_ref["fn"]()

    def _record_inline_duration(def_id: str, name: str, duration_text: str,
                                comment: str) -> None:
        seconds = parse_duration_seconds(duration_text)
        if seconds is None or seconds <= 0:
            # Silent no-op on bad input — the field's placeholder text
            # already tells the user the accepted formats. A future
            # iteration could surface an inline error.
            return
        end = datetime.now(timezone.utc)
        start = datetime.fromtimestamp(end.timestamp() - seconds, tz=timezone.utc)
        try:
            reply = client.record_annotation(
                def_id, comment=comment or None,
                start_time=_iso_z(start), end_time=_iso_z(end),
            )
        except Exception:
            return
        if reply.get("ok") and reply.get("source_id"):
            _after_record(reply["source_id"], reply.get("name") or name)

    def _record_moment(def_id: str, name: str, comment: str) -> None:
        try:
            reply = client.record_annotation(def_id, comment=comment or None)
        except Exception:
            return
        if reply.get("ok") and reply.get("source_id"):
            _after_record(reply["source_id"], reply.get("name") or name)

    def _toggle_favorite(def_id: str, currently_pinned: bool,
                          all_defs: list[dict]) -> None:
        """Star-click handler. Computes the desired new favorites set
        from the cached def list (which already carries ``pinned``
        flags) plus the user's click, then PUTs the full list to the
        daemon and rebuilds the popover.

        We send the full list (not a single add/remove) because the
        daemon's command surface is replace-all — keeps the file write
        atomic and avoids a race where two pin-toggles in quick
        succession would step on each other.
        """
        if not def_id:
            return
        new_set: set[str] = {
            d.get("id") for d in all_defs
            if d.get("pinned") and d.get("id") and d.get("id") != def_id
        }
        if not currently_pinned:
            new_set.add(def_id)
        try:
            client.set_quick_record_favorites(sorted(new_set))
        except Exception:
            # If the daemon dropped, leave the in-memory view alone —
            # the next popover open will sync up.
            return
        rebuild_ref["fn"]()

    def _undo(source_id: str) -> None:
        try:
            reply = client.delete_annotation(source_id)
        except Exception:
            return
        if reply.get("ok"):
            for entry in recent:
                if entry["source_id"] == source_id:
                    entry["undone"] = True
                    break
            rebuild_ref["fn"]()

    # ── Populate the content view ─────────────────────────────────────────

    def rebuild():
        for sv in list(content.subviews()):
            sv.removeFromSuperview()

        try:
            reply = client.quick_record_list()
        except Exception as exc:
            reply = {"ok": False, "error": str(exc), "definitions": []}

        if not reply.get("ok"):
            err_label = NSTextField.labelWithString_(
                f"Quick record unavailable: {reply.get('error', 'unknown')}"
            )
            err_label.setFont_(typography.small())
            err_label.setTextColor_(colors.text_secondary())
            err_label.setFrame_(NSMakeRect(16, 4, width - 32, 32))
            content.addSubview_(err_label)
            content.setFrame_(NSMakeRect(0, 0, width, 40))
            return

        all_defs = reply.get("definitions", [])
        defs, view_state = quick_record_view_state(all_defs)

        if view_state == "no_defs":
            _add_empty_message(
                content, width,
                "No annotation tracks yet. Create one at fulcradynamics.com "
                "and it'll show up here.",
            )
            return
        if view_state == "none_pinned":
            _add_pin_cta(content, width, on_preferences)
            return

        SECTION_H = 22.0
        MOMENT_ROW_H = 44.0
        DURATION_ROW_H = 84.0
        y = 0.0
        last_group: str | None = None
        for d in defs:
            atype = (d.get("annotation_type") or "").lower()
            if atype != last_group:
                header_view = _make_section_header(width, SECTION_H,
                                                   _group_label(atype))
                header_view.setFrame_(NSMakeRect(0, y, width, SECTION_H))
                content.addSubview_(header_view)
                y += SECTION_H
                last_group = atype
            if atype == "duration":
                row = _make_duration_row(
                    d, width, DURATION_ROW_H,
                    timer=active_timers.get(d.get("id") or ""),
                    on_start_timer=_start_timer,
                    on_stop_timer=_stop_timer_and_record,
                    on_record_inline=_record_inline_duration,
                    on_toggle_favorite=lambda did, pinned, _ad=all_defs:
                        _toggle_favorite(did, pinned, _ad),
                )
                row.setFrame_(NSMakeRect(0, y, width, DURATION_ROW_H))
                content.addSubview_(row)
                y += DURATION_ROW_H
            else:
                row = _make_moment_row(
                    d, width, MOMENT_ROW_H,
                    on_record=_record_moment,
                    on_toggle_favorite=lambda did, pinned, _ad=all_defs:
                        _toggle_favorite(did, pinned, _ad),
                )
                row.setFrame_(NSMakeRect(0, y, width, MOMENT_ROW_H))
                content.addSubview_(row)
                y += MOMENT_ROW_H

        # ── "Recently recorded" footer section ─────────────────────────
        if recent:
            recent_header = _make_section_header(width, SECTION_H,
                                                  "Recently recorded")
            recent_header.setFrame_(NSMakeRect(0, y, width, SECTION_H))
            content.addSubview_(recent_header)
            y += SECTION_H
            RECENT_ROW_H = 32.0
            # Most-recent at the top.
            for entry in reversed(recent):
                row = _make_recent_row(entry, width, RECENT_ROW_H,
                                       on_undo=_undo)
                row.setFrame_(NSMakeRect(0, y, width, RECENT_ROW_H))
                content.addSubview_(row)
                y += RECENT_ROW_H

        content.setFrame_(NSMakeRect(0, 0, width, max(y, BODY_H)))

    rebuild_ref["fn"] = rebuild
    rebuild()
    return root


# ── ISO helper ───────────────────────────────────────────────────────────────

def _iso_z(dt: datetime) -> str:
    """Format an aware datetime as ISO-8601 with trailing 'Z'."""
    return dt.isoformat().replace("+00:00", "Z")


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
    #   comment   138 .. 278 (140pt)
    #   Record    288 .. 348 (60pt, gap 10pt)
    STAR_W = 22.0
    NAME_W = 92.0
    COMMENT_W = 140.0
    BUTTON_W = 76.0

    star_btn = _make_star_button(
        pinned=pinned, height=height,
        on_click=lambda: (on_toggle_favorite(def_id, pinned)
                          if on_toggle_favorite and def_id else None),
    )
    star_btn.setFrame_(NSMakeRect(12, (height - 22) / 2, STAR_W, 22))
    row.addSubview_(star_btn)

    name_label = NSTextField.labelWithString_(def_name)
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
        on_record(def_id, def_name, comment)
        # Clear the comment field after a successful record.
        comment_field.setStringValue_("")

    _attach(btn, _on_click)
    row.addSubview_(btn)
    return row


def _make_duration_row(
    definition: dict,
    width: float,
    height: float,
    *,
    timer: dict | None,
    on_start_timer: Callable[[str, str], None],
    on_stop_timer: Callable[[str, str], None],
    on_record_inline: Callable[[str, str, str, str], None],
    on_toggle_favorite: Callable[[str, bool], None] | None = None,
) -> NSView:
    """One Duration row: name + comment + (inline duration field + Record)
    + Start/Stop timer button.

    Both record patterns coexist — the user can either type "90m" and
    click Record OR start a timer that ends with Stop. If a timer is
    running for this def, the inline duration controls are still
    rendered (the user is free to abandon the timer by recording inline),
    and the Start button changes to Stop.
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
    name_label.setFont_(typography.body())
    name_label.setTextColor_(colors.text())
    name_label.setLineBreakMode_(4)
    name_label.setFrame_(NSMakeRect(12 + STAR_W + 4, name_y,
                                     width - 228 - STAR_W - 4, 18))
    row.addSubview_(name_label)

    if timer is not None:
        hint = NSTextField.labelWithString_("● timer running")
        hint.setFont_(typography.small())
        # Cyan to match the menubar overlay colour.
        hint.setTextColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(
            0x10 / 255.0, 0xC7 / 255.0, 0xBE / 255.0, 1.0,
        ))
        hint.setFrame_(NSMakeRect(width - 180, name_y, 140, 18))
        row.addSubview_(hint)

    # Line 2: comment field, inline-duration field, Record-inline button,
    # Start/Stop timer button.
    #
    # Widths are tuned so the Record and Timer buttons sit ~24pt apart —
    # they fire very different actions (Record = log a one-shot duration;
    # Timer = start/stop a running timer) and used to be 8pt apart, easy
    # to mis-click. If you widen COMMENT_W back toward 140, the gap
    # collapses again. See SP1 L1 in the 2026-05-27 menubar drift audit.
    COMMENT_W = 120.0
    DURATION_W = 64.0
    RECORD_W = 56.0
    TIMER_W = 56.0
    row_y = name_y - 32

    comment_field = NSTextField.alloc().initWithFrame_(
        NSMakeRect(16, row_y, COMMENT_W, 22)
    )
    comment_field.setPlaceholderString_("Comment (optional)")
    comment_field.setFont_(typography.small())
    row.addSubview_(comment_field)

    duration_field = NSTextField.alloc().initWithFrame_(
        NSMakeRect(16 + COMMENT_W + 6, row_y, DURATION_W, 22)
    )
    duration_field.setPlaceholderString_("e.g. 90m")
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
        on_record_inline(def_id, def_name, duration_text, comment)
        comment_field.setStringValue_("")
        duration_field.setStringValue_("")

    _attach(record_btn, _on_record)
    row.addSubview_(record_btn)

    timer_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(width - TIMER_W - 12, row_y, TIMER_W, 22)
    )
    timer_btn.setBezelStyle_(NSBezelStyleRounded)
    if timer is None:
        timer_btn.setTitle_("Start")
        # Tooltip documents the in-memory-only caveat.
        timer_btn.setToolTip_(
            "Start a timer for this annotation. The timer lives in the "
            "menubar app — a restart or daemon stop cancels it."
        )

        def _on_start(_sender):
            if def_id:
                on_start_timer(def_id, def_name)

        _attach(timer_btn, _on_start)
    else:
        timer_btn.setTitle_("Stop")
        timer_btn.setToolTip_(
            "Stop the timer and record a Duration ending now."
        )

        def _on_stop(_sender):
            if def_id:
                comment = str(comment_field.stringValue() or "")
                on_stop_timer(def_id, comment)
                comment_field.setStringValue_("")

        _attach(timer_btn, _on_stop)
    row.addSubview_(timer_btn)
    return row


def _make_recent_row(
    entry: dict,
    width: float,
    height: float,
    *,
    on_undo: Callable[[str], None],
) -> NSView:
    """One row in the "Recently recorded" section.

    Tooltip on the Undo button calls out the soft-delete caveat —
    Fulcra's timeline will still show the original event because there's
    no per-event hard-delete primitive.
    """
    row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    row.setWantsLayer_(True)
    row.layer().setBackgroundColor_(colors.bg().CGColor())

    sep = NSView.alloc().initWithFrame_(NSMakeRect(16, 0, width - 16, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    row.addSubview_(sep)

    ago = _humanise_ago(entry.get("ts"))
    name = entry.get("name", "(unknown)")
    label_text = f"Recorded \"{name}\" {ago}"
    if entry.get("undone"):
        label_text += " — undone"

    label = NSTextField.labelWithString_(label_text)
    label.setFont_(typography.small())
    label.setTextColor_(colors.text_secondary())
    label.setLineBreakMode_(4)
    label.setFrame_(NSMakeRect(16, (height - 16) / 2, width - 100, 16))
    row.addSubview_(label)

    undo_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(width - 72, (height - 22) / 2, 60, 22)
    )
    undo_btn.setTitle_("Undo")
    undo_btn.setBezelStyle_(NSBezelStyleRounded)
    undo_btn.setEnabled_(not entry.get("undone", False))
    undo_btn.setToolTip_(
        "Mark this recording as undone. Fulcra has no per-event delete, "
        "so the event will still appear on your timeline — Undo writes "
        "a tombstone marker for the paper trail."
    )

    def _on_undo(_sender, sid: Any = entry.get("source_id")):
        if isinstance(sid, str) and sid:
            on_undo(sid)

    _attach(undo_btn, _on_undo)
    row.addSubview_(undo_btn)
    return row


# Maximum entries kept in the "Recently recorded" in-memory list.
# Picked at 5 because the popover is space-constrained and older entries
# are less likely to be useful for the undo affordance.
RECENT_MAX = 5


def _append_recent(recent: list[dict], entry: dict) -> list[dict]:
    """Append an entry to the recently-recorded list, enforcing the cap.

    Pure function — testable without AppKit. The popover's _after_record
    delegates to this so the cap logic lives in one place.

    Mutates ``recent`` in place and returns it (for convenience in
    test assertions).
    """
    recent.append(entry)
    while len(recent) > RECENT_MAX:
        recent.pop(0)
    return recent


def _humanise_ago(ts: datetime | None) -> str:
    """A short relative-time string ("just now", "2m ago", "1h ago")
    used in the Recently-recorded rows. Caps at hours — anything older
    is fine to round off because we only keep the last 5 entries."""
    if ts is None:
        return ""
    now = datetime.now(timezone.utc)
    delta = (now - ts).total_seconds()
    if delta < 5:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    return f"{int(delta // 3600)}h ago"
