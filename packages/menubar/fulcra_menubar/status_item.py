"""The menubar icon. Holds a reference to the NSStatusItem owned by
rumps and applies overlay states driven by the StatusModel.

Three overlays:
  - running pulse: a violet glow CALayer that fades in/out while the
    in-flight set is non-empty.
  - failure badge: a small red dot in the bottom-right corner while any
    enabled plugin has consecutive_failures > 0.
  - daemon-down: the base image at 40% alpha.

The base template image stays untouched so macOS continues to tint it
with the menubar's foreground colour.
"""
from __future__ import annotations

from pathlib import Path

from AppKit import (  # type: ignore[import-not-found]
    NSBezierPath, NSColor, NSCompositingOperationSourceOver,
    NSImage, NSMakeRect,
)
from Quartz import (  # type: ignore[import-not-found]
    CABasicAnimation, CALayer, kCAFillModeForwards,
)

from ._dispatch import on_main_thread
from .model import OverallState, StatusModel
from .theme import palette

ASSET = Path(__file__).parent / "assets" / "menubar-icon.pdf"


def _hex_to_cgcolor(hex_value: str, alpha: float = 1.0):
    h = hex_value.lstrip("#")
    r = int(h[0:2], 16) / 255.0
    g = int(h[2:4], 16) / 255.0
    b = int(h[4:6], 16) / 255.0
    return NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, alpha).CGColor()


def _compose_image_with_badge(base: NSImage, badge_hex: str) -> NSImage:
    """Return a new NSImage = base with a 6pt dot of `badge_hex` at the
    bottom-right. The base is drawn first; the dot overlays it."""
    size = base.size()
    out = NSImage.alloc().initWithSize_(size)
    out.lockFocus()
    base.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, size.width, size.height),
        NSMakeRect(0, 0, 0, 0),
        NSCompositingOperationSourceOver,
        1.0,
    )
    NSColor.colorWithSRGBRed_green_blue_alpha_(
        int(badge_hex[1:3], 16) / 255.0,
        int(badge_hex[3:5], 16) / 255.0,
        int(badge_hex[5:7], 16) / 255.0,
        1.0,
    ).set()
    badge = NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(size.width - 7, 0, 6, 6)
    )
    badge.fill()
    out.unlockFocus()
    out.setTemplate_(False)
    return out


class StatusItemController:
    def __init__(self, rumps_app, model: StatusModel) -> None:
        self._app = rumps_app
        self._model = model
        self._base = NSImage.alloc().initWithContentsOfFile_(str(ASSET))
        if self._base is not None:
            self._base.setTemplate_(True)
            self._with_badge = _compose_image_with_badge(self._base, palette.ERROR)
        else:
            self._with_badge = None
        self._pulse_layer = None
        self._apply()
        model.add_observer(on_main_thread(lambda _m: self._apply()))

    def _ns_button(self):
        try:
            return self._app._nsapp.nsstatusitem.button()
        except AttributeError:
            return None

    def _apply(self) -> None:
        btn = self._ns_button()
        if btn is None:
            return
        state = self._model.overall

        if state is OverallState.DAEMON_STOPPED:
            btn.setImage_(self._base)
            btn.setAlphaValue_(0.4)
            self._set_pulse(active=False)
            return

        btn.setAlphaValue_(1.0)

        if self._model.failing_count > 0 and self._with_badge is not None:
            btn.setImage_(self._with_badge)
        else:
            btn.setImage_(self._base)

        self._set_pulse(active=(state is OverallState.RUNNING))

    def _set_pulse(self, *, active: bool) -> None:
        btn = self._ns_button()
        if btn is None:
            return
        btn.setWantsLayer_(True)
        layer = btn.layer()
        if self._pulse_layer is None and active:
            self._pulse_layer = CALayer.layer()
            self._pulse_layer.setFrame_(layer.bounds())
            self._pulse_layer.setBackgroundColor_(
                _hex_to_cgcolor(palette.ACCENT_VIOLET, alpha=0.0)
            )
            self._pulse_layer.setCornerRadius_(4.0)
            layer.addSublayer_(self._pulse_layer)
            anim = CABasicAnimation.animationWithKeyPath_("backgroundColor")
            anim.setFromValue_(_hex_to_cgcolor(palette.ACCENT_VIOLET, alpha=0.0))
            anim.setToValue_(_hex_to_cgcolor(palette.ACCENT_VIOLET, alpha=0.45))
            anim.setDuration_(0.9)
            anim.setAutoreverses_(True)
            anim.setRepeatCount_(1e9)
            anim.setFillMode_(kCAFillModeForwards)
            self._pulse_layer.addAnimation_forKey_(anim, "pulse")
        elif self._pulse_layer is not None and not active:
            self._pulse_layer.removeAllAnimations()
            self._pulse_layer.removeFromSuperlayer()
            self._pulse_layer = None
