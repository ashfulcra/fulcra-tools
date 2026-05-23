"""The NSPopover host. White background, fixed width, scrolling body.

Section content (plugin rows, bootstrap card) is added in later tasks.
For now this task lands the popover shell and the header — enough to
verify the white surface and the header refreshes on model changes.
"""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSPopover, NSView, NSViewController, NSMakeRect, NSMakeSize,
)

from ..model import StatusModel
from ..theme import colors
from .header import make_header


WIDTH = 360.0
DEFAULT_HEIGHT = 240.0


class PopoverRoot:
    def __init__(self, model: StatusModel) -> None:
        self._model = model
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
