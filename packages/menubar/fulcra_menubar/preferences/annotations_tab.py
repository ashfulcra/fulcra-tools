"""Preferences → Annotations tab — manage annotation definitions.

Mirrors the web UI's /?route=settings page in scope:

  - Bulk favorites: a per-row NSSwitch toggles the def's pinned-in-
    quick-record status. Edits write through the existing
    set_quick_record_favorites UDS command. We read the live favorites
    set inside the toggle handler so concurrent edits from the popover
    star or the web UI don't get clobbered by stale state.
  - Per-row soft-delete: a Delete button on each row triggers an
    NSAlert confirmation, then calls DaemonClient.delete_definition
    (SP2 task 2). On success we remove the row from the inner view
    immediately so the user gets visual feedback; the daemon's quick-
    record cache is busted server-side so the next list call won't
    resurrect it.

Created as part of SP2 (drift audit 2026-05-27). Q1 answer:
"New Preferences tab + per-row '…'" — this tab is the bulk view; the
popover quick-record '…' menu (SP2 task 4) is the per-row affordance.
Q4 answer: simple NSAlert confirmation (no two-step undo).

Layout note: the sibling tabs use _TAB_H = 440 (the NSTabView in
window.py is HEIGHT-22 = 458pt tall; 440 is the historical content
height). We match that here rather than the 480 in the original plan
draft so spacing reads consistent across tabs.
"""
from __future__ import annotations

import logging
from typing import Any

from AppKit import (  # type: ignore[import-not-found]
    NSBezelStyleRounded,
    NSButton,
    NSLineBreakByTruncatingMiddle,
    NSLineBreakByWordWrapping,
    NSScrollView,
    NSSwitch,
    NSTextField,
    NSView,
    NSMakeRect,
)

from .._definition_delete import show_delete_alert
from .._objc_targets import attach as _attach
from ..daemon_client import DaemonClient
from ..theme import colors, typography

_log = logging.getLogger("fulcra_menubar.preferences.annotations")

_TAB_W = 640.0
_TAB_H = 440.0
_ROW_H = 56.0  # name + type label + favorites switch + delete button per row


def make_annotations_tab(*, client: DaemonClient) -> NSView:
    """Build and return the NSView root for the Annotations tab.

    Layout (top-down in AppKit y-coords; tab height = 440):
      y = TAB_H - 36 .. - 16:  Header "Annotations"
      y = TAB_H - 96 .. - 68:  Subtitle (two-line, wrapped)
      y = 16        .. SCROLL_TOP: Scrollable list of definition rows
    Each row is _ROW_H pt tall:
      [Name] [annotation_type]   [pinned NSSwitch] [Delete… button]

    Refreshes via close + reopen Preferences (the plan's accepted
    SP2-stage pattern); a future polish could subscribe to a daemon
    model so the tab redraws live.

    Args:
        client: DaemonClient used to fetch definitions + favorites and
            to call delete_definition.
    """
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _TAB_W, _TAB_H))
    # Paint the root view white so dark-mode system chrome can't bleed
    # through a transparent container — same fix applied to sibling tabs.
    view.setWantsLayer_(True)
    view.layer().setBackgroundColor_(colors.bg().CGColor())

    # ------- Header -------
    title = NSTextField.labelWithString_("Annotations")
    title.setFont_(typography.title())
    title.setTextColor_(colors.text())
    title.setFrame_(NSMakeRect(16, _TAB_H - 36, 400, 20))
    view.addSubview_(title)

    subtitle = NSTextField.labelWithString_(
        "Pin tracks to quick-record, or soft-delete tracks you no longer "
        "want in your pickers. Deleted tracks keep their already-written "
        "events on your Fulcra timeline."
    )
    subtitle.setFont_(typography.small())
    subtitle.setTextColor_(colors.text_secondary())
    subtitle.setFrame_(NSMakeRect(16, _TAB_H - 96, _TAB_W - 32, 32))
    subtitle.setLineBreakMode_(NSLineBreakByWordWrapping)
    view.addSubview_(subtitle)

    # ------- Scrollable list -------
    SCROLL_TOP = _TAB_H - 104
    SCROLL_Y = 16
    SCROLL_H = SCROLL_TOP - SCROLL_Y
    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(0, SCROLL_Y, _TAB_W, SCROLL_H)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)

    # Pull fresh data each time the tab is built. quick_record_list
    # returns {"definitions": [...]}, get_quick_record_favorites returns
    # {"ok": True, "favorites": [def_id, ...]}.
    try:
        defs_reply = client.quick_record_list()
    except Exception as exc:  # daemon-unavailable / socket error
        _log.warning("quick_record_list failed: %s", exc)
        defs_reply = {"definitions": []}
    try:
        favs_reply = client.get_quick_record_favorites()
    except Exception as exc:
        _log.warning("get_quick_record_favorites failed: %s", exc)
        favs_reply = {"favorites": []}

    defs = list((defs_reply or {}).get("definitions", []) or [])
    favs = set((favs_reply or {}).get("favorites", []) or [])

    inner_h = max(len(defs) * _ROW_H + 8.0, SCROLL_H)
    inner = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _TAB_W, inner_h))
    inner.setWantsLayer_(True)
    inner.layer().setBackgroundColor_(colors.bg().CGColor())

    # Render rows top-down (high y first, then descend by _ROW_H).
    y = inner_h - _ROW_H
    for d in defs:
        def_id = d.get("id", "")
        if not def_id:
            continue
        row = _make_def_row(d, favs, client, y_in_inner=y)
        inner.addSubview_(row)
        y -= _ROW_H

    # Empty-state hint when there are no defs (e.g., brand-new account).
    if not defs:
        empty = NSTextField.labelWithString_(
            "No annotation definitions yet. Create one from Quick Record "
            "in the popover, or in the Fulcra web UI."
        )
        empty.setFont_(typography.small())
        empty.setTextColor_(colors.text_tertiary())
        empty.setFrame_(NSMakeRect(16, inner_h - 40, _TAB_W - 32, 18))
        inner.addSubview_(empty)

    scroll.setDocumentView_(inner)
    view.addSubview_(scroll)
    return view


