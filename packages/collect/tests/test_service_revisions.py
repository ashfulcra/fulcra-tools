"""Service reconfiguration must retire the old worker before replacing it."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import tomlkit

from fulcra_collect import config
from fulcra_collect.daemon import Daemon
from fulcra_collect.plugin import Plugin
from fulcra_collect.registry import RegistryResult
from fulcra_collect.supervisor import ServiceSupervisor


class Process:
    def __init__(self):
        self.alive = True
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.terminated += 1

    def kill(self):
        self.killed += 1
        self.alive = False


def make_supervisor():
    elapsed = [0.0]
    supervisor = ServiceSupervisor(clock=lambda: elapsed[0])
    processes = []
    def spawn(_):
        assert all(proc.poll() is not None for proc in processes), "overlapping service workers"
        process = Process()
        processes.append(process)
        return process
    return supervisor, processes, elapsed, spawn


def test_revision_change_waits_for_old_exit_and_adopts_the_latest_revision():
    sup, processes, elapsed, spawn = make_supervisor()
    start = datetime(2026, 10, 23, tzinfo=timezone.utc)
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "a"}, spawn=spawn)
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "b"}, spawn=spawn)
    assert processes[0].terminated == 1
    assert len(processes) == 1
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "c"}, spawn=spawn)
    assert processes[0].terminated == 1
    processes[0].alive = False
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "c"}, spawn=spawn)
    assert len(processes) == 2
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "c"}, spawn=spawn)
    assert processes[1].terminated == 0
    assert not sup._exits.get("relay")


def test_stuck_reconfigured_worker_is_killed_before_replacement():
    sup, processes, elapsed, spawn = make_supervisor()
    start = datetime(2026, 10, 23, tzinfo=timezone.utc)
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "a"}, spawn=spawn)
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "b"}, spawn=spawn)
    elapsed[0] = 9
    sup.tick(now=start + timedelta(seconds=9), enabled_ids={"relay"}, revisions={"relay": "b"}, spawn=spawn)
    assert len(processes) == 1 and processes[0].killed == 0
    elapsed[0] = 10
    sup.tick(now=start + timedelta(seconds=10), enabled_ids={"relay"}, revisions={"relay": "b"}, spawn=spawn)
    assert processes[0].killed == 1
    assert len(processes) == 2
    assert "relay" not in sup.degraded


def test_disable_reenable_cannot_spawn_over_a_worker_still_stopping():
    sup, processes, elapsed, spawn = make_supervisor()
    start = datetime(2026, 10, 23, tzinfo=timezone.utc)
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "a"}, spawn=spawn)
    sup.tick(now=start, enabled_ids=set(), revisions={}, spawn=spawn)
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "a"}, spawn=spawn)
    assert len(processes) == 1
    assert processes[0].terminated == 1
    processes[0].alive = False
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "a"}, spawn=spawn)
    assert len(processes) == 2


def test_new_revision_resets_a_crash_loop_without_waiting_for_cooldown():
    sup, processes, elapsed, spawn = make_supervisor()
    start = datetime(2026, 10, 23, tzinfo=timezone.utc)
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "a"}, spawn=spawn)
    processes[0].alive = False
    sup.degraded.add("relay")
    sup._exits["relay"] = [start] * 6
    sup._backoff_until["relay"] = start + timedelta(minutes=5)
    sup.tick(now=start, enabled_ids={"relay"}, revisions={"relay": "b"}, spawn=spawn)
    assert len(processes) == 2
    assert "relay" not in sup.degraded


def test_unknown_revision_cannot_start_or_stop_a_worker():
    sup, processes, elapsed, spawn = make_supervisor()
    with pytest.raises(ValueError, match="revision"):
        sup.tick(now=datetime(2026, 10, 23, tzinfo=timezone.utc), enabled_ids={"relay"}, revisions={}, spawn=spawn)
    assert not processes


def daemon_with_service():
    plugin = Plugin(id="relay", name="Synthetic receiver", kind="service", collect_mode="live_continuous", run=lambda _: None)
    return Daemon(registry=RegistryResult(plugins={"relay": plugin}), config=config.Config())


def test_runtime_plan_reads_current_settings_including_manual_toml_edits(collect_home):
    daemon = daemon_with_service()
    cfg = config.load()
    cfg.enable("relay")
    cfg.update_plugin_settings("relay", {"port": 1000})
    config.save(cfg)
    ready, enabled, before = daemon._runtime_configuration()
    assert ready and enabled == {"relay"}
    epoch = config.load().plugin_epochs["relay"]
    path = config._config_path()
    doc = tomlkit.parse(path.read_text())
    doc["plugin_settings"]["relay"]["port"] = 2000
    path.write_text(tomlkit.dumps(doc))
    ready, enabled, after = daemon._runtime_configuration()
    assert config.load().plugin_epochs["relay"] == epoch
    assert ready and enabled == {"relay"} and before != after
    assert daemon.config.plugin_settings["relay"]["port"] == 2000


def test_runtime_plan_stops_services_during_account_transition(collect_home):
    daemon = daemon_with_service()
    cfg = config.load()
    cfg.enable("relay")
    config.save(cfg)
    config._persist_account_transition(config._config_path(), True)
    ready, enabled, revisions = daemon._runtime_configuration()
    assert not ready and not enabled and not revisions


def test_unreadable_runtime_configuration_stops_work_without_echoing_contents(collect_home, caplog):
    daemon = daemon_with_service()
    config._config_path().write_text('enabled = [\nprivate_value = "synthetic-secret-value"\n')
    ready, enabled, revisions = daemon._runtime_configuration()
    assert not ready and not enabled and not revisions
    assert "synthetic-secret-value" not in caplog.text
    assert "configuration" in caplog.text.lower()


@pytest.mark.parametrize("blocked", ["account", "unreadable"])
def test_dispatch_pauses_scheduled_work_and_retires_services_on_unknown_consent(collect_home, monkeypatch, blocked):
    import fulcra_collect.daemon as daemon_module
    daemon = daemon_with_service()
    cfg = config.load()
    cfg.enable("relay")
    config.save(cfg)
    if blocked == "account":
        config._persist_account_transition(config._config_path(), True)
    else:
        config._config_path().write_text("enabled = [")
    ticks = []
    supervisor = SimpleNamespace(tick=lambda **kwargs: ticks.append(kwargs))
    monkeypatch.setattr(daemon_module, "is_online", lambda: pytest.fail("source dispatch must remain paused"))
    daemon._dispatch_tick(supervisor, now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    assert len(ticks) == 1
    assert ticks[0]["enabled_ids"] == set() and ticks[0]["revisions"] == {}


def test_dispatch_uses_fresh_config_for_services_and_scheduled_sources(collect_home, monkeypatch):
    import fulcra_collect.daemon as daemon_module
    daemon = daemon_with_service()
    cfg = config.load()
    cfg.enable("relay")
    cfg.update_plugin_settings("relay", {"port": 2000})
    config.save(cfg)
    ticks, dispatched = [], []
    supervisor = SimpleNamespace(tick=lambda **kwargs: ticks.append(kwargs))
    monkeypatch.setattr(daemon_module, "is_online", lambda: True)
    def due(plugins, current, states, now, online):
        assert current.plugin_settings["relay"]["port"] == 2000
        return ["synthetic-scheduled"]
    monkeypatch.setattr(daemon_module, "due_plugins", due)
    monkeypatch.setattr(daemon, "_trigger", lambda pid: dispatched.append(pid))
    daemon._dispatch_tick(supervisor, now=datetime(2026, 10, 23, tzinfo=timezone.utc))
    assert ticks[0]["enabled_ids"] == {"relay"} and ticks[0]["revisions"]["relay"]
    assert dispatched == ["synthetic-scheduled"]
