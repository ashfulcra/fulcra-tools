"""The NSPopover host. White background, fixed width, scrolling body.

Layout (top-to-bottom inside the popover content view):
  header   56 pt  — title, status pill, optional gear button
  body     variable (scrollable plugin list or bootstrap card)
  footer   36 pt  — Quit button (and future Reload config, etc.)
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


WIDTH = 360.0
HEADER_HEIGHT = 56.0
FOOTER_HEIGHT = 36.0
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
            DaemonClient forwarded to plugin rows for "Run now" actions.
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

        # ── Header ────────────────────────────────────────────────────────────
        header = make_header(model, on_preferences=on_preferences)
        header.setFrame_(NSMakeRect(0, DEFAULT_HEIGHT - HEADER_HEIGHT, WIDTH, HEADER_HEIGHT))
        root.addSubview_(header)

        # ── Footer ────────────────────────────────────────────────────────────
        footer = _make_footer(on_quit=on_quit)
        footer.setFrame_(NSMakeRect(0, 0, WIDTH, FOOTER_HEIGHT))
        root.addSubview_(footer)

        # ── Body (scrollable plugin list) ─────────────────────────────────────
        from .plugin_row import make_row, ROW_HEIGHT
        from .bootstrap import make_bootstrap_card

        body_top = FOOTER_HEIGHT
        body_height = DEFAULT_HEIGHT - HEADER_HEIGHT - FOOTER_HEIGHT

        body_container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, body_top, WIDTH, body_height)
        )
        root.addSubview_(body_container)

        def render(_model=None):
            # Capture the current scroll origin before tearing down so we
            # can restore it on the new scrollview after the rebuild. This
            # prevents the popover snapping back to the top every 2 seconds
            # while the model-poll fires with the user scrolled down.
            saved_scroll = None
            try:
                for sv in body_container.subviews():
                    if isinstance(sv, NSScrollView):
                        saved_scroll = sv.contentView().bounds().origin
                        break
            except Exception:
                saved_scroll = None

            for sv in list(body_container.subviews()):
                sv.removeFromSuperview()
            if self._model.daemon_stopped:
                card = make_bootstrap_card(WIDTH, body_height)
                body_container.addSubview_(card)
                return
            scroll = NSScrollView.alloc().initWithFrame_(
                NSMakeRect(0, 0, WIDTH, body_height)
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
            content.setFrame_(NSMakeRect(0, 0, WIDTH, max(y, body_height)))
            body_container.addSubview_(scroll)
            # Restore the scroll position captured before teardown so the
            # user's view doesn't jump back to the top on every poll tick.
            if saved_scroll is not None:
                try:
                    scroll.contentView().scrollToPoint_(saved_scroll)
                    scroll.reflectScrolledClipView_(scroll.contentView())
                except Exception:
                    pass

        render()
        model.add_observer(on_main_thread(render))

        controller.setView_(root)
        self._popover.setContentViewController_(controller)
        # Force light appearance (NSAppearanceNameAqua) so the popover
        # stays on the brand-mandated white regardless of system theme.
        from AppKit import NSAppearance  # type: ignore[import-not-found]
        self._popover.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameAqua"))

    @property
    def is_shown(self) -> bool:
        return bool(self._popover.isShown())

    def toggle(self, anchor_view) -> None:
        if self._popover.isShown():
            self._popover.close()
        else:
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
