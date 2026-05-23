"""The menubar icon. Holds a reference to the NSStatusItem owned by
rumps and applies overlay states (idle / running / failure / down)
driven by the StatusModel.

This task wires up the idle and daemon-stopped states only. The
running pulse and the failure badge land in Task 12.
"""
from __future__ import annotations

from pathlib import Path

from AppKit import NSImage  # type: ignore[import-not-found]

from .model import OverallState, StatusModel

ASSET = Path(__file__).parent / "assets" / "menubar-icon.pdf"


class StatusItemController:
    def __init__(self, rumps_app, model: StatusModel) -> None:
        self._app = rumps_app
        self._model = model
        self._base_image = NSImage.alloc().initWithContentsOfFile_(str(ASSET))
        if self._base_image is not None:
            self._base_image.setTemplate_(True)
        self._apply()
        model.add_observer(lambda _m: self._apply())

    def _apply(self) -> None:
        try:
            ns_item = self._app._nsapp.nsstatusitem
            button = ns_item.button()
        except AttributeError:
            return
        if self._model.overall is OverallState.DAEMON_STOPPED:
            button.setImage_(self._base_image)
            button.setAlphaValue_(0.4)
        else:
            button.setImage_(self._base_image)
            button.setAlphaValue_(1.0)