def _make_def_row(
    d: dict[str, Any],
    favs: set[str],
    client: DaemonClient,
    *,
    y_in_inner: float,
) -> NSView:
    """Build a single row in the Annotations list.

    Why each control is positioned where it is — the row is _ROW_H
    (56pt) tall. The name label sits high (y = _ROW_H - 28); the type
    caption sits underneath (y = _ROW_H - 48). The favorites switch and
    Delete button align horizontally near the right edge so the row's
    left side stays a single "what is this track" reading column.
    """
    def_id = d.get("id", "")
    name = d.get("name") or "(unnamed)"
    ann_type = d.get("annotation_type", "")

    row = NSView.alloc().initWithFrame_(
        NSMakeRect(0, y_in_inner, _TAB_W, _ROW_H)
    )

    # Name (primary label).
    name_lbl = NSTextField.labelWithString_(name)
    name_lbl.setFont_(typography.body())
    name_lbl.setTextColor_(colors.text())
    name_lbl.setFrame_(NSMakeRect(16, _ROW_H - 28, _TAB_W - 220, 18))
    name_lbl.setLineBreakMode_(NSLineBreakByTruncatingMiddle)
    row.addSubview_(name_lbl)

    # Annotation type (small caption underneath).
    type_lbl = NSTextField.labelWithString_(ann_type)
    type_lbl.setFont_(typography.small())
    type_lbl.setTextColor_(colors.text_secondary())
    type_lbl.setFrame_(NSMakeRect(16, _ROW_H - 48, _TAB_W - 220, 16))
    row.addSubview_(type_lbl)

    # Favorites switch ("pinned in quick record"). Read live favs in the
    # handler — the in-closure `favs` set is the snapshot at tab-build
    # time, which may be stale by the time the user toggles (e.g., the
    # popover star edited it). Always re-fetch + mutate + write so we
    # don't drop other rows' pin state.
    pinned = def_id in favs
    fav_switch = NSSwitch.alloc().initWithFrame_(
        NSMakeRect(_TAB_W - 196, _ROW_H - 38, 50, 22)
    )
    fav_switch.setState_(1 if pinned else 0)

    def on_pin_toggle(sender):
        # Read current favorites with NO empty-set fallback — a transient
        # daemon hiccup must not result in writing an empty / singleton
        # list over the user's real favorites. Same orphan-state hazard
        # as feedback_account_switch_caches.md: stale/empty state from a
        # failed read getting written back authoritatively.
        try:
            cur = client.get_quick_record_favorites() or {}
        except Exception as exc:
            _log.warning(
                "favorites read failed during pin toggle (%s): %s; "
                "reverting switch, not writing.",
                def_id, exc,
            )
            sender.setState_(0 if sender.state() else 1)
            return
        favs_now = set(cur.get("favorites", []) or [])
        if sender.state():
            favs_now.add(def_id)
        else:
            favs_now.discard(def_id)
        try:
            client.set_quick_record_favorites(list(favs_now))
        except Exception as exc:
            _log.warning("set_quick_record_favorites failed (%s): %s", def_id, exc)
            sender.setState_(0 if sender.state() else 1)

    _attach(fav_switch, on_pin_toggle)
    row.addSubview_(fav_switch)

    # Delete button — NSAlert confirmation, then call delete_definition.
    del_btn = NSButton.alloc().initWithFrame_(
        NSMakeRect(_TAB_W - 112, _ROW_H - 40, 96, 26)
    )
    del_btn.setTitle_("Delete…")
    del_btn.setBezelStyle_(NSBezelStyleRounded)

    def on_delete(_sender):
        # Confirmation + delete_definition + error alert all live in the
        # shared _definition_delete.show_delete_alert helper (single
        # source of truth across this tab and the popover quick-record
        # '…' menu). The success-side row reflow is tab-specific so it
        # stays here, wrapped in the on_done closure.
        def _on_deleted():
            # Remove the row AND shift every row below it up so the list
            # doesn't leave a 56pt visual hole. The inner content view's
            # height doesn't shrink, which is fine: the trailing
            # whitespace lives at the BOTTOM of the scrollable region
            # where it reads as expected slack, not a gap mid-list.
            #
            # AppKit coordinate note: rows are laid out top-down by
            # decreasing y (see the `y -= _ROW_H` loop in
            # make_annotations_tab). So rows VISUALLY below the deleted
            # row have LOWER y values, and closing the gap means
            # shifting them UP — which in unflipped Cocoa coords means
            # ADDING _ROW_H to their y origin.
            parent = row.superview()
            my_y = row.frame().origin.y
            row.removeFromSuperview()
            if parent is not None:
                for sibling in list(parent.subviews()):
                    sib_frame = sibling.frame()
                    if sib_frame.origin.y < my_y:
                        # NSView frames are mutable but the Cocoa idiom
                        # is to setFrame_ with a fresh NSRect.
                        new_frame = NSMakeRect(
                            sib_frame.origin.x,
                            sib_frame.origin.y + _ROW_H,
                            sib_frame.size.width,
                            sib_frame.size.height,
                        )
                        sibling.setFrame_(new_frame)

        show_delete_alert(def_id, name, client, on_done=_on_deleted)

    _attach(del_btn, on_delete)
    row.addSubview_(del_btn)

    return row
