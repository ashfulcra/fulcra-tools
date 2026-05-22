"""The scheduler — pure 'which scheduled plugins are due' logic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulcra_collect.config import Config
from fulcra_collect.plugin import Plugin
from fulcra_collect.scheduler import due_plugins
from fulcra_collect.state import PluginState

T0 = datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)


def _scheduled(pid, hours):
    return Plugin(id=pid, name=pid, kind="scheduled", run=lambda ctx: None,
                  default_interval=timedelta(hours=hours))


def test_a_never_run_enabled_plugin_is_due():
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled={"lastfm"})
    due = due_plugins(plugins, cfg, states={}, now=T0)
    assert due == ["lastfm"]


def test_a_disabled_plugin_is_never_due():
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled=set())
    assert due_plugins(plugins, cfg, states={}, now=T0) == []


def test_a_plugin_run_recently_is_not_due():
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled={"lastfm"})
    states = {"lastfm": PluginState("lastfm", last_run=T0 - timedelta(minutes=30))}
    assert due_plugins(plugins, cfg, states, now=T0) == []


def test_a_plugin_past_its_interval_is_due():
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled={"lastfm"})
    states = {"lastfm": PluginState("lastfm", last_run=T0 - timedelta(hours=2))}
    assert due_plugins(plugins, cfg, states, now=T0) == ["lastfm"]


def test_interval_override_is_respected():
    plugins = {"lastfm": _scheduled("lastfm", 6)}  # default 6h
    cfg = Config(enabled={"lastfm"}, interval_overrides={"lastfm": 600})  # 10 min
    states = {"lastfm": PluginState("lastfm", last_run=T0 - timedelta(minutes=15))}
    assert due_plugins(plugins, cfg, states, now=T0) == ["lastfm"]


def test_manual_and_service_plugins_are_never_scheduled():
    plugins = {
        "dayone": Plugin(id="dayone", name="d", kind="manual", run=lambda c: None),
        "relay": Plugin(id="relay", name="r", kind="service", run=lambda c: None),
    }
    cfg = Config(enabled={"dayone", "relay"})
    assert due_plugins(plugins, cfg, states={}, now=T0) == []


def test_a_long_sleep_yields_exactly_one_catch_up_run():
    # Overdue by 50 intervals (a machine asleep for ~2 days) — still ONE run.
    plugins = {"lastfm": _scheduled("lastfm", 1)}
    cfg = Config(enabled={"lastfm"})
    states = {"lastfm": PluginState("lastfm", last_run=T0 - timedelta(hours=50))}
    assert due_plugins(plugins, cfg, states, now=T0) == ["lastfm"]


def test_offline_excludes_network_requiring_plugins():
    plugins = {"lastfm": _scheduled("lastfm", 1)}  # requires_network defaults True
    cfg = Config(enabled={"lastfm"})
    assert due_plugins(plugins, cfg, states={}, now=T0, online=False) == []


def test_offline_still_runs_plugins_that_do_not_need_network():
    podcasts = Plugin(id="podcasts", name="Podcasts", kind="scheduled",
                      run=lambda c: None, default_interval=timedelta(hours=1),
                      requires_network=False)
    cfg = Config(enabled={"podcasts"})
    assert due_plugins({"podcasts": podcasts}, cfg, states={}, now=T0,
                       online=False) == ["podcasts"]
