"""The Plugins tab. One expanded row per plugin: enable toggle,
interval input (scheduled only), credentials (Connect / Disconnect),
Run now button.

Edits to enable / interval are persisted via fulcra_collect.config,
followed by a daemon `reload`. Credentials writes go through the
daemon's set_credential / delete_credential — never via the keychain
directly.
"""
from __future__ import annotations

from AppKit import (  # type: ignore[import-not-found]
    NSButton, NSBezelStyleRounded, NSFontAttributeName, NSLineBreakByWordWrapping,
    NSScrollView, NSSecureTextField, NSStringDrawingUsesLineFragmentOrigin,
    NSSwitch, NSTextField, NSView, NSMakeRect, NSMakeSize,
)
from Foundation import NSString  # type: ignore[import-not-found]

from fulcra_collect import config as _config

from .._dispatch import on_main_thread
from .._humanize import humanize_minutes
from .._objc_targets import attach as _attach
from ..daemon_client import DaemonClient
from ..model import PluginSnapshot, StatusModel
from ..theme import colors, typography


def _compute_desc_height(text: str, width: float, font, cap: float = 80.0) -> float:
    """Measure the rendered height of a word-wrapped description label.

    Why: the description block used to be a hardcoded 32pt — anything
    past 2 lines was silently clipped. Computing the real height lets
    the row grow to fit (capped so a runaway description doesn't
    swallow the whole tab). See SP1 L2 in the 2026-05-27 menubar
    drift audit.

    Args:
        text: the description string.
        width: pixel width the label will be laid out into.
        font: NSFont to render with.
        cap: maximum height we'll allow; anything taller will scroll
             behind clipping (acceptable since the truncation is now
             very rare — ~5 lines of small text is plenty for our
             actual plugin descriptions).
    """
    if not text:
        return 32.0
    attrs = {NSFontAttributeName: font}
    ns_text = NSString.stringWithString_(text)
    bound = ns_text.boundingRectWithSize_options_attributes_(
        NSMakeSize(width, 1000.0),
        NSStringDrawingUsesLineFragmentOrigin,
        attrs,
    )
    needed = float(bound.size.height) + 4.0  # 4pt visual padding
    return min(max(needed, 32.0), cap)


class _FlippedView(NSView):  # type: ignore[misc]
    """NSView with a top-left origin (y=0 at top). Used as the Plugins-tab
    scroll-view document view so the first sorted plugin renders at the
    top of the visible area. Without flipping, AppKit's default
    bottom-left origin reverses the visual order and breaks hit-testing
    for subviews whose frames use `height - X` math."""

    def isFlipped(self):  # noqa: N802 — ObjC selector name
        return True


