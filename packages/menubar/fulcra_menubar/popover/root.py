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
    NSButton, NSColor, NSPopover, NSScrollView, NSTextField, NSView,
    NSViewController, NSMakeRect, NSMakeSize,
    NSBezelStyleRounded,
)

from .._dispatch import on_main_thread
from .._objc_targets import attach as _attach
from ..model import StatusModel
from ..theme import colors, typography
from .header import make_header
from .quick_record import make_quick_record_view


WIDTH = 360.0
HEADER_HEIGHT = 56.0
FOOTER_HEIGHT = 36.0
SWITCHER_HEIGHT = 32.0  # height of the "← Quick Record" back bar in status view
DAEMON_BAR_HEIGHT = 64.0  # see popover/daemon_bar.py — always visible
DEFAULT_BODY_HEIGHT = 240.0
DEFAULT_HEIGHT = (
    HEADER_HEIGHT + DEFAULT_BODY_HEIGHT + DAEMON_BAR_HEIGHT + FOOTER_HEIGHT  # 396
)


class PopoverRoot:
    def __init__(
        self,
        model: StatusModel,
        client,
        *,
        on_preferences: Optional[Callable[[str | None], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
        notify: Optional[Callable[[str, str], None]] = None,
        status_item: Optional[object] = None,
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
        notify:
            Optional ``(title, body)`` callback used by the daemon-controls
            bar to surface launchctl / SMAppService errors as macOS
            notifications.  In production, ``app.py`` forwards this to
            ``NotificationCentre._post`` so the user sees the actual error
            instead of a silent no-op.  Tests pass None.
        status_item:
            Optional reference to the StatusItemController so the
            quick-record popover can toggle the cyan timer overlay on
            the menubar icon when a Duration timer starts/stops. Tests
            pass None.
        """
        self._model = model
        self._client = client
        self._status_item = status_item
        # In-memory state shared across popover open/close cycles:
        # active Duration timers keyed by definition_id, and the
        # "Recently recorded" list. Doesn't survive a menubar restart —
        # that's OK because timers are short-lived and recent entries
        # are recent enough that losing them is acceptable.
        self._active_timers: dict[str, dict] = {}
        self._recent: list[dict] = []
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

        # ── Daemon controls bar (always visible, sits above the footer) ──────
        # Surfaces Start / Stop / Restart + "Open at Login" toggle.  Refreshes
        # its state every time the popover is opened (see toggle()), not on
        # the polling timer — daemon lifecycle is gesture-driven, not realtime.
        from .daemon_bar import make_daemon_bar
        daemon_bar = make_daemon_bar(width=WIDTH, notify=notify)
        daemon_bar.setFrame_(NSMakeRect(0, FOOTER_HEIGHT, WIDTH, DAEMON_BAR_HEIGHT))
        root.addSubview_(daemon_bar)
        self._daemon_bar = daemon_bar

        # ── Body container — holds one sub-view at a time ─────────────────────
        body_top = FOOTER_HEIGHT + DAEMON_BAR_HEIGHT
        body_height = (
            DEFAULT_HEIGHT - HEADER_HEIGHT - FOOTER_HEIGHT - DAEMON_BAR_HEIGHT
        )

        body_container = NSView.alloc().initWithFrame_(
            NSMakeRect(0, body_top, WIDTH, body_height)
        )
        root.addSubview_(body_container)

        # Internal state: which view is shown
        self._showing_status: bool = False

        # ── Build the quick-record primary view ───────────────────────────────
        # Pass body_height so the quick-record view's internal header / scroll /
        # footer split is sized against the container it'll actually live in,
        # not the legacy 360 pt assumption that left content rendering above
        # the visible region (huge empty void in the middle of the popover).
        quick_record_view = make_quick_record_view(
            client=client,
            model=model,
            on_view_status=self._show_status,
            width=WIDTH,
            height=body_height,
            active_timers=self._active_timers,
            recent=self._recent,
            on_timer_changed=self._on_timer_changed,
            on_preferences=on_preferences,
        )
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

            # SP3 task 3 (drift fix D5): the popover plugin-status view used
            # to lump plugins into a single sorted list by `kind`
            # (service / scheduled / manual) — the technical taxonomy from
            # the Plugin contract. The user-facing question in this surface
            # is "is data flowing?", not "what's the implementation type?".
            # Re-group by `collect_mode` to match the historical-vs-live
            # framing that the web UI's collect_modes onboarding introduced.
            # The kind taxonomy stays in Preferences → Plugins (per Q2 in
            # the SP3 plan) where Run-now affordances + scheduling intervals
            # make the technical truth matter.
            #
            # Order matters: most-live first, so a user opening the popover
            # sees the continuously-streaming sources at the top.
            groups: list[tuple[str, str]] = [
                ("live_continuous", "Live (continuous)"),
                ("live_polled", "Live (polled)"),
                ("historical", "Historical (one-shot)"),
            ]
            by_mode: dict[str, list] = {mode: [] for mode, _ in groups}
            for p in self._model.plugins:
                # Defensive: an unknown collect_mode (shouldn't happen
                # post-SP3 task 2) falls into the historical bucket so the
                # plugin still renders rather than vanishing silently.
                by_mode.setdefault(p.collect_mode, by_mode["historical"]).append(p)

            # NSScrollView is bottom-origin; we lay out from top-of-content
            # downward by first computing total height, then placing each
            # element at decreasing y. Headers + rows stack contiguously.
            HEADER_H = 22.0
            total_h = 0.0
            sections = []
            for mode, label in groups:
                plugins_in_mode = sorted(by_mode[mode], key=lambda p: p.name)
                if not plugins_in_mode:
                    # Skip empty groups — no blank "Live (polled)" header
                    # when the user has no polled plugins enabled.
                    continue
                sections.append((label, plugins_in_mode))
                total_h += HEADER_H + ROW_HEIGHT * len(plugins_in_mode)

            content.setFrame_(
                NSMakeRect(0, 0, WIDTH, max(total_h, plugin_area_height))
            )
            # Place sections from top to bottom in the bottom-origin coord
            # system: cursor starts at the top of the content view.
            cursor_y = max(total_h, plugin_area_height)
            for label, plugins_in_mode in sections:
                cursor_y -= HEADER_H
                header_view = _make_group_header(WIDTH, HEADER_H, label)
                header_view.setFrame_(NSMakeRect(0, cursor_y, WIDTH, HEADER_H))
                content.addSubview_(header_view)
                for snapshot in plugins_in_mode:
                    cursor_y -= ROW_HEIGHT
                    row = make_row(
                        snapshot, client=self._client, model=self._model,
                        width=WIDTH,
                    )
                    row.setFrame_(NSMakeRect(0, cursor_y, WIDTH, ROW_HEIGHT))
                    content.addSubview_(row)
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

    # ── Menubar overlay coordination ──────────────────────────────────────────

    def _on_timer_changed(self) -> None:
        """Called by the quick-record view whenever a Duration timer
        starts or stops. We forward the "any timer active?" state to
        the StatusItemController so it can show / hide the cyan glow
        overlay on the menubar icon.

        Coexists with the violet running-pulse: both layers can stack.
        See status_item.py:_apply_timer_overlay for the layering rules.
        """
        if self._status_item is None:
            return
        active = bool(self._active_timers)
        try:
            self._status_item.set_timer_active(active)
        except Exception:  # pragma: no cover — defensive
            import logging
            logging.getLogger("fulcra_menubar.popover").exception(
                "status_item.set_timer_active raised",
            )

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
            # Refresh daemon-controls state so the user always sees the
            # current PID / Running-or-Stopped pill the moment the
            # popover opens (the lifecycle layer is gesture-driven, not
            # polled — see popover/daemon_bar.py for the rationale).
            try:
                self._daemon_bar.refresh()
            except Exception:  # pragma: no cover — defensive
                import logging
                logging.getLogger("fulcra_menubar.popover").exception(
                    "daemon_bar.refresh() raised; popover opening anyway",
                )
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
    view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, SWITCHER_HEIGHT))
    view.setWantsLayer_(True)

    # Tinted background to visually distinguish the navigation bar
    view.layer().setBackgroundColor_(NSColor.controlBackgroundColor().CGColor())

    # Hairline separator at the bottom of the bar
    sep = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 1))
    sep.setWantsLayer_(True)
    sep.layer().setBackgroundColor_(NSColor.separatorColor().CGColor())
    view.addSubview_(sep)

    back_btn = NSButton.alloc().initWithFrame_(NSMakeRect(8, 4, 160, 24))
    back_btn.setTitle_("← Quick Record")
    back_btn.setBezelStyle_(NSBezelStyleRounded)
    _attach(back_btn, lambda _s: on_back())
    view.addSubview_(back_btn)

    return view


def _make_group_header(width: float, height: float, label: str) -> NSView:
    """Section header for the plugin-status group-by-collect_mode view.

    Mirrors the visual style of `quick_record._make_section_header` —
    uppercased small text in the secondary colour, with a hairline
    separator at the bottom — so the popover's two scrollable surfaces
    feel like one cohesive list. Kept local rather than imported from
    quick_record to avoid a cross-module dependency for a small helper
    whose styling may diverge if either surface evolves.
    """
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
