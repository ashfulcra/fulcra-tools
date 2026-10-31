"""Preferences: protocol2 account-scoped track pins and legacy track management.

One complete handshake supplies choices, account/epoch and favorite revision.
Pin changes are additive CAS diffs off the UI thread, with explicit logical
response checks. Definition deletion remains a separate legacy management action.
"""
from __future__ import annotations

from threading import Thread
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
from .._dispatch import on_main_thread
from ..quick_record_client import native_client
from .._refresh_view import refresh_view
from .._objc_targets import attach as _attach, detach_view
from ..daemon_client import DaemonClient
from ..theme import colors, typography


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
    view = refresh_view(NSMakeRect(0, 0, _TAB_W, _TAB_H))
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

    inner = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, _TAB_W, SCROLL_H))
    scroll.setDocumentView_(inner)
    view.addSubview_(scroll)
    feedback = NSTextField.labelWithString_("Loading tracks…")
    feedback.setFont_(typography.small())
    feedback.setFrame_(NSMakeRect(16, 0, _TAB_W - 32, 20))
    view.addSubview_(feedback)
    state = {"workflow": None, "loading": False, "generation": 0, "refresh_pending": False}

    def refresh(error=False):
        state["generation"] += 1
        if state["loading"]:
            state["refresh_pending"] = True
            return
        state["loading"] = True
        generation = state["generation"]
        feedback.setStringValue_("Pin change failed; refreshing before retry." if error else "Loading tracks…")
        def load():
            try:
                if state["workflow"] is None:
                    state["workflow"] = native_client(client)
                snapshot, failed = state["workflow"].bootstrap(), False
            except Exception:
                snapshot, failed = None, True
            @on_main_thread
            def apply():
                state["loading"] = False
                if generation != state["generation"] or state["refresh_pending"]:
                    state["refresh_pending"] = False
                    refresh(error)
                    return
                for child in list(inner.subviews()):
                    detach_view(child)
                    child.removeFromSuperview()
                if failed:
                    feedback.setStringValue_("Could not load tracks. Reopen Preferences or reconnect in Collect.")
                    return
                definitions = snapshot["definitions"]
                inner_h = max(len(definitions) * _ROW_H + 8.0, SCROLL_H)
                inner.setFrame_(NSMakeRect(0, 0, _TAB_W, inner_h))
                y = inner_h - _ROW_H
                for definition in definitions:
                    row = _make_def_row(definition, set(snapshot["favorites"]), client,
                        workflow=state["workflow"], snapshot=snapshot, on_changed=refresh, y_in_inner=y)
                    inner.addSubview_(row)
                    y -= _ROW_H
                feedback.setStringValue_("Pin change failed; review the refreshed tracks before retrying." if error
                    else "" if definitions else "No moment or duration tracks yet. Create one in Fulcra.")
            apply()
        Thread(target=load, daemon=True, name="collect-favorite-choices").start()

    view._refresh_callback = refresh
    refresh()
    return view


def _make_def_row(
    d: dict[str, Any],
    favs: set[str],
    client: DaemonClient,
    *,
    y_in_inner: float,
    workflow, snapshot, on_changed,
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
        desired = bool(sender.state())
        sender.setEnabled_(False)
        def save():
            try:
                workflow.change_pin(def_id, pinned=desired, view=snapshot)
                failed = False
            except Exception:
                failed = True
            @on_main_thread
            def finish():
                if failed:
                    sender.setState_(1 if pinned else 0)
                sender.setEnabled_(True)
                on_changed(failed)
            finish()
        Thread(target=save, daemon=True, name="collect-favorite-change").start()

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
            on_changed()
            parent = row.superview()
            my_y = row.frame().origin.y
            detach_view(row)
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
