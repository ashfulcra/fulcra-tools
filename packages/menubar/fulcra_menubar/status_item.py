"""The menubar icon. Holds a reference to the NSStatusItem owned by
rumps and applies overlay states driven by the StatusModel.

Four overlays:
  - running pulse: a violet glow CALayer that fades in/out while the
    in-flight set is non-empty.
  - timer-active overlay: a steady (non-pulsing) cyan glow CALayer that
    shows while a quick-record Duration timer is running in the
    popover. Stays visible AROUND the running pulse — they coexist;
    layers stack rather than override. The timer is in-memory in the
    popover (Sprint B 2026-05-26); a daemon restart kills it.
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
    NSImage, NSMakeRect, NSMakeSize,
)

# macOS menubar items render at 22pt. The source PNG is 88x88 (high-res
# for retina rendering); we set the LOGICAL size to 22x22 so AppKit
# displays it at the right scale while keeping the underlying pixels
# crisp at any density. Without this, the status item is invisible
# because the 88pt image overflows the menubar slot and gets clipped.
_MENUBAR_ICON_SIZE = NSMakeSize(22, 22)
from Quartz import (  # type: ignore[import-not-found]
    CABasicAnimation, CALayer, kCAFillModeForwards,
)

from ._dispatch import on_main_thread
from .model import OverallState, StatusModel
from .theme import palette

ASSET = Path(__file__).parent / "assets" / "menubar-icon.png"


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
            self._base.setSize_(_MENUBAR_ICON_SIZE)
            self._base.setTemplate_(True)
            # Two tiers of failure overlay — amber for 1-2 consecutive
            # failures ("Failed, give it a beat"), red for >=3 ("Failing,
            # look at this now"). Matches the dashboard pill mapping
            # landed 2026-05-26 and the product-brainstorming gap that
            # observed "any failure = red dot" was too noisy.
            self._with_warning_badge = _compose_image_with_badge(self._base, palette.WARNING)
            self._with_warning_badge.setSize_(_MENUBAR_ICON_SIZE)
            self._with_critical_badge = _compose_image_with_badge(self._base, palette.ERROR)
            self._with_critical_badge.setSize_(_MENUBAR_ICON_SIZE)
        else:
            self._with_warning_badge = None
            self._with_critical_badge = None
        self._pulse_layer = None
        # Timer-active overlay layer — separate from the run pulse so
        # both can show simultaneously (run pulse = "an importer is
        # working", timer overlay = "a quick-record timer is ticking").
        self._timer_layer = None
        self._timer_active = False
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

        # Red dot wins over amber when any plugin is at >=3 failures, even
        # if other plugins are only at 1-2. The user needs to see the worst
        # state first; the popover surfaces the per-plugin breakdown.
        if (self._model.failing_critical_count > 0
                and self._with_critical_badge is not None):
            btn.setImage_(self._with_critical_badge)
        elif (self._model.failing_warning_count > 0
                and self._with_warning_badge is not None):
            btn.setImage_(self._with_warning_badge)
        else:
            btn.setImage_(self._base)

        self._set_pulse(active=(state is OverallState.RUNNING))
        # Reapply the timer overlay so a model-driven _apply() doesn't
        # blow it away when the run pulse toggles.
        self._apply_timer_overlay()

    def set_timer_active(self, active: bool) -> None:
        """Public API: toggle the cyan timer-active overlay on the
        menubar icon. Called by the quick-record popover when a Duration
        timer starts (active=True) or stops (active=False).

        The overlay is intentionally separate from the run pulse so the
        two states coexist: a violet pulse means an importer is working;
        a steady cyan glow means a quick-record timer is ticking. If
        both are true the user sees the violet pulse on top of the cyan
        glow.

        Timer state lives in the popover (in-memory), NOT in the model
        — a daemon restart kills any in-flight timer.
        """
        self._timer_active = bool(active)
        # _apply() will pick up the new state on the next observer tick,
        # but also reflect immediately for snappy feedback.
        self._apply_timer_overlay()

    def _apply_timer_overlay(self) -> None:
        btn = self._ns_button()
        if btn is None:
            return
        btn.setWantsLayer_(True)
        layer = btn.layer()
        active = self._timer_active
        if self._timer_layer is None and active:
            self._timer_layer = CALayer.layer()
            self._timer_layer.setFrame_(layer.bounds())
            # Steady cyan glow — non-pulsing on purpose so it reads as
            # "something is sustained" vs. the violet pulse's "something
            # is happening RIGHT NOW".
            self._timer_layer.setBackgroundColor_(
                _hex_to_cgcolor(palette.ACCENT_CYAN, alpha=0.35)
            )
            self._timer_layer.setCornerRadius_(4.0)
            # Insert beneath the pulse layer so the violet pulse (when
            # it's also showing) appears on top.
            layer.insertSublayer_atIndex_(self._timer_layer, 0)
        elif self._timer_layer is not None and not active:
            self._timer_layer.removeFromSuperlayer()
            self._timer_layer = None

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