def make_plugins_tab(*, model: StatusModel, client: DaemonClient) -> NSView:
    width = 640.0
    height = 440.0
    # Wrap the scroll view in a parent NSView so the tab's root view can be
    # painted white — prevents dark-mode system chrome bleeding through the
    # transparent NSScrollView chrome.
    outer = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    outer.setWantsLayer_(True)
    outer.layer().setBackgroundColor_(colors.bg().CGColor())

    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)

    # The content view is FLIPPED (y=0 at top) so the first sorted plugin
    # lands at the top of the scroll view and the visual order matches the
    # sort. Without this, AppKit's default unflipped semantics put the
    # first row at the BOTTOM of the content — visually reversing the list
    # AND causing hit-testing weirdness because subview frames computed
    # with `height - X` math get mapped wrong relative to scroll position.
    content = _FlippedView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 0))
    content.setWantsLayer_(True)
    content.layer().setBackgroundColor_(colors.bg().CGColor())
    scroll.setDocumentView_(content)

    # Track the last plugin id set so we can short-circuit observer calls when
    # the plugin list hasn't changed (e.g. a mere status-timestamp update).
    # credential_status is a blocking UDS call; skipping it on unchanged lists
    # prevents the N-plugin × 5 s freeze described in Bug 4.
    _last_state: dict = {"plugin_ids": None}

    def rebuild(_model=None):
        plugin_ids = tuple(sorted(p.id for p in model.plugins))
        if plugin_ids == _last_state["plugin_ids"]:
            return  # plugin set unchanged — no need to rebuild the tab
        _last_state["plugin_ids"] = plugin_ids

        # Hoist credential_status fetches so row-height calculation has the
        # actual credential count before any row view is created. Each call is
        # a single blocking UDS round-trip; the short-circuit above ensures
        # this block runs only when the plugin set genuinely changes.
        cred_map: dict[str, dict[str, str]] = {}
        for snap in model.plugins:
            try:
                cred_reply = client.credential_status(snap.id)
                cred_map[snap.id] = (
                    cred_reply.get("credentials", {})
                    if cred_reply.get("ok")
                    else {}
                )
            except Exception:
                cred_map[snap.id] = {}

        for sv in list(content.subviews()):
            sv.removeFromSuperview()

        # Onboarding paragraph at the top of the tab. Doubles as the
        # padding that pushes the first plugin row clear of the tab bar
        # (without this, the tab buttons sit visually on top of the first
        # row's name+toggle, making the toggle unclickable).
        intro_text = (
            "Enable the plugins you want recorded into Fulcra. Some need "
            "credentials (paste-secret fields); some are scheduled and run "
            "automatically; some are manual — for those, click the Fulcra "
            "Collect icon in the menubar and pick \"Run now\"."
        )
        intro = NSTextField.labelWithString_(intro_text)
        intro.setFont_(typography.small())
        intro.setTextColor_(colors.text_secondary())
        intro.setLineBreakMode_(NSLineBreakByWordWrapping)
        intro.setFrame_(NSMakeRect(16, 12, width - 32, 56))
        content.addSubview_(intro)

        y = 80  # below the intro paragraph + a touch of breathing room
        ordered = sorted(model.plugins, key=lambda p: (p.kind, p.name))
        for snap in ordered:
            credentials = cred_map.get(snap.id, {})
            # Description block grows to fit — capped at 80pt so a runaway
            # description can't swallow the whole tab. The 4 fixed regions
            # of the row are: 28 name + desc_h + 28 interval-or-pad + 24
            # run btn, plus 24pt per credential. See SP1 L2 in the
            # 2026-05-27 menubar drift audit.
            desc_h = _compute_desc_height(
                snap.description or "",
                width - 120,
                typography.small(),
            )
            row_height = 28 + desc_h + 28 + 24 + 24 * len(credentials)
            row = _make_plugin_row(snap, width, row_height, desc_h=desc_h,
                                   credentials=credentials,
                                   client=client, model=model)
            row.setFrame_(NSMakeRect(0, y, width, row_height))
            content.addSubview_(row)
            y += row_height
        content.setFrame_(NSMakeRect(0, 0, width, max(y, height)))

    rebuild()
    model.add_observer(on_main_thread(rebuild))
    outer.addSubview_(scroll)
    return outer


_COLLECT_MODE_LABEL = {
    "historical": "Historical",
    "live_polled": "Live (polled)",
    "live_continuous": "Live (continuous)",
}


def _make_collect_mode_chip(mode: str) -> NSView:
    """Build a small bordered chip showing the plugin's collect_mode.

    Augments — does not replace — the kind taxonomy (service / scheduled
    / manual), per user Q2 on the SP3 plan: "kind" still drives Run-now
    affordances and interval scheduling in this Preferences tab, while
    "collect_mode" answers the user-facing question "is this data stream
    live or historical?" See SP3 D5 in the 2026-05-27 menubar drift
    audit.

    Implementation note: a plain bordered NSTextField produced a too-loud
    inset-bezel look on macOS, so we wrap a labelled NSTextField inside
    a layer-backed NSView whose CALayer carries a thin 1pt border and a
    pill-shaped corner radius. This gives a clean chip independent of
    the system's text-field chrome.
    """
    label_text = _COLLECT_MODE_LABEL.get(mode, mode)
    label = NSTextField.labelWithString_(label_text)
    label.setFont_(typography.small())
    label.setTextColor_(colors.text_secondary())
    label.sizeToFit()
    label_w = float(label.frame().size.width)
    label_h = float(label.frame().size.height)

    # 8pt horizontal padding inside the chip, 2pt vertical.
    chip_w = label_w + 16
    chip_h = label_h + 4
    chip = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, chip_w, chip_h))
    chip.setWantsLayer_(True)
    layer = chip.layer()
    layer.setBorderWidth_(1.0)
    layer.setBorderColor_(colors.border().CGColor())
    layer.setCornerRadius_(chip_h / 2.0)

    label.setFrame_(NSMakeRect(8, 2, label_w, label_h))
    chip.addSubview_(label)
    return chip


