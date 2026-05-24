"""The NSPopover host. White background, fixed width.

Layout overview
───────────────
The popover has TWO sub-views stacked in the same content region.
Only one is visible at a time; they swap on user action:

  PRIMARY   — "Quick Record" surface: user's Moment annotation
              definitions, each with a one-tap Record button.
              This is the default view shown when the popover opens.

  SECONDARY — "Plugin Status" list: the existing scrollable view of
              plugins with their last-run timestamps and Run-now
              buttons. Reached via the "View Status →" button in the
              Quick Record footer.

A "← Quick Record" button appears at the top of the Plugin Status view
so the user can navigate back.

The popover also has a fixed footer at y=0 (36 pt) that contains the
Quit button; the footer is shared by both sub-views and is always
visible.
"""
from __future__ import annotations

from typing import Callable, Optional

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSPopover, NSScrollView, NSView,
    NSViewController, NSMakeRect, NSMakeSize,
    NSBezelStyleRounded,
)

from .._dispatch import on_main_thread
from .._objc_targets import attach as _attach
from ..model import StatusModel
from ..theme import colors
from .header import make_header
from .quick_record import make_quick_record_view


WIDTH = 360.0
HEADER_HEIGHT = 56.0
FOOTER_HEIGHT = 36.0
SWITCHER_HEIGHT = 32.0  # height of the "← Quick Record" back bar in status view
DEFAULT_BODY_HEIGHT = 240.0
DEFAULT_HEIGHT = HEADER_HEIGHT + DEFAULT_BODY_HEIGHT + FOOTER_HEIGHT  # 332


class PopoverRoot:
    def __init__(
        self,
        model: StatusModel,
        client,
        *,
        on_preferences: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
    ) -> None:
        """Construct the popover.

        Parameters
        ----------
        model:
            Shared status model; the popover subscribes for live updates.
        client:
            DaemonClient forwarded to plugin rows for "Run now" actions
            and to the quick-record view for recording annotations.
        on_preferences:
            Called when the user clicks the gear icon in the header.  If None,
            no gear button is rendered (test fixtures can omit it).
        on_quit:
            Called when the user clicks the "Quit" button in the footer.  If
            None, the footer still renders the button but it has no effect
            (safe default; in practice ``app.py`` always passes a handler).
        """
        self._model = model
        self._client = client
        self._popover = NSPopover.alloc().init()
        # NSPopoverBehaviorTransient = 1
        self._popover.setBehavior_(1)
        self._popover.setContentSize_(NSMakeSize(WIDTH, DEFAULT_HEIGHT))

        controller = NSViewController.alloc().init()
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, DEFAULT_HEIGHT))
        root.setWantsLayer_(True)
        root.layer().setBackgroundColor_(colors.bg().CGColor())

        # ── Header (always visible) ───────────────────────────────────────────
        header = make_header(model, on_preferences=on_preferences)
        header.setFrame_(NSMakeRect(0, DEFAULT_HEIGHT - HEADER_HEIGHT, WIDTH, HEADER_HEIGHT))
        root.addSubview_(header)

        # ── Footer (always visible) ───────────────────────────────────────────
        footer = _make_footer(on_quit=on_quit)
        footer.setFrame_(NSMakeRect(0, 0, WIDTH, FOOTER_HEIGHT))
        root.addSubview_(footer)

        # ── Body container — holds one sub-view at a time ─────────────────────
        body_top = FOOTER_HEIGHT
        body_height = DEFAULT_HEIGHT - HEADER_HEIGHT - FOOTER_HEIGHT

        body_container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, body_top, WIDTH, body_height)
        )
        root.addSubview_(body_container)

        # Internal state: which view is shown
        self._showing_status: bool = False

        # ── Build the quick-record primary view ───────────────────────────────
        quick_record_view = make_quick_record_view(
            client=client,
            model=model,
            on_view_status=self._show_status,
            width=WIDTH,
        )
        # The quick-record view manages its own height; clip it to body_height
        quick_record_view.setFrame_(NSMakeRect(0, 0, WIDTH, body_height))
        self._quick_record_view = quick_record_view

        # ── Build the plugin-status secondary view ────────────────────────────
        status_container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH, body_height)
        )
        status_container.setWantsLayer_(True)
        status_container.layer().setBackgroundColor_(colors.bg().CGColor())
        self._status_container = status_container

        # "← Quick Record" back bar at the top of the status view
        back_bar = _make_back_bar(on_back=self._show_quick_record, width=WIDTH)
        back_bar.setFrame_(NSMakeRect(0, body_height - SWITCHER_HEIGHT,
                                       WIDTH, SWITCHER_HEIGHT))
        status_container.addSubview_(back_bar)

        # Scrollable plugin list — occupies the area below the back bar
        plugin_area_height = body_height - SWITCHER_HEIGHT
        plugin_scroll_container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIDTH, plugin_area_height)
        )
        status_container.addSubview_(plugin_scroll_container)

        def render(_model=None):
            # Preserve scroll position across model-poll rebuilds
            saved_scroll = None
            try:
                for sv in plugin_scroll_container.subviews():
                    if isinstance(sv, NSScrollView):
                        saved_scroll = sv.contentView().bounds().origin
                        break
            except Exception:
                saved_scroll = None

            for sv in list(plugin_scroll_container.subviews()):
                sv.removeFromSuperview()

            from .bootstrap import make_bootstrap_card
            if self._model.daemon_stopped:
                card = make_bootstrap_card(WIDTH, plugin_area_height)
                plugin_scroll_container.addSubview_(card)
                return

            from .plugin_row import make_row, ROW_HEIGHT
            scroll = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(0, 0, WIDTH, plugin_area_height)
            )
            scroll.setHasVerticalScroller_(True)
            scroll.setBorderType_(0)
            scroll.setDrawsBackground_(False)
            content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, 0))
            content.setWantsLayer_(True)
            content.layer().setBackgroundColor_(colors.bg().CGColor())
            scroll.setDocumentView_(content)
            ordered = sorted(self._model.plugins, key=lambda p: (
                {"service": 0, "scheduled": 1, "manual": 2}.get(p.kind, 3), p.name
            ))
            y = 0
            for snapshot in ordered:
                row = make_row(
                    snapshot, client=self._client, model=self._model, width=WIDTH,
                )
                row.setFrame_(NSMakeRect(0, y, WIDTH, ROW_HEIGHT))
                content.addSubview_(row)
                y += ROW_HEIGHT
            content.setFrame_(NSMakeRect(0, 0, WIDTH, max(y, plugin_area_height)))
            plugin_scroll_container.addSubview_(scroll)
            if saved_scroll is not None:
                try:
                    scroll.contentView().scrollToPoint_(saved_scroll)
                    scroll.reflectScrolledClipView_(scroll.contentView())
                except Exception:
                    pass

        render()
        model.add_observer(on_main_thread(render))

        # ── Show the primary (quick-record) view by default ───────────────────
        body_container.addSubview_(quick_record_view)
        self._body_container = body_container

        controller.setView_(root)
        self._popover.setContentViewController_(controller)
        # Force light appearance (NSAppearanceNameAqua) so the popover
        # stays on the brand-mandated white regardless of system theme.
        from AppKit import NSAppearance  # type: ignore[import-not-found]
        self._popover.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))

    # ── View switching ─────────────────────────────────────────────────────────

    def _show_status(self) -> None:
        """Switch to the plugin-status secondary view."""
        if self._showing_status:
            return
        self._showing_status = True
        self._quick_record_view.removeFromSuperview()
        self._body_container.addSubview_(self._status_container)

    def _show_quick_record(self) -> None:
        """Switch back to the quick-record primary view."""
        if not self._showing_status:
            return
        self._showing_status = False
        self._status_container.removeFromSuperview()
        self._body_container.addSubview_(self._quick_record_view)

    # ── Popover lifecycle ─────────────────────────────────────────────────────

    @property
    def is_shown(self) -> bool:
        return bool(self._popover.isShown())

    def toggle(self, anchor_view) -> None:
        if self._popover.isShown():
            self._popover.close()
        else:
            # Reset to quick-record view on each open so the popover
            # always starts on the primary surface after closing.
            self._show_quick_record()
            # NSMaxYEdge = 5 (rect anchor edge that places below the menubar item)
            self._popover.showRelativeToRect_ofView_preferredEdge_(
                anchor_view.bounds(), anchor_view, 5
            )


