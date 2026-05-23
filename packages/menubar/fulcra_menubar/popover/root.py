"""The NSPopover host. White background, fixed width, scrolling body."""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSPopover, NSScrollView, NSView, NSViewController, NSMakeRect, NSMakeSize,
)

from ..model import StatusModel
from ..theme import colors
from .header import make_header


WIDTH = 360.0
DEFAULT_HEIGHT = 240.0


class PopoverRoot:
    def __init__(self, model: StatusModel, client) -> None:
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

        header = make_header(model)
        header.setFrame_(NSMakeRect(0, DEFAULT_HEIGHT - 56, WIDTH, 56))
        root.addSubview_(header)

        from .plugin_row import make_row, ROW_HEIGHT

        # Section body: a flipped, vertically-stacked list of plugin rows.
        from AppKit import NSScrollView, NSClipView  # type: ignore[import-not-found]

        body_height = DEFAULT_HEIGHT - 56  # below the header
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
        root.addSubview_(scroll)

        def rebuild_rows(_model=None):
            # Clear existing subviews.
            for sv in list(content.subviews()):
                sv.removeFromSuperview()
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

        rebuild_rows()
        model.add_observer(rebuild_rows)

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