def _make_plugin_row(snap: PluginSnapshot, width: float, height: float,
                     *, desc_h: float = 32.0,
                     credentials: dict[str, str],
                     client: DaemonClient, model: StatusModel) -> NSView:
    row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))

    name = NSTextField.labelWithString_(f"{snap.name}  ({snap.id})")
    name.setFont_(typography.body())
    name.setTextColor_(colors.text())
    name.setFrame_(NSMakeRect(16, height - 28, width - 200, 18))
    row.addSubview_(name)

    # collect_mode chip — sits to the left of the Run-now button or
    # Enable switch on the name row. Augments the kind label (which
    # stays as part of the row's behaviour — kind drives the toggle vs
    # button affordance below) per user Q2 on the SP3 plan.
    chip = _make_collect_mode_chip(snap.collect_mode)
    chip_frame = chip.frame()
    chip_w = chip_frame.size.width
    chip_h = chip_frame.size.height
    # Right-edge of the name row's control column is width-80 (where the
    # switch / Run-now button starts). Park the chip 8pt to the left of
    # that, vertically centred on the name baseline.
    chip_x = width - 80 - chip_w - 8
    chip_y = height - 28 + (18 - chip_h) / 2.0
    chip.setFrame_(NSMakeRect(chip_x, chip_y, chip_w, chip_h))
    row.addSubview_(chip)

    # Description label — 12pt secondary text, word-wrapped. Height is
    # computed by the caller (rebuild() in build_plugins_tab) so the row
    # grows to fit instead of clipping at 2 lines.
    if snap.description:
        desc = NSTextField.labelWithString_(snap.description)
        desc.setFont_(typography.small())
        desc.setTextColor_(colors.text_secondary())
        desc.setLineBreakMode_(NSLineBreakByWordWrapping)
        # Description sits 28pt below the row top (height - 28 is the
        # name's baseline; the description bottom is height - 28 - desc_h).
        desc.setFrame_(NSMakeRect(16, height - 28 - desc_h,
                                  width - 120, desc_h))
        row.addSubview_(desc)

    if snap.kind == "manual":
        # Manual plugins have no automatic firing cycle — the Enable toggle is
        # meaningless (daemon never auto-polls them). Replace it with a prominent
        # Run-now button that is always reachable, regardless of enabled state.
        run_btn_top = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - 80, height - 36, 64, 28)
        )
        run_btn_top.setTitle_("Run now")
        run_btn_top.setBezelStyle_(NSBezelStyleRounded)
        _attach(run_btn_top, lambda _s: client.run(snap.id))
        row.addSubview_(run_btn_top)
    else:
        # service / scheduled: keep the Enable toggle so the daemon knows
        # whether to supervise (service) or include in the polling cycle (scheduled).
        enabled_switch = NSSwitch.alloc().initWithFrame_(
            NSMakeRect(width - 80, height - 32, 50, 22)
        )
        enabled_switch.setState_(1 if snap.enabled else 0)

        def on_toggle(sender):
            cfg = _config.load()
            if sender.state():
                cfg.enable(snap.id)
            else:
                cfg.disable(snap.id)
            _config.save(cfg)
            client.reload()
        _attach(enabled_switch, on_toggle)
        row.addSubview_(enabled_switch)

    # Interval input — scheduled only. Placed below the description block.
    # Layout:  "Every" [field] "minutes"
    #          ≈ 6 hours            (live caption, updates as user types)
    if snap.kind == "scheduled":
        cfg = _config.load()
        override = cfg.interval_overrides.get(snap.id)
        seconds = override if override is not None else (snap.default_interval_s or 3600)
        initial_minutes = max(seconds // 60, 1)

        # Interval block top — sits below the description. With desc_h=32
        # this equals the old hardcoded `height - 88`; for taller
        # descriptions the whole block slides down to follow.
        interval_y_top = height - 28 - desc_h - 28

        every_label = NSTextField.labelWithString_("Every")
        every_label.setFont_(typography.small())
        every_label.setFrame_(NSMakeRect(16, interval_y_top, 44, 16))
        row.addSubview_(every_label)

        interval_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(64, interval_y_top - 4, 60, 22)
        )
        interval_field.setStringValue_(str(initial_minutes))

        minutes_label = NSTextField.labelWithString_("minutes")
        minutes_label.setFont_(typography.small())
        minutes_label.setFrame_(NSMakeRect(130, interval_y_top, 60, 16))
        row.addSubview_(minutes_label)

        humanize_caption = NSTextField.labelWithString_(
            f"≈ {humanize_minutes(initial_minutes)}"
        )
        humanize_caption.setFont_(typography.small())
        humanize_caption.setTextColor_(colors.text_secondary())
        humanize_caption.setFrame_(NSMakeRect(16, interval_y_top - 22, 200, 16))
        row.addSubview_(humanize_caption)

        def on_interval_change(sender, _caption=humanize_caption):
            try:
                minutes = max(int(sender.stringValue()), 1)
            except ValueError:
                return
            _caption.setStringValue_(f"≈ {humanize_minutes(minutes)}")
            cfg2 = _config.load()
            cfg2.set_interval(snap.id, minutes * 60)
            _config.save(cfg2)
            client.reload()
        _attach(interval_field, on_interval_change, action="textChanged:")
        row.addSubview_(interval_field)

    # Run now at the bottom of the row — scheduled only and only when enabled.
    # Manual plugins already have their Run-now button in the top action area above.
    if snap.enabled and snap.kind == "scheduled":
        run_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - 200, 16, 100, 24)
        )
        run_btn.setTitle_("Run now")
        run_btn.setBezelStyle_(NSBezelStyleRounded)

        def on_run(_s):
            client.run(snap.id)
            model.mark_in_flight(snap.id)
        _attach(run_btn, on_run)
        row.addSubview_(run_btn)

    # Credentials block — pre-fetched by rebuild() and passed in.
    yoff = 16 + 24
    for key, state in credentials.items():
        label = NSTextField.labelWithString_(f"  {key}: ")
        label.setFont_(typography.small())
        label.setTextColor_(colors.text_secondary())
        label.setFrame_(NSMakeRect(16, yoff, 220, 16))
        row.addSubview_(label)

        if state == "set":
            badge = NSTextField.labelWithString_("Connected")
            badge.setFont_(typography.small())
            badge.setTextColor_(colors.mint())
            badge.setFrame_(NSMakeRect(220, yoff, 100, 16))
            row.addSubview_(badge)

            disc = NSButton.alloc().initWithFrame_(NSMakeRect(330, yoff - 4, 100, 24))
            disc.setTitle_("Disconnect")
            disc.setBezelStyle_(NSBezelStyleRounded)
            _attach(disc, lambda _s, key=key: (
                client.delete_credential(snap.id, key),
            ))
            row.addSubview_(disc)
        else:
            field = NSSecureTextField.alloc().initWithFrame_(
                NSMakeRect(220, yoff - 2, 200, 22)
            )
            field.setPlaceholderString_("paste secret")
            row.addSubview_(field)

            conn = NSButton.alloc().initWithFrame_(NSMakeRect(430, yoff - 4, 80, 24))
            conn.setTitle_("Connect")
            conn.setBezelStyle_(NSBezelStyleRounded)

            def _on_connect(_s, key=key, field=field):
                value = field.stringValue().strip()
                if not value:
                    return
                client.set_credential(snap.id, key, value)

            _attach(conn, _on_connect)
            row.addSubview_(conn)

        yoff += 24

    return row