# ── Footer factory ─────────────────────────────────────────────────────────────

def _make_footer(*, on_quit: Optional[Callable[[], None]]) -> NSView:
    """Build the thin footer bar containing the Quit button.

    The footer sits at the bottom of the popover content view (y=0) and is
    36 pt tall.  It carries a hairline separator at the top edge to visually
    divide it from the scrollable body above.
    """
    from AppKit import NSColor  # type: ignore[import-not-found]

    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, FOOTER_HEIGHT))

    # Hairline separator
    sep = NSView.alloc().initWithFrame_(NSMakeRect(0, FOOTER_HEIGHT - 1, WIDTH, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(
        NSColor.separatorColor().CGColor()
    )
    view.addSubview_(sep)

    # Quit button — sits on the right side of the footer
    quit_btn = NSButton.alloc().initWithFrame_(NSMakeRect(WIDTH - 84, 7, 72, 22))
    quit_btn.setTitle_("Quit")
    quit_btn.setBezelStyle_(NSBezelStyleRounded)

    def _on_quit(_sender):
        if on_quit is not None:
            on_quit()

    _attach(quit_btn, _on_quit)
    view.addSubview_(quit_btn)

    return view


# ── Back-bar factory ──────────────────────────────────────────────────────────

def _make_back_bar(
    *,
    on_back: Callable[[], None],
    width: float,
) -> NSView:
    """Build the thin back-navigation bar shown at the top of the
    plugin-status view. Contains a '← Quick Record' button that
    switches back to the primary quick-record surface."""
    from AppKit import NSColor  # type: ignore[import-not-found]

    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, SWITCHER_HEIGHT))
    view.setWantsLayer_(True)

    # Tinted background to visually distinguish the navigation bar
    view.layer().setBackgroundColor_(NSColor.controlBackgroundColor().CGColor())

    # Hairline separator at the bottom of the bar
    sep = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(_NSColor.separatorColor().CGColor())
    view.addSubview_(sep)

    back_btn = NSButton.alloc().initWithFrame_(NSMakeRect(8, 4, 160, 24))
    back_btn.setTitle_("← Quick Record")
    back_btn.setBezelStyle_(NSBezelStyleRounded)
    _attach(back_btn, lambda _s: on_back())
    view.addSubview_(back_btn)

    return view
