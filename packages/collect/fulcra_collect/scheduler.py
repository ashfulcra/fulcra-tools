"""Scheduling — a pure function deciding which scheduled plugins are due.

The daemon loop calls `due_plugins` periodically; keeping the decision
pure (no clock, no I/O) makes it directly testable with fixed times.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .config import Config
from .plugin import Plugin
from .state import PluginState


def effective_interval(plugin: Plugin, cfg: Config) -> timedelta:
    """The plugin's scheduling interval — the user override if set,
    otherwise its declared default."""
    override = cfg.interval_overrides.get(plugin.id)
    if override is not None:
        return timedelta(seconds=override)
    assert plugin.default_interval is not None  # guaranteed for kind=scheduled
    return plugin.default_interval


def due_plugins(plugins: dict[str, Plugin], cfg: Config,
                states: dict[str, PluginState], now: datetime,
                online: bool = True) -> list[str]:
    """Return the ids of enabled scheduled plugins whose next run is due.

    A plugin is due when `now - last_run >= interval` — so a plugin
    overdue by many intervals (a long sleep) is returned exactly ONCE,
    not once per missed interval; the single run back-fills the gap via
    its watermark. When `online` is False, plugins with
    `requires_network` are skipped — deferred, not failed — so offline
    time never burns the degraded-failure budget.
    """
    due: list[str] = []
    for pid, plugin in sorted(plugins.items()):
        if plugin.kind != "scheduled" or pid not in cfg.enabled:
            continue
        if plugin.requires_network and not online:
            continue
        st = states.get(pid)
        if st is None or st.last_run is None:
            due.append(pid)
            continue
        if now - st.last_run >= effective_interval(plugin, cfg):
            due.append(pid)
    return due
