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
    NSButton, NSBezelStyleRounded, NSScrollView, NSSecureTextField,
    NSSwitch, NSTextField, NSView, NSMakeRect,
)
from Foundation import NSObject  # type: ignore[import-not-found]

from fulcra_collect import config as _config

from .._dispatch import on_main_thread
from ..daemon_client import DaemonClient
from ..model import PluginSnapshot, StatusModel
from ..theme import colors, typography


def make_plugins_tab(*, model: StatusModel, client: DaemonClient) -> NSView:
    width = 640.0
    height = 440.0
    scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(0)
    scroll.setDrawsBackground_(False)

    content = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, 0))
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
        y = 0
        ordered = sorted(model.plugins, key=lambda p: (p.kind, p.name))
        for snap in ordered:
            credentials = cred_map.get(snap.id, {})
            row_height = 80 + 24 * len(credentials)
            row = _make_plugin_row(snap, width, row_height, credentials=credentials,
                                   client=client, model=model)
            row.setFrame_(NSMakeRect(0, y, width, row_height))
            content.addSubview_(row)
            y += row_height
        content.setFrame_(NSMakeRect(0, 0, width, max(y, height)))

    rebuild()
    model.add_observer(on_main_thread(rebuild))
    return scroll


def _make_plugin_row(snap: PluginSnapshot, width: float, height: float,
                     *, credentials: dict[str, str],
                     client: DaemonClient, model: StatusModel) -> NSView:
    row = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, height))

    name = NSTextField.labelWithString_(f"{snap.name}  ({snap.id})")
    name.setFont_(typography.body())
    name.setTextColor_(colors.text())
    name.setFrame_(NSMakeRect(16, height - 28, width - 200, 18))
    row.addSubview_(name)

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
    _Target.attach(enabled_switch, on_toggle)
    row.addSubview_(enabled_switch)

    # Interval input — scheduled only.
    if snap.kind == "scheduled":
        cfg = _config.load()
        override = cfg.interval_overrides.get(snap.id)
        seconds = override if override is not None else (snap.default_interval_s or 3600)
        interval_label = NSTextField.labelWithString_("Interval (minutes):")
        interval_label.setFont_(typography.small())
        interval_label.setFrame_(NSMakeRect(16, height - 56, 140, 16))
        row.addSubview_(interval_label)

        interval_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(160, height - 60, 60, 22)
        )
        interval_field.setStringValue_(str(max(seconds // 60, 1)))

        def on_interval_change(sender):
            try:
                minutes = max(int(sender.stringValue()), 1)
            except ValueError:
                return
            cfg2 = _config.load()
            cfg2.set_interval(snap.id, minutes * 60)
            _config.save(cfg2)
            client.reload()
        _Target.attach(interval_field, on_interval_change, action="textChanged:")
        row.addSubview_(interval_field)

    # Run now (manual + scheduled, only when enabled).
    if snap.enabled and snap.kind in ("manual", "scheduled"):
        run_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(width - 200, 16, 100, 24)
        )
        run_btn.setTitle_("Run now")
        run_btn.setBezelStyle_(NSBezelStyleRounded)

        def on_run(_s):
            client.run(snap.id)
            model.mark_in_flight(snap.id)
        _Target.attach(run_btn, on_run)
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
            _Target.attach(disc, lambda _s, key=key: (
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
            _Target.attach(conn, lambda _s, key=key, field=field: (
                client.set_credential(snap.id, key, field.stringValue()),
            ))
            row.addSubview_(conn)

        yoff += 24

    return row


class _Target:
    _retain: list = []

    @classmethod
    def attach(cls, control, callable_, action: str = "call:"):
        class _T(NSObject):
            def call_(self, sender):
                callable_(sender)
        target = _T.alloc().init()
        control.setTarget_(target)
        control.setAction_(action)
        cls._retain.append(target)
